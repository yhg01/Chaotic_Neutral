"""Owner-scoped memory and private day plans over existing saved Actor jobs.

Inspired by Generative Agents' memory/retrieval/planning decomposition. This is
new code, not imported Smallville code or world state. Retrieval is lexical and
local. No embedding provider, additional model, timer or world effect is added.
"""

import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from agentrpg.actor import (
    ActorOutput,
    ActorView,
    validate_actor_cognition,
    validate_current_cue_dialogue,
)
from agentrpg.actor_memory_schema import (
    MEMORY_PROTOCOL,
    CognitionUpdate,
    DailyAgenda,
    MemoryProfile,
    validate_cognition,
)
from agentrpg.console_execution import save_private
from agentrpg.director import DirectorError, row, rows
from agentrpg.director_clock import DirectorClock, lock_key
from agentrpg.simulation_clock import ClockConflict, digest

POLICY = (
    "This is your private memory and personal agenda. Observations preserve who said "
    "what; assistant claims are not independently verified outcomes. Thoughts are "
    "fallible inferences. Agenda slots are intentions, never proof of activity, "
    "booking, consent, payment or another person's agreement. Current cues, visible "
    "native receipts and current authority take precedence. Do not restart paid work "
    "from a memory. You may use the current Actor response to propose evidence-linked "
    "thoughts and today's or tomorrow's UTC agenda in cognition. An empty agenda "
    "explicitly replaces that day's previous intention. No extra call is needed. "
    "Keep private planning separate from owner dialogue. A required_agendas entry belongs "
    "only in cognition.agendas; never ask the assistant to maintain its own private cognition. "
    "A genuine request to update your file remains owner dialogue. "
    "An elapsed slot is unverified, not completed. Existing Clock events alone wake you."
)


def relevance(query, text):
    a, b = (
        Counter(re.findall(r"\w+", query.casefold())),
        Counter(re.findall(r"\w+", text.casefold())),
    )
    denominator = math.sqrt(sum(v * v for v in a.values()) * sum(v * v for v in b.values()))
    return sum(v * b[k] for k, v in a.items()) / denominator if denominator else 0.0


def rank_memories(memories, query, now, profile):
    """Newer access has greater recency; empty/constant inputs are well defined."""
    ranked = []
    for memory in memories:
        if not memory["created_seconds"] <= now < memory["expires_seconds"]:
            continue
        age = max(0, now - memory["last_accessed_seconds"])
        recency = math.exp2(-age / profile.recency_half_life_seconds)
        score = (
            0.5 * recency
            + 3 * relevance(query, memory["text"])
            + 2 * (memory["importance"] - 1) / 9
        )
        ranked.append((score, memory))
    ranked.sort(key=lambda item: (-item[0], -item[1]["created_seconds"], str(item[1]["id"])))
    selected, used = [], 0
    for score, memory in ranked:
        if len(selected) >= profile.selection_limit:
            break
        if used + len(memory["text"]) > profile.context_chars:
            continue
        selected.append(dict(memory, score=score))
        used += len(memory["text"])
    return selected


class ActorMemoryStore:
    def __init__(self, pool, profile=None):
        self.pool = pool
        self.profile = profile or MemoryProfile()
        self.configuration = self.profile.model_dump(mode="json")
        self.configuration_hash = digest(self.configuration)

    @property
    def binding(self):
        return {"protocol": MEMORY_PROTOCOL, "configuration_hash": self.configuration_hash}

    async def enable(self, run_id, *, require_existing=False):
        """Enable only a new elapsed world; old runs are never retrofitted."""
        run_id = UUID(str(run_id))
        async with DirectorClock(self.pool)._locked(run_id) as (conn, binding, clock, run):
            existing = await row(
                conn, "SELECT * FROM actor_memory_profiles WHERE run_id=%s", (run_id,)
            )
            if existing:
                if existing["configuration_hash"] != self.configuration_hash:
                    raise ClockConflict("The Actor memory profile is frozen.")
                return self.binding
            if require_existing:
                raise DirectorError("The saved Actor memory profile is missing on resume.")
            if clock["now_seconds"] or await row(
                conn, "SELECT 1 FROM director_clock_jobs WHERE run_id=%s", (run_id,)
            ):
                raise DirectorError("Enable Actor memory before the first job in a new world.")
            await conn.execute(
                "INSERT INTO actor_memory_profiles VALUES (%s,%s,%s)",
                (run_id, Jsonb(self.configuration), self.configuration_hash),
            )
        return self.binding

    async def _scope(self, conn, run_id, owner_id):
        profile = await row(
            conn,
            "SELECT p.* FROM actor_memory_profiles p JOIN run_participants r ON r.run_id=p.run_id "
            "WHERE p.run_id=%s AND r.owner_id=%s",
            (run_id, owner_id),
        )
        if not profile or profile["configuration_hash"] != self.configuration_hash:
            raise DirectorError("No matching own Actor memory profile.")
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s)", (lock_key(f"actor-memory:{run_id}:{owner_id}"),)
        )

    async def _remember(
        self,
        conn,
        run_id,
        owner,
        job_id,
        key,
        kind,
        text,
        now,
        ttl,
        evidence,
        provenance,
        importance=5,
        supersedes=None,
    ):
        text = text.strip()[:2000]
        if not text:
            return
        identity = uuid5(run_id, f"actor-memory:{owner}:{key}")
        old = await row(conn, "SELECT text,kind FROM actor_memories WHERE id=%s", (identity,))
        if old:
            if (old["text"], old["kind"]) != (text, kind):
                raise ClockConflict("An Actor memory source changed its content.")
            return
        count = await row(
            conn,
            "SELECT count(*) AS n FROM actor_memories WHERE run_id=%s AND owner_id=%s",
            (run_id, owner),
        )
        if count["n"] >= self.profile.max_memories_per_owner:
            raise DirectorError("The frozen Actor memory record limit is exhausted.")
        await conn.execute(
            "INSERT INTO actor_memories (id,run_id,owner_id,source_key,source_job_id,kind,text,"
            "provenance,evidence,importance,created_seconds,expires_seconds,"
            "last_accessed_seconds,supersedes_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                identity,
                run_id,
                owner,
                key,
                job_id,
                kind,
                text,
                Jsonb(provenance),
                Jsonb(evidence),
                importance,
                now,
                now + ttl,
                now,
                supersedes,
            ),
        )

    async def _synchronize(self, conn, run_id, owner):
        pending = await rows(
            conn,
            "SELECT s.*,j.applied_at_seconds,j.input,j.owner_message_id,o.output AS native_output "
            "FROM actor_memory_snapshots s JOIN director_clock_jobs j ON j.id=s.job_id "
            "JOIN clock_runtime_actor_outputs o ON o.job_id=j.id "
            "WHERE s.run_id=%s AND s.owner_id=%s AND s.applied_seconds IS NULL "
            "AND s.accepted_output IS NOT NULL AND j.status='applied' "
            "ORDER BY j.applied_at_seconds,j.id FOR UPDATE OF s",
            (run_id, owner),
        )
        for saved in pending:
            if saved["accepted_output"] != saved["native_output"]:
                raise DirectorError(
                    "Private cognition differs from the saved accepted Actor output."
                )
            output = ActorOutput.model_validate(saved["accepted_output"])
            validate_cognition(saved["context"], output.cognition)
            update = output.cognition or CognitionUpdate()
            now = saved["applied_at_seconds"]
            for index, thought in enumerate(update.thoughts):
                await self._remember(
                    conn,
                    run_id,
                    owner,
                    saved["job_id"],
                    f"thought:{saved['job_id']}:{index}",
                    "thought",
                    thought.text,
                    now,
                    thought.valid_for_seconds,
                    list(thought.evidence),
                    {"role": "private_inference", "accepted_job_id": str(saved["job_id"])},
                    importance=thought.importance,
                    supersedes=thought.supersedes,
                )
            simulated_at = datetime.fromisoformat(saved["input"]["simulated_at"])
            epoch = simulated_at - timedelta(seconds=saved["input"]["now_seconds"])
            for agenda in update.agendas:
                end = datetime.combine(agenda.day + timedelta(days=1), datetime.min.time(), UTC)
                expires = int((end - epoch).total_seconds())
                if expires <= now:
                    continue  # Accepted proposal remains in the journal, expired on arrival.
                identity = uuid5(saved["job_id"], "actor-agenda:" + agenda.day.isoformat())
                await conn.execute(
                    "INSERT INTO actor_agendas VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        identity,
                        run_id,
                        owner,
                        saved["job_id"],
                        agenda.day,
                        Jsonb([s.model_dump(mode="json") for s in agenda.slots]),
                        now,
                        expires,
                    ),
                )
            await conn.execute(
                "UPDATE actor_memory_snapshots SET applied_seconds=%s WHERE job_id=%s",
                (now, saved["job_id"]),
            )

    async def synchronize(self, run_id, owner_id):
        run_id = UUID(str(run_id))
        async with self.pool.connection() as conn, conn.transaction():
            await self._scope(conn, run_id, owner_id)
            await self._synchronize(conn, run_id, owner_id)

    async def context(self, run_id, owner_id, view):
        run_id, job_id = UUID(str(run_id)), UUID(view.source_event_id)
        if view.owner_id != owner_id or view.actor_memory is not None:
            raise DirectorError("Actor memory accepts only the original own private view.")
        original = view.model_dump(mode="json")
        async with self.pool.connection() as conn, conn.transaction():
            await self._scope(conn, run_id, owner_id)
            job = await row(
                conn,
                "SELECT * FROM director_clock_jobs WHERE id=%s AND run_id=%s "
                "AND owner_id=%s AND role='actor'",
                (job_id, run_id, owner_id),
            )
            if not job or digest(original) != digest(
                ActorView.model_validate(job["input"]["actor_view"]).model_dump(mode="json")
            ):
                raise DirectorError("Actor memory requires the exact saved own Actor input.")
            prior = await row(
                conn, "SELECT * FROM actor_memory_snapshots WHERE job_id=%s", (job_id,)
            )
            if prior:
                if prior["original_view_hash"] != digest(original):
                    raise ClockConflict("Actor input changed during memory replay.")
                if job["status"] == "uncertain" and prior["accepted_output"] is None:
                    raise DirectorError("Unknown Actor output must not be regenerated from memory.")
                return prior
            if job["status"] not in {"pending", "running"}:
                raise DirectorError("Do not retrofit memory onto an already executed Actor job.")
            await self._synchronize(conn, run_id, owner_id)
            now = job["input"]["now_seconds"]
            visible = [(f"fact:{f.id}", f.text, "declared_known_fact") for f in view.known_facts]
            visible += [(f"message:{m.id}", m.text, m.role) for m in view.conversation]
            visible += [(f"observation:{o.id}", o.text, o.kind) for o in view.observations]
            for key, text, role in visible:
                await self._remember(
                    conn,
                    run_id,
                    owner_id,
                    job_id,
                    key,
                    "observation",
                    text,
                    now,
                    self.profile.observation_ttl_seconds,
                    [key],
                    {
                        "role": role,
                        "source_reference": key,
                        "first_observed_seconds": now,
                        "truncated": len(text.strip()) > 2000,
                    },
                )
            memories = await rows(
                conn,
                "SELECT m.* FROM actor_memories m WHERE m.run_id=%s AND m.owner_id=%s "
                "AND m.created_seconds<=%s AND m.expires_seconds>%s "
                "AND NOT EXISTS (SELECT 1 FROM actor_memories n WHERE n.run_id=m.run_id "
                "AND n.owner_id=m.owner_id AND n.supersedes_id=m.id AND n.created_seconds<=%s)",
                (run_id, owner_id, now, now, now),
            )
            query = view.cue + " " + " ".join(m.text for m in view.conversation[-2:])
            selected = rank_memories(memories, query, now, self.profile)
            for memory in selected:
                memory["last_accessed_before_selection_seconds"] = memory["last_accessed_seconds"]
                memory["last_accessed_seconds"] = max(now, memory["last_accessed_seconds"])
                await conn.execute(
                    "UPDATE actor_memories SET last_accessed_seconds="
                    "GREATEST(last_accessed_seconds,%s) WHERE id=%s",
                    (now, memory["id"]),
                )
            today = datetime.fromisoformat(job["input"]["simulated_at"]).date()
            required_agendas = {}
            for event in job["input"]["notifications"]:
                if event["kind"] != "private_cue" or "private_agenda" not in event["payload"]:
                    continue
                agenda = DailyAgenda.model_validate(event["payload"]["private_agenda"])
                if agenda.day not in {today, today + timedelta(days=1)}:
                    raise DirectorError(
                        "A required agenda must be for the current or next UTC day."
                    )
                required_agendas[agenda.day] = {"cue_id": event["event_key"], "agenda": agenda}
            plans = await rows(
                conn,
                "SELECT DISTINCT ON (day) * FROM actor_agendas WHERE run_id=%s AND owner_id=%s "
                "AND day=ANY(%s) AND created_seconds<=%s AND expires_seconds>%s "
                "ORDER BY day,created_seconds DESC,id DESC",
                (run_id, owner_id, [today, today + timedelta(days=1)], now, now),
            )
            minute = (
                datetime.fromisoformat(job["input"]["simulated_at"]).hour * 60
                + datetime.fromisoformat(job["input"]["simulated_at"]).minute
            )
            for plan in plans:
                for slot in plan["slots"]:
                    slot["status"] = (
                        "future_intention"
                        if plan["day"] > today or slot["start_minute"] > minute
                        else "elapsed_unverified"
                        if slot["end_minute"] <= minute
                        else "current_intention"
                    )
            context = jsonable_encoder(
                {
                    **self.binding,
                    "policy": POLICY,
                    "as_of_seconds": now,
                    "as_of_utc": job["input"]["simulated_at"],
                    "memories": selected,
                    "agendas": plans,
                    "planning_days": [today.isoformat(), (today + timedelta(days=1)).isoformat()],
                    "planning_needed": not any(p["day"] == today for p in plans),
                    **(
                        {"required_agendas": list(required_agendas.values())}
                        if required_agendas
                        else {}
                    ),
                    "thought_ttl_limit_seconds": self.profile.thought_ttl_limit_seconds,
                    "allowed_evidence": [
                        "request",
                        *[f"goal:{i}" for i in range(len(view.goals))],
                        *[v[0] for v in visible],
                        *[f"memory:{m['id']}" for m in selected],
                    ],
                }
            )
            await conn.execute(
                "INSERT INTO actor_memory_snapshots "
                "(job_id,run_id,owner_id,original_view_hash,context,context_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (job_id, run_id, owner_id, digest(original), Jsonb(context), digest(context)),
            )
            return await row(
                conn, "SELECT * FROM actor_memory_snapshots WHERE job_id=%s", (job_id,)
            )

    async def accept(self, run_id, owner_id, view, output, output_tokens, diagnostics=()):
        validate_current_cue_dialogue(view, output)
        if output.source_event_id != view.source_event_id:
            raise DirectorError("A private cognition decision names a different Actor input.")
        async with self.pool.connection() as conn, conn.transaction():
            await self._scope(conn, UUID(str(run_id)), owner_id)
            saved = await row(
                conn,
                "SELECT * FROM actor_memory_snapshots WHERE job_id=%s "
                "AND run_id=%s AND owner_id=%s FOR UPDATE",
                (UUID(view.source_event_id), run_id, owner_id),
            )
            if not saved or digest(view.actor_memory) != saved["context_hash"]:
                raise DirectorError("Actor cognition requires its frozen private context.")
            validate_actor_cognition(view, output)
            data = output.model_dump(mode="json")
            if saved["accepted_output"] is not None:
                if saved["accepted_output"] != data:
                    raise ClockConflict("The accepted Actor cognition decision is immutable.")
                return
            await conn.execute(
                "UPDATE actor_memory_snapshots SET accepted_output=%s,output_tokens=%s,"
                "diagnostics=%s WHERE job_id=%s",
                (Jsonb(data), output_tokens, Jsonb(list(diagnostics)), UUID(view.source_event_id)),
            )

    async def export(self, run_id, directory):
        run_id = UUID(str(run_id))
        async with self.pool.connection() as conn:
            members = await rows(
                conn, "SELECT owner_id FROM run_participants WHERE run_id=%s", (run_id,)
            )
        for member in members:
            await self.synchronize(run_id, member["owner_id"])
        async with self.pool.connection() as conn:
            tables = {
                name: await rows(conn, f"SELECT * FROM {name} WHERE run_id=%s", (run_id,))
                for name in [
                    "actor_memory_profiles",
                    "actor_memories",
                    "actor_memory_snapshots",
                    "actor_agendas",
                ]
            }
        result = {
            "protocol": MEMORY_PROTOCOL,
            "run_id": str(run_id),
            "tables": tables,
            "scope": "Private Actor observations, inferences and intentions; no world authority.",
        }
        path = Path(directory) / "actor-memory.json"
        save_private(path, result)
        return {
            "path": str(path),
            "memories": len(tables["actor_memories"]),
            "snapshots": len(tables["actor_memory_snapshots"]),
            "agendas": len(tables["actor_agendas"]),
        }


class RememberingActor:
    """Wrap the same Actor/model/reviewer; use one existing generation response."""

    def __init__(self, store, run_id, owner_id, delegate):
        self.store, self.run_id, self.owner_id, self.delegate = (
            store,
            UUID(str(run_id)),
            owner_id,
            delegate,
        )
        self.cognition_binding = store.binding

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def respond(self, view):
        saved = await self.store.context(self.run_id, self.owner_id, view)
        if saved["accepted_output"] is not None:
            # Carry already-generated tokens into native elapsed accounting,
            # without generating or reserving another provider request.
            self.usage.output_tokens += saved["output_tokens"]
            self.delegate.last_attempts = saved["diagnostics"]
            return ActorOutput.model_validate(saved["accepted_output"])
        enriched = view.model_copy(update={"actor_memory": saved["context"]})
        before = self.usage.output_tokens
        candidate = await self.delegate.respond(enriched)
        output = ActorOutput.model_validate(candidate.model_dump(mode="json"))
        await self.store.accept(
            self.run_id,
            self.owner_id,
            enriched,
            output,
            self.usage.output_tokens - before,
            self.delegate.last_attempts,
        )
        return output
