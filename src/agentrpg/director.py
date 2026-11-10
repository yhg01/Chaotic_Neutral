"""Durable scene conductor inspired by Concordia's scene and observation components.

The frozen scenario decides eligible beats. PostgreSQL records observations and
accepted outputs; language models never run inside a transaction.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from agentrpg.scenario_schema import ScenarioSpec


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class DirectorError(ValueError):
    """A safe domain error for the API and runner."""


async def row(conn, query, args=()):
    return await (await conn.execute(query, args)).fetchone()


async def rows(conn, query, args=()):
    return await (await conn.execute(query, args)).fetchall()


async def require_window_profile(conn, run_id):
    if await row(conn, "SELECT id FROM simulation_clocks WHERE manager_run_id=%s", (run_id,)):
        raise DirectorError("Use the elapsed Director Clock adapter for this run.")


async def emit(conn, run_id, kind, window, payload, operation_key, owner_id=None):
    event_id = uuid5(UUID(str(run_id)), operation_key)
    await conn.execute(
        """INSERT INTO run_events(id,run_id,kind,"window",payload,owner_id,operation_key)
           VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id,operation_key) DO NOTHING""",
        (event_id, run_id, kind, window, Jsonb(payload), owner_id, operation_key),
    )
    return event_id


async def save_message(conn, run_id, owner_id, source, content, request_id, window):
    content = content.strip()
    if not content or len(content) > 12000:
        raise DirectorError("Owner messages must contain 1 to 12000 characters.")
    payload_hash = fingerprint({"content": content})
    message_id = uuid5(UUID(str(run_id)), f"message:{owner_id}:{source}:{request_id}")
    await conn.execute(
        """INSERT INTO owner_messages
           (id,run_id,owner_id,source,content,request_id,payload_hash,"window")
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_id,owner_id,source,request_id) DO NOTHING""",
        (message_id, run_id, owner_id, source, content, request_id, payload_hash, window),
    )
    existing = await row(conn, "SELECT * FROM owner_messages WHERE id=%s", (message_id,))
    if existing["payload_hash"] != payload_hash:
        raise DirectorError("The message request ID has different content.")
    await emit(
        conn,
        run_id,
        "owner_message",
        window,
        {"message_id": str(message_id), "source": source, "content": content},
        f"message:{message_id}",
        owner_id,
    )
    return {k: v for k, v in existing.items() if k != "payload_hash"}


class Director:
    def __init__(self, pool):
        self.pool = pool

    async def create_run(self, spec: ScenarioSpec) -> dict:
        from agentrpg.inbox import InboxService
        from agentrpg.resources import ResourceService
        from agentrpg.world import WorldService

        run_id, cluster_id = uuid4(), uuid4()
        participants = []
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO clusters(id,name) VALUES (%s,%s)",
                    (cluster_id, f"{spec.name[:42]} {run_id}"),
                )
                await conn.execute(
                    """INSERT INTO environment_runs
                       (id,cluster_id,scenario,scenario_hash,seed,max_windows)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        run_id,
                        cluster_id,
                        Jsonb(spec.model_dump(mode="json")),
                        spec.content_hash(),
                        spec.seed,
                        spec.max_windows,
                    ),
                )
                for owner in spec.owners:
                    agent_id, session_id = uuid4(), uuid4()
                    token = secrets.token_urlsafe(32)
                    await conn.execute(
                        "INSERT INTO agents(id,cluster_id,name,token_hash) VALUES (%s,%s,%s,%s)",
                        (
                            agent_id,
                            cluster_id,
                            f"Assistant for {owner.name[:60]}",
                            hashlib.sha256(token.encode()).hexdigest(),
                        ),
                    )
                    await conn.execute(
                        "INSERT INTO run_participants VALUES (%s,%s,%s,%s)",
                        (run_id, owner.id, agent_id, session_id),
                    )
                    participants.append(
                        {
                            "owner_id": owner.id,
                            "agent_id": str(agent_id),
                            "session_id": str(session_id),
                            "token": token,
                        }
                    )
                await WorldService(self.pool).initialize(
                    run_id, spec.model_dump(mode="json"), connection=conn
                )
                await ResourceService(self.pool).initialize(run_id, spec.resources, connection=conn)
                await InboxService(self.pool).initialize(run_id, spec.initial_mail, connection=conn)
                await emit(
                    conn, run_id, "control_audit_enabled", 0, {"revision": 1}, "control_audit:1"
                )
        return {"run_id": str(run_id), "cluster_id": str(cluster_id), "participants": participants}

    async def get_run(self, run_id) -> dict:
        async with self.pool.connection() as conn:
            result = await row(conn, "SELECT * FROM environment_runs WHERE id=%s", (run_id,))
            if result is None:
                raise DirectorError("Run not found.")
            return result

    async def ensure_extensions(self, run_id):
        """Create empty extension state when resuming a run saved before these services existed."""
        from agentrpg.resources import ResourceService

        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
            )
            if run is None:
                raise DirectorError("Run not found.")
            present = await row(
                conn, "SELECT run_id FROM resource_state WHERE run_id=%s", (run_id,)
            )
            if present is None:
                spec = ScenarioSpec.model_validate(run["scenario"])
                await ResourceService(self.pool).initialize(run_id, spec.resources, connection=conn)

    async def participants(self, run_id) -> list[dict]:
        async with self.pool.connection() as conn:
            return await rows(
                conn, "SELECT * FROM run_participants WHERE run_id=%s ORDER BY owner_id", (run_id,)
            )

    async def participant(self, run_id, agent_id) -> dict:
        async with self.pool.connection() as conn:
            value = await row(
                conn,
                "SELECT * FROM run_participants WHERE run_id=%s AND agent_id=%s",
                (run_id, agent_id),
            )
        if value is None:
            raise DirectorError("Run participant not found.")
        return value

    async def control(self, run_id, action: str) -> dict:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                run = await row(
                    conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
                )
                if run is None:
                    raise DirectorError("Run not found.")
                await require_window_profile(conn, run_id)
                status = run["status"]
                result = run["result"]
                if action == "start" and status in {"pending", "running"}:
                    status = "running"
                    result = None
                    await emit(conn, run_id, "run_started", 0, {}, "run_started")
                elif action == "pause" and status in {"running", "paused"}:
                    status = "paused"
                    result = {"reason": "operator_pause"}
                elif action == "resume" and status in {"paused", "running"}:
                    status = "running"
                    result = None
                else:
                    raise DirectorError("This run cannot accept that control action.")
                await conn.execute(
                    "UPDATE environment_runs SET status=%s,result=%s WHERE id=%s",
                    (status, Jsonb(result) if result is not None else None, run_id),
                )
                await emit(
                    conn,
                    run_id,
                    "operator_control",
                    run["current_window"],
                    {"action": action, "previous_status": run["status"]},
                    f"operator_control:{uuid4()}",
                )
        return await self.get_run(run_id)

    async def messages(self, run_id, owner_id):
        async with self.pool.connection() as conn:
            return await rows(
                conn,
                'SELECT id,source,content,"window",created_at FROM owner_messages '
                "WHERE run_id=%s AND owner_id=%s ORDER BY created_at,id",
                (run_id, owner_id),
            )

    async def send_message(self, run_id, owner_id, content, request_id):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                from agentrpg.clock_runtime import lock_profile, notify, tool_context

                elapsed = await lock_profile(conn, run_id)
                run = await row(
                    conn, "SELECT * FROM environment_runs WHERE id=%s FOR SHARE", (run_id,)
                )
                existing = await row(
                    conn,
                    "SELECT id FROM owner_messages WHERE run_id=%s "
                    "AND owner_id=%s AND source='agent' AND request_id=%s",
                    (run_id, owner_id, request_id),
                )
                if elapsed and not existing:
                    from agentrpg.confirmations import guard_freeform

                    await guard_freeform(conn, run_id, owner_id)
                    await tool_context(
                        conn,
                        run_id,
                        owner_id,
                        "send_owner_message",
                        {"content": content},
                        request_id,
                    )
                if run is None or (run["status"] != "running" and not existing and not elapsed):
                    raise DirectorError("The run is not running.")
                message = await save_message(
                    conn, run_id, owner_id, "agent", content, request_id, run["current_window"]
                )
                if elapsed and not existing:
                    await notify(
                        conn,
                        self.pool,
                        run_id,
                        owner_id,
                        "assistant_reply",
                        message["id"],
                        {"message_id": str(message["id"])},
                    )
                return message

    async def fire_beats(self, run_id):
        """Compile scene conditions into private, durable Actor observations."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                run = await row(
                    conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
                )
                if run["status"] != "running":
                    return
                spec = ScenarioSpec.model_validate(run["scenario"])
                window = run["current_window"]
                await emit(
                    conn, run_id, "window_reached", window, {"window": window}, f"window:{window}"
                )
                events = await rows(
                    conn,
                    'SELECT * FROM run_events WHERE run_id=%s AND "window"<=%s '
                    'ORDER BY "window",created_at,id',
                    (run_id, window),
                )
                states = {
                    s["beat_id"]: s
                    for s in await rows(conn, "SELECT * FROM run_beats WHERE run_id=%s", (run_id,))
                }
                # Scenario validation proves this graph is acyclic. Evaluate
                # prerequisites first even when the author listed a child first.
                ordered, remaining, seen = [], list(spec.beats), set()
                while remaining:
                    ready = [beat for beat in remaining if set(beat.requires) <= seen]
                    ordered.extend(ready)
                    seen.update(beat.id for beat in ready)
                    remaining = [beat for beat in remaining if beat.id not in seen]
                for beat in ordered:
                    state = states.get(
                        beat.id, {"use_count": 0, "last_window": None, "source_event_ids": []}
                    )
                    if state["use_count"] >= beat.max_uses:
                        continue
                    if beat.expires_window is not None and window > beat.expires_window:
                        continue
                    if any(states.get(b, {}).get("use_count", 0) == 0 for b in beat.requires):
                        continue
                    if state["last_window"] is not None and (
                        window - state["last_window"] < max(1, beat.cooldown_windows)
                    ):
                        continue
                    candidates = [
                        e
                        for e in events
                        if self._matches(beat.trigger, e)
                        and e["id"] not in state["source_event_ids"]
                        and (e["kind"] != "window_reached" or e["window"] == window)
                    ]
                    if not candidates:
                        continue
                    source = candidates[0]
                    count = state["use_count"] + 1
                    from agentrpg.inbox import InboxService
                    from agentrpg.resources import ResourceService

                    for mail in beat.mail:
                        await InboxService(self.pool).send(
                            run_id,
                            mail.sender_owner_id,
                            mail.recipient_owner_id,
                            mail.subject,
                            mail.body,
                            uuid5(UUID(str(run_id)), f"beat-mail:{beat.id}:{count}:{mail.id}"),
                            source="actor" if mail.sender_owner_id else "director",
                            connection=conn,
                        )
                    for index, effect in enumerate(beat.resource_effects):
                        await ResourceService(self.pool).apply_request(
                            run_id,
                            effect.owner_id,
                            uuid5(UUID(str(run_id)), f"beat-resource:{beat.id}:{count}:{index}"),
                            effect.operation,
                            connection=conn,
                        )
                    for owner_id in beat.recipients:
                        await emit(
                            conn,
                            run_id,
                            "actor_cue",
                            window,
                            {"beat_id": beat.id, "cue": beat.cue, "cause_id": str(source["id"])},
                            f"beat:{beat.id}:{count}:{owner_id}",
                            owner_id,
                        )
                    state = {
                        "use_count": count,
                        "last_window": window,
                        "source_event_ids": [*state["source_event_ids"], source["id"]],
                    }
                    states[beat.id] = state
                    await conn.execute(
                        """INSERT INTO run_beats VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (run_id,beat_id) DO UPDATE SET
                           use_count=EXCLUDED.use_count,last_window=EXCLUDED.last_window,
                           source_event_ids=EXCLUDED.source_event_ids""",
                        (run_id, beat.id, count, window, state["source_event_ids"]),
                    )

    @staticmethod
    def _matches(trigger, event):
        if event["kind"] != trigger.kind:
            return False
        if trigger.window is not None:
            if trigger.every_windows is not None:
                if (
                    event["window"] < trigger.window
                    or (event["window"] - trigger.window) % trigger.every_windows
                ):
                    return False
            elif event["window"] != trigger.window:
                return False
        if trigger.owner_id is not None and event["owner_id"] != trigger.owner_id:
            return False
        payload = event["payload"]
        if trigger.result_status is not None and payload.get("status") != trigger.result_status:
            return False
        source = {"agent": "assistant", "owner": "actor"}.get(
            payload.get("source"), payload.get("source", "world")
        )
        if trigger.source is not None and source != trigger.source:
            return False
        return trigger.asset_id is None or payload.get("asset_id") == trigger.asset_id

    async def trace(self, run_id):
        async with self.pool.connection() as conn:
            return await rows(
                conn,
                'SELECT * FROM run_events WHERE run_id=%s ORDER BY "window",created_at,id',
                (run_id,),
            )
