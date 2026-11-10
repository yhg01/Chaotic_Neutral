"""Opt-in durable Director/elapsed-Clock dispatch, without a provider adapter.

The base profile delivers scoped private chat. The separate opt-in timed-transfer
profile preserves exact Actor authority. Neither profile selects a model/budget.
"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import Field

from agentrpg.actor import build_actor_view
from agentrpg.director import DirectorError, emit, row, rows, save_message
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.simulation_clock import (
    ClockConflict,
    ClockContract,
    Control,
    Frozen,
    Identifier,
    Schedule,
    Seconds,
    SimulationClock,
    Sleep,
    Work,
    digest,
    generation_seconds,
)


class TaskOutput(Frozen):
    """Saved generation output and its next timed wait; never a world receipt."""

    text: str = Field(default="", max_length=12000)
    output_tokens: int = Field(strict=True, ge=0, le=1_000_000)
    until_seconds: Seconds
    interrupt_on: tuple[Identifier, ...] = Field(default=(), max_length=20)


def lock_key(identity):
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)


class DirectorClock:
    def __init__(self, pool):
        self.pool = pool
        self.clock = SimulationClock(pool)

    @asynccontextmanager
    async def _locked(self, run_id, *, connection=None):
        async with self.clock._connection(connection) as conn, conn.transaction():
            binding = await row(
                conn, "SELECT * FROM director_clock_bindings WHERE run_id=%s", (run_id,)
            )
            if binding is None:
                raise DirectorError("Run has no elapsed Clock binding.")
            clock = await row(
                conn,
                "SELECT * FROM simulation_clocks WHERE id=%s FOR UPDATE",
                (binding["clock_id"],),
            )
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
            )
            if run["status"] != "paused" or clock["manager_run_id"] != run["id"]:
                raise DirectorError("The elapsed dispatch profile is not in its expected state.")
            yield conn, binding, clock, run

    async def bind(self, run_id, contract: ClockContract, *, max_jobs=1000):
        run_id = UUID(str(run_id))
        if type(max_jobs) is not int or not 1 <= max_jobs <= 10000:
            raise DirectorError("The frozen job limit must be an integer from 1 to 10000.")
        contract = ClockContract.model_validate(contract.model_dump(mode="json"))
        clock_id = uuid5(run_id, "director-clock-jobs-v1")
        async with self.pool.connection() as conn, conn.transaction():
            # The same run conductor lock prevents binding across an active legacy worker.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(%s)", (lock_key(f"director:{run_id}"),)
            )
            await self.clock.create(contract, clock_id=clock_id, connection=conn)
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
            )
            if run is None:
                raise DirectorError("Run not found.")
            prior = await row(
                conn, "SELECT * FROM director_clock_bindings WHERE run_id=%s", (run_id,)
            )
            if prior:
                if prior["max_jobs"] != max_jobs:
                    raise ClockConflict("Binding already has a different job limit.")
                return str(clock_id)
            if run["status"] != "pending":
                raise DirectorError("Bind only a new pending run; legacy history is not converted.")
            participants = await rows(
                conn, "SELECT * FROM run_participants WHERE run_id=%s", (run_id,)
            )
            expected = {(p["owner_id"], role) for p in participants for role in ("actor", "agent")}
            if {(p.owner_id, p.role) for p in contract.processes} != expected:
                raise DirectorError("Clock lanes must match this run's exact owner pairs.")
            await conn.execute(
                "UPDATE simulation_clocks SET manager_run_id=%s WHERE id=%s", (run_id, clock_id)
            )
            await conn.execute(
                "INSERT INTO director_clock_bindings(run_id,clock_id,version,max_jobs) VALUES "
                "(%s,%s,%s,%s)",
                (run_id, clock_id, "director-clock-jobs-v1", max_jobs),
            )
            # Legacy tools require running. Keep them disabled in this dispatch-only profile.
            await conn.execute(
                "UPDATE environment_runs SET status='paused',result=%s WHERE id=%s",
                (Jsonb({"reason": "elapsed_clock_dispatch_only"}), run_id),
            )
            await emit(
                conn,
                run_id,
                "elapsed_clock_bound",
                0,
                {"clock_id": str(clock_id), "version": "director-clock-jobs-v1"},
                "elapsed_clock_bound",
            )
        return str(clock_id)

    async def _job(self, conn, run_id, job_id):
        job = await row(
            conn, "SELECT * FROM director_clock_jobs WHERE id=%s AND run_id=%s", (job_id, run_id)
        )
        if job is None:
            raise DirectorError("Job not found in this run.")
        return job

    async def _notices(self, conn, clock_id, process_id):
        return await rows(
            conn,
            """SELECT e.event_key,e.at_seconds,e.kind,e.payload
            FROM simulation_clock_events e WHERE e.clock_id=%s AND e.status='delivered'
            AND e.recipients @> %s
            AND e.kind NOT IN ('timer','generation_complete','tool_complete','paired_role_complete')
            AND NOT EXISTS (SELECT 1 FROM director_clock_deliveries d WHERE d.clock_id=e.clock_id
                AND d.event_key=e.event_key AND d.process_id=%s)
            ORDER BY e.delivered_revision,e.event_key LIMIT 101""",
            (clock_id, Jsonb([process_id]), process_id),
        )

    async def prepare(self, run_id, process_id, request_id, *, consume_notifications=True):
        run_id, request_id = UUID(str(run_id)), UUID(str(request_id))
        job_id = uuid5(run_id, f"clock-job:{request_id}")
        async with self._locked(run_id) as (conn, binding, clock, run):
            prior = await row(conn, "SELECT * FROM director_clock_jobs WHERE id=%s", (job_id,))
            if prior:
                if (
                    prior["process_id"] != process_id
                    or prior["input"].get("consume_notifications", True) != consume_notifications
                ):
                    raise ClockConflict("Prepare request already names a different process.")
                return self._public_job(prior)
            if clock["status"] != "active":
                raise DirectorError("Resume the Clock before preparing new work.")
            lane = await row(
                conn,
                "SELECT * FROM simulation_clock_processes WHERE clock_id=%s AND process_id=%s",
                (clock["id"], process_id),
            )
            if lane is None or lane["state"] != "ready":
                raise DirectorError("Prepare requires an own ready lane.")
            active = await row(
                conn,
                "SELECT id FROM director_clock_jobs WHERE clock_id=%s AND process_id=%s AND "
                "status<>'applied'",
                (clock["id"], process_id),
            )
            if active:
                raise DirectorError("This lane already has unfinished work.")
            count = await row(
                conn, "SELECT count(*) AS n FROM director_clock_jobs WHERE run_id=%s", (run_id,)
            )
            if count["n"] >= binding["max_jobs"]:
                raise DirectorError("The frozen dispatch job limit is exhausted.")
            notices = (
                await self._notices(conn, clock["id"], process_id) if consume_notifications else []
            )
            if len(notices) > 100:
                raise DirectorError("Notification input exceeds the profile's 100-event bound.")
            chat = await rows(
                conn,
                "SELECT m.id,m.source,m.content,j.applied_at_seconds AS simulated_seconds "
                "FROM owner_messages m LEFT JOIN director_clock_jobs j ON j.owner_message_id=m.id "
                "WHERE m.run_id=%s AND m.owner_id=%s "
                "ORDER BY m.created_at DESC,m.id DESC LIMIT 50",
                (run_id, lane["owner_id"]),
            )
            chat.reverse()
            participant = await row(
                conn,
                "SELECT * FROM run_participants WHERE run_id=%s AND owner_id=%s",
                (run_id, lane["owner_id"]),
            )
            chat_count = await row(
                conn,
                "SELECT count(*) AS n FROM owner_messages WHERE run_id=%s AND owner_id=%s",
                (run_id, lane["owner_id"]),
            )
            spec = ClockContract.model_validate(clock["contract"])
            packet = {
                "job_id": str(job_id),
                "run_id": str(run_id),
                "clock_id": str(clock["id"]),
                "owner_id": lane["owner_id"],
                "role": lane["role"],
                "process_id": process_id,
                "cluster_id": str(run["cluster_id"]),
                "participant_id": str(participant["agent_id"]),
                "session_id": str(participant["session_id"]),
                "now_seconds": clock["now_seconds"],
                "simulated_at": (spec.epoch + timedelta(seconds=clock["now_seconds"])).isoformat(),
                "notifications": notices,
                "conversation": [dict(m, id=str(m["id"])) for m in chat],
                "conversation_truncated": chat_count["n"] > len(chat),
            }
            if not consume_notifications:
                packet["consume_notifications"] = False
            from agentrpg.clock_transfers import enabled

            if await enabled(conn, run_id):
                from agentrpg.transfers import TransferService

                packet["transfer_account"] = jsonable_encoder(
                    await TransferService(self.pool).account(
                        run_id, lane["owner_id"], connection=conn
                    )
                )
            if lane["role"] == "actor":
                prior_activation = await row(
                    conn,
                    "SELECT a.result FROM clock_runtime_activations a JOIN "
                    "simulation_clock_processes p ON p.clock_id=%s AND "
                    "p.process_id=a.process_id WHERE a.run_id=%s AND p.owner_id=%s "
                    "ORDER BY a.ordinal DESC LIMIT 1",
                    (clock["id"], run_id, lane["owner_id"]),
                )
                current_cues = [
                    str(e["payload"].get("text", e["payload"]))
                    for e in notices
                    if e["kind"] == "private_cue"
                ]
                cue = f"Current date and time: {packet['simulated_at']}. "
                cue += (
                    "Current private Director cue:\n" + "\n\n".join(current_cues)
                    if current_cues
                    else "Respond to your own assistant and current private observations."
                )
                packet["actor_view"] = build_actor_view(
                    ScenarioSpec.model_validate(run["scenario"]),
                    lane["owner_id"],
                    cue,
                    conversation=[
                        {
                            "role": "assistant" if m["source"] == "agent" else "owner",
                            "text": (
                                f"[t={m['simulated_seconds']}s] "
                                if m["simulated_seconds"] is not None
                                else ""
                            )
                            + m["content"],
                            "id": str(m["id"]),
                        }
                        for m in chat
                    ],
                    observations=[
                        {
                            "id": e["event_key"],
                            "kind": e["kind"],
                            "text": str(e["payload"].get("text", e["payload"])),
                            "owner_response_required": e["payload"].get(
                                "owner_response_required", False
                            ),
                        }
                        for e in notices
                        if e["kind"] == "private_cue"
                    ],
                    source_event_id=str(job_id),
                    window=0,
                    assistant_wait_for=(prior_activation["result"] or {}).get("wait_for", ())
                    if prior_activation
                    else (),
                ).model_dump(mode="json")
                if "transfer_account" in packet:
                    packet["actor_view"]["transfers"] = packet["transfer_account"]
                from agentrpg.inbox import InboxService

                packet["actor_view"]["inbox"] = jsonable_encoder(
                    await InboxService(self.pool).actor_view(
                        run_id, lane["owner_id"], connection=conn
                    )
                )
                from agentrpg.confirmations import actor_view as confirmation_view

                packet["actor_view"]["confirmations"] = jsonable_encoder(
                    await confirmation_view(conn, run_id, lane["owner_id"])
                )
            await conn.execute(
                """INSERT INTO director_clock_jobs
                (id,run_id,clock_id,process_id,owner_id,role,request_id,input,input_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    job_id,
                    run_id,
                    clock["id"],
                    process_id,
                    lane["owner_id"],
                    lane["role"],
                    request_id,
                    Jsonb(packet),
                    digest(packet),
                ),
            )
            for event in notices:
                await conn.execute(
                    "INSERT INTO director_clock_deliveries VALUES (%s,%s,%s,%s)",
                    (clock["id"], event["event_key"], process_id, job_id),
                )
            return self._public_job(await self._job(conn, run_id, job_id))

    @staticmethod
    def _public_job(job):
        return {
            k: (str(job[k]) if k.endswith("_id") or k == "id" else job[k])
            for k in (
                "id",
                "run_id",
                "process_id",
                "owner_id",
                "role",
                "status",
                "input",
                "output",
                "attempts",
                "error",
            )
        }

    async def dispatch(self, run_id, job_id, executor):
        """Exactly one dispatch attempt; an unknown outcome is never auto-retried.

        executor is an explicit local callback. No provider/model is installed here.
        The session lock is committed before it runs; no DB transaction is held.
        """
        run_id, job_id = UUID(str(run_id)), UUID(str(job_id))
        key = lock_key(f"clock-job:{job_id}")
        async with self.pool.connection() as lease:
            acquired = await row(lease, "SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            await lease.commit()
            if not acquired["ok"]:
                raise DirectorError("This dispatch still has a live worker.")
            try:
                async with self._locked(run_id, connection=lease) as (conn, binding, clock, run):
                    job = await self._job(conn, run_id, job_id)
                    if job["output"] is not None:
                        return self._public_job(job)
                    if job["status"] != "pending":
                        raise DirectorError(
                            "Dispatch outcome is unknown; reconcile the original attempt "
                            "instead of retrying."
                        )
                    if clock["status"] != "active":
                        raise DirectorError("Resume the Clock before dispatch.")
                    token = uuid4()
                    await conn.execute(
                        "UPDATE director_clock_jobs SET "
                        "status='running',claim_token=%s,attempts=1 WHERE id=%s",
                        (token, job_id),
                    )
                    packet = deepcopy(job["input"])
                try:
                    output = await executor(packet)
                    await self._save(run_id, job_id, token, output, connection=lease)
                except BaseException:

                    async def mark_unknown():
                        async with self._locked(run_id, connection=lease) as (conn, *_):
                            await conn.execute(
                                "UPDATE director_clock_jobs SET "
                                "status='uncertain',error='dispatch_output_not_durably_saved' "
                                "WHERE id=%s AND status='running' AND claim_token=%s",
                                (job_id, token),
                            )

                    await asyncio.shield(mark_unknown())
                    raise
            finally:
                await lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
                await lease.commit()
        return await self.arm(run_id, job_id)

    async def _save(self, run_id, job_id, token, output, *, connection=None):
        async with self._locked(run_id, connection=connection) as (conn, binding, clock, run):
            job = await self._job(conn, run_id, job_id)
            from agentrpg.clock_transfers import decode_output

            output = await decode_output(conn, run_id, job, output)
            data = output.model_dump(mode="json")
            if job["output"] is not None:
                if job["output_hash"] != digest(data):
                    raise ClockConflict("Saved output already has different content.")
                return
            if job["status"] != "running" or job["claim_token"] != token:
                raise DirectorError("The dispatch claim is not current.")
            spec = ClockContract.model_validate(clock["contract"])
            due = job["input"]["now_seconds"] + generation_seconds(
                output.output_tokens, spec.output_tokens_per_minute
            )
            if output.until_seconds <= due:
                raise DirectorError("Next wait must end after generation completion.")
            if job["error"]:
                # Preserve the failure after reconciliation clears the current error field.
                await emit(
                    conn,
                    run_id,
                    "elapsed_clock_dispatch_uncertain",
                    0,
                    {
                        "job_id": str(job_id),
                        "reason": job["error"],
                        "simulated_seconds": job["input"]["now_seconds"],
                    },
                    f"elapsed_uncertain:{job_id}",
                    job["owner_id"],
                )
            await conn.execute(
                "UPDATE director_clock_jobs SET "
                "output=%s,output_hash=%s,status='saved',error=NULL WHERE id=%s",
                (Jsonb(data), digest(data), job_id),
            )

    async def arm(self, run_id, job_id):
        async with self._locked(run_id) as (conn, binding, clock, run):
            job = await self._job(conn, run_id, job_id)
            if job["status"] in {"timed", "applied"}:
                return self._public_job(job)
            if job["status"] != "saved":
                raise DirectorError("A durable saved output is required before timing work.")
            if clock["status"] != "active":
                return self._public_job(job)
            if clock["now_seconds"] != job["input"]["now_seconds"]:
                raise DirectorError("Time advanced past an unresolved generation input.")
            result = await self.clock.execute(
                clock["id"],
                uuid5(job["id"], "generation-duration"),
                Work(
                    process_id=job["process_id"],
                    kind="generation",
                    output_tokens=job["output"]["output_tokens"],
                ),
                connection=conn,
                manager_run_id=run["id"],
            )
            await conn.execute(
                "UPDATE director_clock_jobs SET status='timed',event_key=%s WHERE id=%s",
                (result["event_key"], job["id"]),
            )
            return self._public_job(await self._job(conn, run["id"], job["id"]))

    async def recover(self, run_id, job_id):
        async with self._locked(run_id) as (conn, binding, clock, run):
            job = await self._job(conn, run_id, job_id)
            if job["status"] == "running":
                free = await row(
                    conn,
                    "SELECT pg_try_advisory_xact_lock(%s) AS ok",
                    (lock_key(f"clock-job:{job['id']}"),),
                )
                if free["ok"]:
                    await conn.execute(
                        "UPDATE director_clock_jobs SET "
                        "status='uncertain',error='worker_lost_before_saved_output' WHERE "
                        "id=%s",
                        (job["id"],),
                    )
            return self._public_job(await self._job(conn, run_id, job_id))

    async def reconcile(self, run_id, job_id, output, evidence_reference):
        """Trusted recovery of an existing response, not permission to regenerate it."""
        if (
            not isinstance(evidence_reference, str)
            or not 1 <= len(evidence_reference.strip()) <= 240
        ):
            raise DirectorError("A saved-response evidence reference is required.")
        async with self._locked(run_id) as (conn, binding, clock, run):
            job = await self._job(conn, run_id, job_id)
            from agentrpg.clock_transfers import decode_output

            output = await decode_output(conn, run_id, job, output)
            prior = await row(
                conn, "SELECT * FROM director_clock_recovery WHERE job_id=%s", (job_id,)
            )
            if prior:
                if (
                    prior["output_hash"] != digest(output.model_dump(mode="json"))
                    or prior["evidence_reference"] != evidence_reference
                ):
                    raise ClockConflict("Recovery evidence already differs.")
                return self._public_job(job)
            if job["status"] != "uncertain":
                raise DirectorError("Reconcile only an explicitly uncertain original attempt.")
            # Reuse the original fenced save path inside the same transaction.
            await conn.execute(
                "UPDATE director_clock_jobs SET status='running' WHERE id=%s", (job_id,)
            )
            await self._save(run_id, job_id, job["claim_token"], output, connection=conn)
            await conn.execute(
                "INSERT INTO director_clock_recovery(job_id,evidence_reference,output_hash) "
                "VALUES (%s,%s,%s)",
                (job_id, evidence_reference, digest(output.model_dump(mode="json"))),
            )
            return self._public_job(await self._job(conn, run_id, job_id))

    async def control(self, run_id, request_id, command):
        if isinstance(command, dict):
            from agentrpg.simulation_clock import COMMAND

            command = COMMAND.validate_python(command)
        if not isinstance(command, (Control, Schedule, Sleep)) or (
            isinstance(command, Control) and command.operation == "step"
        ):
            raise DirectorError("Use step for delivery and arm for generation duration.")
        async with self._locked(run_id) as (conn, binding, clock, run):
            if isinstance(command, Sleep):
                active = await row(
                    conn,
                    "SELECT id FROM director_clock_jobs WHERE clock_id=%s AND process_id=%s "
                    "AND status<>'applied'",
                    (clock["id"], command.process_id),
                )
                if active:
                    raise DirectorError("An unresolved job cannot be replaced with a wait.")
            return await self.clock.execute(
                clock["id"], request_id, command, connection=conn, manager_run_id=run["id"]
            )

    async def step(self, run_id, request_id):
        async with self._locked(run_id) as (conn, binding, clock, run):
            result = await self.clock.execute(
                clock["id"],
                request_id,
                Control(operation="step"),
                connection=conn,
                manager_run_id=run["id"],
            )
            event = result.get("event")
            job = None
            transfer = None
            if event and event["kind"] == "generation_complete":
                job = await row(
                    conn,
                    "SELECT * FROM director_clock_jobs WHERE clock_id=%s AND event_key=%s",
                    (clock["id"], event["event_key"]),
                )
                if job is None:
                    raise DirectorError("Generation completion has no durable job.")
                if job["status"] != "applied":
                    await self._apply(conn, run, clock, job, result["now_seconds"])
            if event and event["kind"] == "tool_complete":
                from agentrpg.clock_runtime import release_tools
                from agentrpg.clock_transfers import finish_transfer

                released = await release_tools(conn, clock, event)
                if not released:
                    transfer = await finish_transfer(
                        conn, self.pool, self, run, clock, event, result["now_seconds"]
                    )
            if result["reason"] == "horizon_cutoff":
                await emit(
                    conn,
                    run["id"],
                    "elapsed_clock_horizon",
                    0,
                    {"simulated_seconds": result["now_seconds"], "task_success": None},
                    "elapsed_clock_horizon",
                )
            response = {**result, "applied_job_id": str(job["id"]) if job else None}
            if transfer is not None:
                response["transfer"] = transfer
            return response

    async def _apply(self, conn, run, clock, job, now):
        if job["status"] != "timed":
            raise DirectorError("Only a timed saved result can be applied.")
        from agentrpg.clock_transfers import authorize_job, decode_output

        output = await decode_output(conn, run["id"], job, job["output"])
        from agentrpg.clock_runtime import apply_actor_mail, apply_actor_tasks

        message = None
        if output.text.strip():
            if job["role"] == "agent":
                from agentrpg.confirmations import guard_freeform

                await guard_freeform(conn, run["id"], job["owner_id"])
            message = await save_message(
                conn,
                run["id"],
                job["owner_id"],
                "owner" if job["role"] == "actor" else "agent",
                output.text,
                job["id"],
                0,
            )
            await apply_actor_tasks(conn, self.pool, run, job, output, message)
            from agentrpg.confirmations import apply_actor as apply_confirmation_actions

            await apply_confirmation_actions(conn, run, job, output, message)
            await authorize_job(conn, self.pool, run, job, output, message)
            peer = await row(
                conn,
                "SELECT process_id FROM simulation_clock_processes WHERE clock_id=%s AND "
                "owner_id=%s AND role<>%s",
                (clock["id"], job["owner_id"], job["role"]),
            )
            await self.clock.execute(
                clock["id"],
                uuid5(job["id"], "private-chat-notice"),
                Schedule(
                    event_key=f"message-{job['id'].hex}",
                    at_seconds=now,
                    priority=1,
                    kind="owner_message" if job["role"] == "actor" else "assistant_reply",
                    recipients=(peer["process_id"],),
                    payload={"message_id": str(message["id"])},
                ),
                connection=conn,
                manager_run_id=run["id"],
            )
        await apply_actor_mail(conn, self.pool, run, job, output)
        queued = await row(
            conn,
            """SELECT EXISTS (
            SELECT 1 FROM simulation_clock_events e WHERE e.clock_id=%s AND e.status='delivered'
            AND e.recipients @> %s AND e.kind=ANY(%s)
            AND NOT EXISTS (SELECT 1 FROM director_clock_deliveries d WHERE d.clock_id=e.clock_id
                AND d.event_key=e.event_key AND d.process_id=%s)) AS present""",
            (clock["id"], Jsonb([job["process_id"]]), list(output.interrupt_on), job["process_id"]),
        )
        if not queued["present"] and not getattr(output, "continue_ready", False):
            await self.clock.execute(
                clock["id"],
                uuid5(job["id"], "next-wait"),
                Sleep(
                    process_id=job["process_id"],
                    until_seconds=output.until_seconds,
                    interrupt_on=output.interrupt_on,
                ),
                connection=conn,
                manager_run_id=run["id"],
            )
        await conn.execute(
            "UPDATE director_clock_jobs SET "
            "status='applied',applied_at_seconds=%s,owner_message_id=%s WHERE id=%s",
            (now, message["id"] if message else None, job["id"]),
        )
        await emit(
            conn,
            run["id"],
            "elapsed_clock_job_applied",
            0,
            {
                "job_id": str(job["id"]),
                "simulated_seconds": now,
                "role": job["role"],
                "message_id": str(message["id"]) if message else None,
            },
            f"elapsed_job:{job['id']}",
            job["owner_id"],
        )
        from agentrpg.clock_runtime import release_paired_role

        await release_paired_role(
            conn, self.pool, run["id"], job["owner_id"], job["role"], job["id"]
        )

    async def snapshot(self, run_id):
        async with self._locked(run_id) as (conn, binding, clock, run):
            jobs = await rows(
                conn,
                "SELECT "
                "id,process_id,owner_id,role,status,attempts,event_key,applied_at_seconds "
                "FROM director_clock_jobs WHERE run_id=%s ORDER BY id",
                (run_id,),
            )
            return {
                "run_id": str(run["id"]),
                "clock_id": str(clock["id"]),
                "now_seconds": clock["now_seconds"],
                "status": clock["status"],
                "profile": binding["version"],
                "jobs": [dict(j, id=str(j["id"])) for j in jobs],
                "legacy_world_tools_enabled": False,
                "provider_adapter_installed": False,
                "task_success": None,
            }
