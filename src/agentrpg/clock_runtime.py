"""Opt-in Director conductor for real Actor and Deep Agents node boundaries."""

import json
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from agentrpg.activation import ActivationEnvelope
from agentrpg.actor import ActorOutput, ActorView, validate_current_cue_dialogue
from agentrpg.budget import budget_context
from agentrpg.clock_graph import ClockGraph
from agentrpg.clock_transfers import TimedActorOutput
from agentrpg.confirmation_schema import ConfirmationAction
from agentrpg.director import DirectorError, emit, row, rows
from agentrpg.director_clock import DirectorClock, TaskOutput, lock_key
from agentrpg.mail_schema import MailRequest
from agentrpg.simulation_clock import ClockConflict, ClockContract, Schedule, Sleep, Work, digest
from agentrpg.transfer_task_schema import TransferTaskRequest
from agentrpg.transfer_tasks import TransferTaskService

INTERRUPTS = (
    "owner_message",
    "assistant_reply",
    "inbox_received",
    "transfer_completed",
    "transfer_rejected",
    "world_change",
    "board_posted",
    "private_cue",
)
PAIRED_RELEASE = "paired_role_complete"


def causal_priority(clock):
    priority = (
        max(1, clock["last_event_priority"] + 1)
        if clock["last_event_second"] == clock["now_seconds"]
        else 1
    )
    if priority > 100:
        raise DirectorError("The declared same-second causal priority bound is exhausted.")
    return priority


async def release_paired_role(conn, pool, run_id, owner_id, role, source_id):
    if not await row(conn, "SELECT run_id FROM clock_runtime_profiles WHERE run_id=%s", (run_id,)):
        return
    clock = await row(
        conn, "SELECT * FROM simulation_clocks WHERE manager_run_id=%s FOR UPDATE", (run_id,)
    )
    unfinished = await row(
        conn,
        "SELECT EXISTS(SELECT 1 FROM director_clock_jobs WHERE run_id=%s "
        "AND owner_id=%s AND role=%s AND status<>'applied') AS present",
        (run_id, owner_id, role),
    )
    if unfinished["present"]:
        return
    if role == "agent" and await row(
        conn,
        "SELECT a.id FROM clock_runtime_activations a JOIN "
        "simulation_clock_processes p ON p.clock_id=%s AND "
        "p.process_id=a.process_id WHERE a.run_id=%s AND p.owner_id=%s AND"
        " a.status='active' LIMIT 1",
        (clock["id"], run_id, owner_id),
    ):
        return
    peer = await row(
        conn,
        "SELECT * FROM simulation_clock_processes WHERE clock_id=%s AND "
        "owner_id=%s AND role<>%s AND state='sleeping' AND interrupt_on @>"
        " %s",
        (clock["id"], owner_id, role, Jsonb([PAIRED_RELEASE])),
    )
    if not peer:
        return
    identity = uuid5(UUID(str(source_id)), "paired-release:" + peer["process_id"])
    await DirectorClock(pool).clock.execute(
        clock["id"],
        identity,
        Schedule(
            event_key="pair-" + identity.hex,
            at_seconds=clock["now_seconds"],
            priority=causal_priority(clock),
            kind=PAIRED_RELEASE,
            recipients=(peer["process_id"],),
        ),
        connection=conn,
        manager_run_id=run_id,
    )


class AgentPhaseOutput(TaskOutput):
    continue_ready: bool = False


class RuntimeActorOutput(TimedActorOutput):
    confirmation_actions: tuple[ConfirmationAction, ...] = Field(default=(), max_length=5)
    mail_requests: tuple[MailRequest, ...] = Field(default=(), max_length=5)
    transfer_task_requests: tuple[TransferTaskRequest, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def task_instruction(self):
        if self.transfer_task_requests and not self.text.strip():
            raise ValueError("Actor task changes require an accompanying owner instruction.")
        return self


async def actor_task_context(conn, run_id, owner_id, message_id, request_id, request, job_id):
    job = await row(
        conn,
        "SELECT j.*,c.now_seconds,c.status AS "
        "clock_status,e.at_seconds,e.status AS event_status FROM "
        "director_clock_jobs j JOIN simulation_clocks c ON c.id=j.clock_id"
        " JOIN simulation_clock_events e ON e.clock_id=j.clock_id AND "
        "e.event_key=j.event_key JOIN clock_runtime_profiles p ON "
        "p.run_id=j.run_id WHERE j.id=%s AND j.run_id=%s AND j.owner_id=%s",
        (job_id, run_id, owner_id),
    )
    if (
        not job
        or job["role"] != "actor"
        or job["status"] != "timed"
        or job["clock_status"] != "active"
        or job["event_status"] != "delivered"
        or job["now_seconds"] != job["at_seconds"]
    ):
        raise DirectorError("No current Actor task completion matches this request.")
    message = await row(conn, "SELECT * FROM owner_messages WHERE id=%s", (message_id,))
    if (
        not message
        or message["request_id"] != job["id"]
        or message["run_id"] != UUID(str(run_id))
        or message["owner_id"] != owner_id
        or message["source"] != "owner"
        or message["content"] != job["output"]["text"].strip()
    ):
        raise DirectorError("Elapsed task changes require their actual Actor message.")
    for index, command in enumerate(
        RuntimeActorOutput.model_validate(job["output"]).transfer_task_requests
    ):
        if (
            UUID(str(request_id)) == uuid5(job["id"], f"clock-transfer-task:{index}")
            and request == command
        ):
            return
    raise DirectorError("Actor task differs from its saved decision.")


async def apply_actor_tasks(conn, pool, run, job, output, message):
    for index, command in enumerate(getattr(output, "transfer_task_requests", ())):
        if job["role"] != "actor" or message is None:
            raise DirectorError("Owner tasks require an Actor message.")
        await TransferTaskService(pool).owner_request(
            run["id"],
            job["owner_id"],
            message["id"],
            uuid5(job["id"], f"clock-transfer-task:{index}"),
            command,
            connection=conn,
            clock_job_id=job["id"],
        )


async def actor_mail_context(conn, run_id, owner_id, request_id, payload, job_id):
    job = await row(
        conn,
        "SELECT j.*,c.now_seconds,c.status AS "
        "clock_status,e.at_seconds,e.status AS event_status FROM "
        "director_clock_jobs j JOIN simulation_clocks c ON c.id=j.clock_id"
        " JOIN simulation_clock_events e ON e.clock_id=j.clock_id AND "
        "e.event_key=j.event_key JOIN clock_runtime_profiles p ON "
        "p.run_id=j.run_id WHERE j.id=%s AND j.run_id=%s AND j.owner_id=%s",
        (job_id, run_id, owner_id),
    )
    if (
        not job
        or job["role"] != "actor"
        or job["status"] != "timed"
        or job["clock_status"] != "active"
        or job["event_status"] != "delivered"
        or job["now_seconds"] != job["at_seconds"]
    ):
        raise DirectorError("No current Actor mail completion matches this request.")
    output = RuntimeActorOutput.model_validate(job["output"])
    for index, mail in enumerate(output.mail_requests):
        if UUID(str(request_id)) == uuid5(
            job["id"], f"actor-mail:{index}"
        ) and payload == mail.model_dump(mode="json", exclude_none=True):
            return
    raise DirectorError("Actor mail differs from its saved decision.")


async def apply_actor_mail(conn, pool, run, job, output):
    if job["role"] != "actor":
        return
    from agentrpg.inbox import InboxService

    for index, mail in enumerate(getattr(output, "mail_requests", ())):
        message = await InboxService(pool).send(
            run["id"],
            job["owner_id"],
            mail.recipient_owner_id,
            mail.subject,
            mail.body,
            uuid5(job["id"], f"actor-mail:{index}"),
            source="actor",
            in_reply_to=mail.in_reply_to,
            connection=conn,
            clock_job_id=job["id"],
        )
        await notify(
            conn,
            pool,
            run["id"],
            mail.recipient_owner_id,
            "inbox_received",
            message["id"],
            {"message_id": str(message["id"])},
        )


async def release_tools(conn, clock, event):
    batch = await row(
        conn,
        "SELECT * FROM clock_runtime_tools WHERE clock_id=%s AND event_key=%s",
        (clock["id"], event["event_key"]),
    )
    if not batch:
        return False
    await conn.execute(
        "UPDATE clock_runtime_tools SET status='released' WHERE id=%s AND status='timed'",
        (batch["id"],),
    )
    return True


async def lock_profile(conn, run_id):
    """Use the same Clock -> run lock order as delivery, before native world writes."""
    return await row(
        conn,
        "SELECT c.* FROM simulation_clocks c JOIN clock_runtime_profiles p"
        " ON p.run_id=c.manager_run_id WHERE p.run_id=%s FOR UPDATE OF c",
        (run_id,),
    )


async def tool_context(conn, run_id, owner_id, name, arguments, request_id):
    batches = await rows(
        conn,
        """SELECT t.*,c.now_seconds,c.status AS clock_status,s.state
        FROM clock_runtime_tools t JOIN simulation_clocks c ON c.id=t.clock_id
        JOIN simulation_clock_processes s ON s.clock_id=t.clock_id AND s.process_id=t.process_id
        WHERE t.run_id=%s AND s.owner_id=%s AND s.role='agent'
          AND t.status IN ('released','done') AND t.due_seconds=c.now_seconds""",
        (run_id, owner_id),
    )
    for batch in batches:
        if batch["clock_status"] != "active" or batch["state"] != "ready":
            continue
        for call in batch["calls"]:
            if (
                call["name"] == name
                and call["args"] == arguments
                and uuid5(batch["namespace"], call["id"]) == UUID(str(request_id))
            ):
                return batch
    raise DirectorError("No released elapsed tool call matches this exact write.")


async def notify(conn, pool, run_id, owner_id, kind, identity, payload):
    binding = await row(conn, "SELECT * FROM director_clock_bindings WHERE run_id=%s", (run_id,))
    clock = await row(conn, "SELECT * FROM simulation_clocks WHERE id=%s", (binding["clock_id"],))
    role = "actor" if kind == "assistant_reply" else "agent"
    lane = await row(
        conn,
        "SELECT process_id FROM simulation_clock_processes WHERE "
        "clock_id=%s AND owner_id=%s AND role=%s",
        (binding["clock_id"], owner_id, role),
    )
    await DirectorClock(pool).clock.execute(
        binding["clock_id"],
        uuid5(UUID(str(identity)), "elapsed-notice:" + kind),
        Schedule(
            event_key="notice-" + uuid5(UUID(str(identity)), kind).hex,
            at_seconds=clock["now_seconds"],
            priority=causal_priority(clock),
            kind=kind,
            recipients=(lane["process_id"],),
            payload=payload,
        ),
        connection=conn,
        manager_run_id=run_id,
    )


class ClockRuntime:
    """Single resumable conductor; provider calls run outside DB transactions.

    Model adapters remain caller-supplied. The frozen configuration contains
    identifiers/hashes, never API keys. A bounded invocation may stop and resume
    without creating a new activation or rewriting a stored model response.
    """

    def __init__(self, pool, *, actors, agents):
        self.pool, self.actors, self.agents = pool, actors, agents
        self.director = DirectorClock(pool)

    def bindings(self):
        def model_id(model):
            return (
                getattr(model, "model_name", None)
                or getattr(model, "model", None)
                or type(model).__name__
            )

        return {
            "agents": {
                o: {
                    "model": model_id(a.model),
                    "instructions_hash": a._instructions_hash,
                    "agent_id": str(a.agent_id),
                    "session_id": str(a.session_id),
                    "max_model_calls": a.max_model_calls,
                }
                for o, a in sorted(self.agents.items())
            },
            "actors": {
                o: {
                    "model": model_id(getattr(a, "model", a)),
                    "reviewer": model_id(getattr(a, "reviewer", None)),
                    "prompt_hash": digest(getattr(a, "prompt", "supplied")),
                    "review_prompt_hash": digest(getattr(a, "review_prompt", "supplied")),
                    **(
                        {"actor_memory": a.cognition_binding}
                        if getattr(a, "cognition_binding", None) is not None
                        else {}
                    ),
                    "describe_output_delivery": getattr(a, "describe_output_delivery", False),
                    "require_live_transfer_task_binding": getattr(
                        a, "require_live_transfer_task_binding", False
                    ),
                    **(
                        {"require_current_cue_review": True}
                        if getattr(a, "require_current_cue_review", False)
                        else {}
                    ),
                    "gates": {
                        name: getattr(a, name, None)
                        for name in (
                            "max_repairs",
                            "require_mail_intent_review",
                            "require_transfer_task_intent_review",
                            "require_transfer_authority_review",
                            "require_transfer_task_coverage_review",
                        )
                    },
                }
                for o, a in sorted(self.actors.items())
            },
        }

    async def enable(self, run_id, configuration):
        run_id = UUID(str(run_id))
        configuration = {
            "operator_configuration": configuration,
            "bindings": self.bindings(),
            "version": "clock-runtime-v1",
            "tool_time_policy": "one_frozen_parallel_graph_batch_then_native_transfer_duration",
            "context_policy": "pause_before_untimed_compaction",
            "paired_dialogue_policy": "complete_current_role_turn_before_paired_reply",
        }
        async with self.director._locked(run_id) as (conn, binding, clock, run):
            prior = await row(
                conn, "SELECT * FROM clock_runtime_profiles WHERE run_id=%s", (run_id,)
            )
            if prior:
                if prior["configuration_hash"] != digest(configuration):
                    raise ClockConflict("The elapsed model/runtime configuration is frozen.")
                return
            spec = ClockContract.model_validate(clock["contract"])
            owners = {p.owner_id for p in spec.processes}
            if set(self.actors) != owners or set(self.agents) != owners:
                raise DirectorError("Supply exactly one Actor and personal Agent for each owner.")
            if "graph_tools" not in spec.tool_seconds:
                raise DirectorError("Freeze a positive graph-tools batch duration.")
            if not await row(
                conn, "SELECT run_id FROM clock_transfer_profiles WHERE run_id=%s", (run_id,)
            ):
                raise DirectorError("Enable native timed transfers before the model conductor.")
            if clock["now_seconds"] != 0 or await row(
                conn, "SELECT id FROM director_clock_jobs WHERE run_id=%s LIMIT 1", (run_id,)
            ):
                raise DirectorError("Enable the model conductor before the first dispatch.")
            scenario = run["scenario"]
            if (
                scenario["world"]["assets"]
                or scenario["resources"]
                or any(
                    b["trigger"]["kind"] != "run_started" or b["mail"] or b["resource_effects"]
                    for b in scenario["beats"]
                )
            ):
                raise DirectorError(
                    "This elapsed profile needs board/mail/wallet scenes and an "
                    "explicit elapsed recurrence plan."
                )
            for owner, agent in self.agents.items():
                participant = await row(
                    conn,
                    "SELECT * FROM run_participants WHERE run_id=%s AND owner_id=%s",
                    (run_id, owner),
                )
                if (
                    agent.agent_id != participant["agent_id"]
                    or agent.session_id != participant["session_id"]
                    or not agent.environment
                    or agent.environment.run_id != str(run_id)
                    or not agent.environment.timed_transfers
                ):
                    raise DirectorError(
                        "Each personal Agent must retain its own scoped timed session."
                    )
            task_modes = {a.environment.track_transfer_tasks for a in self.agents.values()}
            if len(task_modes) != 1:
                raise DirectorError(
                    "All paired Agents must share the frozen business-task profile."
                )
            if True in task_modes:
                if any(not a.guard_transfer_task_yield for a in self.agents.values()):
                    raise DirectorError("Tracked elapsed work requires the saved task-yield guard.")
                await TransferTaskService(self.pool).enable(
                    run_id, connection=conn, clock_runtime=True
                )
            elif await TransferTaskService.enabled(conn, run_id):
                raise DirectorError("Agent task tools must match the native tracking profile.")
            await conn.execute(
                "INSERT INTO clock_runtime_profiles VALUES (%s,%s,%s)",
                (run_id, Jsonb(configuration), digest(configuration)),
            )
            from agentrpg.confirmations import initialize

            definitions = configuration["operator_configuration"].get("confirmations", [])
            if {a.environment.confirmations for a in self.agents.values()} != {bool(definitions)}:
                raise DirectorError("Confirmation tool bindings must match the frozen contracts.")
            await initialize(conn, run_id, definitions)
            for lane in spec.processes:
                if lane.role == "agent":
                    await self.director.clock.execute(
                        clock["id"],
                        uuid5(run_id, "initial-wait:" + lane.id),
                        Sleep(
                            process_id=lane.id,
                            until_seconds=spec.horizon_seconds + 1,
                            interrupt_on=INTERRUPTS,
                        ),
                        connection=conn,
                        manager_run_id=run_id,
                    )
            initial_mail = await rows(
                conn,
                "SELECT id,recipient_owner_id FROM inbox_messages WHERE run_id=%s ORDER BY id",
                (run_id,),
            )
            for mail in initial_mail:
                await notify(
                    conn,
                    self.pool,
                    run_id,
                    mail["recipient_owner_id"],
                    "inbox_received",
                    mail["id"],
                    {"message_id": str(mail["id"])},
                )

    async def _job(self, run_id, process_id, *, consume=True):
        async with self.pool.connection() as conn:
            active = await row(
                conn,
                "SELECT * FROM director_clock_jobs WHERE run_id=%s AND "
                "process_id=%s AND status<>'applied'",
                (run_id, process_id),
            )
            count = await row(
                conn,
                "SELECT count(*) AS n FROM director_clock_jobs WHERE run_id=%s AND process_id=%s",
                (run_id, process_id),
            )
        if active:
            return self.director._public_job(active)
        async with self.pool.connection() as conn:
            run = await row(
                conn,
                "SELECT r.scenario,p.role FROM environment_runs r JOIN "
                "director_clock_bindings b ON b.run_id=r.id JOIN "
                "simulation_clock_processes p ON p.clock_id=b.clock_id WHERE "
                "r.id=%s AND p.process_id=%s",
                (run_id, process_id),
            )
        if run["role"] == "actor":
            if count["n"] >= run["scenario"]["limits"]["max_actor_turns_per_owner"]:
                raise DirectorError("The frozen Actor turn limit is exhausted.")
        return await self.director.prepare(
            run_id,
            process_id,
            uuid5(run_id, f"runtime:{process_id}:{count['n']}"),
            consume_notifications=consume,
        )

    async def _dispatch(self, run_id, job, executor, recovery):
        if job["status"] == "running":
            job = await self.director.recover(run_id, job["id"])
        if job["status"] == "uncertain":
            saved = await recovery(job)
            if saved is None:
                raise DirectorError(
                    "Elapsed model output is uncertain; automatic regeneration is blocked."
                )
            await self.director.reconcile(
                run_id, job["id"], saved, "clock-runtime-durable-response:" + job["id"]
            )
            return await self.director.arm(run_id, job["id"])
        if job["status"] == "saved":
            return await self.director.arm(run_id, job["id"])
        return await self.director.dispatch(run_id, job["id"], executor)

    async def _actor(self, run_id, lane, horizon):
        job = await self._job(run_id, lane["process_id"])
        actor = self.actors[lane["owner_id"]]

        async def execute(packet):
            before = actor.usage.output_tokens
            try:
                with budget_context(run_id=run_id, owner_id=lane["owner_id"]):
                    candidate = await actor.respond(ActorView.model_validate(packet["actor_view"]))
                    output = ActorOutput.model_validate(
                        candidate.model_dump(mode="json")
                        if isinstance(candidate, ActorOutput)
                        else candidate
                    )
                    validate_current_cue_dialogue(
                        ActorView.model_validate(packet["actor_view"]), output
                    )
                    from agentrpg.confirmations import validate_actions

                    validate_actions(ActorView.model_validate(packet["actor_view"]), output)
            except Exception as error:
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO clock_runtime_actor_outputs VALUES (%s,%s,NULL,%s)",
                        (
                            UUID(packet["job_id"]),
                            Jsonb(
                                {
                                    "failure_kind": type(error).__name__,
                                    "failed_output_tokens": actor.usage.output_tokens - before,
                                }
                            ),
                            Jsonb(getattr(actor, "last_attempts", [])),
                        ),
                    )
                raise
            async with self.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO clock_runtime_actor_outputs VALUES (%s,%s,NULL,%s)",
                    (
                        UUID(packet["job_id"]),
                        Jsonb(output.model_dump(mode="json")),
                        Jsonb(getattr(actor, "last_attempts", [])),
                    ),
                )
            if (
                output.kind not in {"reply", "ask_question", "wait"}
                or output.resource_requests
                or output.commerce_consent
                or output.shopping_task_requests
                or output.authorized_amount is not None
            ):
                raise DirectorError(
                    "This elapsed transfer profile cannot commit that Actor effect."
                )
            async with self.pool.connection() as conn:
                rejected = await row(
                    conn,
                    "SELECT COALESCE(sum(carried_output_tokens),0) AS n "
                    "FROM clock_runtime_actor_rejections WHERE job_id=%s",
                    (UUID(packet["job_id"]),),
                )
            timed = RuntimeActorOutput(
                text="" if output.kind == "wait" else output.text,
                output_tokens=actor.usage.output_tokens - before + rejected["n"],
                until_seconds=horizon + 86400,
                interrupt_on=("assistant_reply", "private_cue"),
                transfer_consent=output.transfer_consent,
                mail_requests=output.mail_requests,
                transfer_task_requests=output.transfer_task_requests,
                confirmation_actions=output.confirmation_actions,
            )
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE clock_runtime_actor_outputs SET timed_output=%s WHERE job_id=%s",
                    (Jsonb(timed.model_dump(mode="json")), UUID(packet["job_id"])),
                )
            return timed

        async def recovery(job):
            async with self.pool.connection() as conn:
                saved = await row(
                    conn,
                    "SELECT timed_output FROM clock_runtime_actor_outputs WHERE job_id=%s",
                    (UUID(job["id"]),),
                )
                return saved["timed_output"] if saved else None

        await self._dispatch(run_id, job, execute, recovery)

    async def _activation(self, run_id, lane):
        async with self.pool.connection() as conn:
            active = await row(
                conn,
                "SELECT * FROM clock_runtime_activations WHERE run_id=%s AND "
                "process_id=%s AND status='active'",
                (run_id, lane["process_id"]),
            )
        if active:
            return active
        job = await self._job(run_id, lane["process_id"])
        packet = job["input"]
        async with self.pool.connection() as conn, conn.transaction():
            count = await row(
                conn,
                "SELECT count(*) AS n FROM clock_runtime_activations WHERE "
                "run_id=%s AND process_id=%s",
                (run_id, lane["process_id"]),
            )
            run = await row(conn, "SELECT scenario FROM environment_runs WHERE id=%s", (run_id,))
            if count["n"] >= run["scenario"]["limits"]["max_agent_activations_per_owner"]:
                raise DirectorError("The frozen Agent activation limit is exhausted.")
            messages = await rows(
                conn,
                "SELECT m.* FROM owner_messages m WHERE m.run_id=%s AND "
                "m.owner_id=%s AND m.source='owner' AND NOT EXISTS(SELECT 1 FROM "
                "clock_runtime_owner_deliveries d WHERE d.message_id=m.id) ORDER "
                "BY m.created_at,m.id LIMIT 51",
                (run_id, lane["owner_id"]),
            )
            if len(messages) > 50:
                raise DirectorError("Owner activation exceeds the exact input bound.")
            events = packet["notifications"]
            ids = [uuid5(UUID(packet["clock_id"]), e["event_key"]) for e in events]
            if len(ids) >= 100:
                raise DirectorError(
                    "Reserve one activation reference for the frozen Clock context."
                )
            time_id = uuid5(UUID(job["id"]), "clock-context")
            message_times = {m["id"]: m.get("simulated_seconds") for m in packet["conversation"]}
            envelope = ActivationEnvelope(
                run_id=run_id,
                participant_id=packet["participant_id"],
                activation_id=job["id"],
                window_id=0,
                source_event_ids=tuple([*ids, time_id]),
                owner_messages=tuple(
                    {
                        "message_id": m["id"],
                        "content": (
                            f"[t={message_times[str(m['id'])]}s] "
                            if message_times.get(str(m["id"])) is not None
                            else ""
                        )
                        + m["content"],
                    }
                    for m in messages
                ),
                world_observations=tuple(
                    [
                        {
                            "event_id": identity,
                            "kind": e["kind"],
                            "content": json.dumps(
                                {"simulated_seconds": e["at_seconds"], **e["payload"]},
                                sort_keys=True,
                            ),
                        }
                        for identity, e in zip(ids, events, strict=True)
                        if e["kind"] != "owner_message"
                    ]
                    + [
                        {
                            "event_id": time_id,
                            "kind": "clock_time",
                            "content": json.dumps(
                                {
                                    "simulated_seconds": packet["now_seconds"],
                                    "simulated_at": packet["simulated_at"],
                                },
                                sort_keys=True,
                            ),
                        }
                    ]
                ),
            )
            await conn.execute(
                "INSERT INTO "
                "clock_runtime_activations(id,run_id,process_id,ordinal,envelope) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    envelope.activation_id,
                    run_id,
                    lane["process_id"],
                    count["n"],
                    Jsonb(envelope.model_dump(mode="json")),
                ),
            )
            for message in messages:
                await conn.execute(
                    "INSERT INTO clock_runtime_owner_deliveries VALUES (%s,%s)",
                    (message["id"], envelope.activation_id),
                )
            return await row(
                conn,
                "SELECT * FROM clock_runtime_activations WHERE id=%s",
                (envelope.activation_id,),
            )

    async def _agent(self, run_id, lane, horizon):
        active = await self._activation(run_id, lane)
        envelope = ActivationEnvelope.model_validate(active["envelope"])
        graph = ClockGraph(self.agents[lane["owner_id"]])
        async with self.pool.connection() as conn:
            tool_saved = await row(
                conn,
                "SELECT t.id,s.result FROM clock_runtime_tools t JOIN "
                "clock_graph_steps s ON s.step_id=t.id AND s.session_key=%s WHERE "
                "t.activation_id=%s AND t.status='released' AND s.status='saved'",
                (graph.agent.session_key, active["id"]),
            )
            if tool_saved:
                await conn.execute(
                    "UPDATE clock_runtime_tools SET status='done' WHERE id=%s", (tool_saved["id"],)
                )
                result = tool_saved["result"]
                if result["phase"] == "done" and result["result"]["pending_ids"]:
                    await conn.execute(
                        "UPDATE clock_runtime_activations SET status='done',result=%s WHERE id=%s",
                        (Jsonb(result["result"]), active["id"]),
                    )
                    await release_paired_role(
                        conn, self.pool, run_id, lane["owner_id"], "agent", active["id"]
                    )
                    return
            unfinished = await row(
                conn,
                "SELECT j.* FROM director_clock_jobs j JOIN clock_graph_steps s ON"
                " s.step_id=j.id AND s.session_key=%s WHERE j.run_id=%s AND "
                "j.process_id=%s AND j.status<>'applied'",
                (graph.agent.session_key, run_id, lane["process_id"]),
            )
        if unfinished:

            async def restore(packet):
                saved = await graph.advance(envelope, UUID(packet["job_id"]), "model")
                return AgentPhaseOutput(
                    output_tokens=saved["output_tokens"],
                    until_seconds=horizon + 86400,
                    continue_ready=True,
                )

            await self._dispatch(
                run_id,
                self.director._public_job(unfinished),
                restore,
                lambda j: restore(j["input"]),
            )
            return
        boundary = await graph.open(envelope)
        if boundary["phase"] == "tools":
            await self._tools(run_id, lane, active, graph, envelope, boundary)
            return
        job = await self._job(run_id, lane["process_id"], consume=False)
        if boundary["phase"] == "done":
            result = boundary["result"]
            async with self.pool.connection() as conn:
                batches = await rows(
                    conn,
                    "SELECT namespace,calls FROM clock_runtime_tools WHERE activation_id=%s",
                    (active["id"],),
                )
                ids = [
                    uuid5(b["namespace"], c["id"])
                    for b in batches
                    for c in b["calls"]
                    if c["name"] == "send_owner_message"
                ]
                sent = await row(
                    conn,
                    "SELECT EXISTS(SELECT 1 FROM owner_messages WHERE run_id=%s AND "
                    "owner_id=%s AND source='agent' AND request_id=ANY(%s)) AS present",
                    (run_id, lane["owner_id"], ids),
                )

            async def finished(packet):
                return AgentPhaseOutput(
                    text="" if sent["present"] else result["text"],
                    output_tokens=0,
                    until_seconds=horizon + 86400,
                    interrupt_on=INTERRUPTS,
                )

            await self._dispatch(run_id, job, finished, lambda j: finished(j["input"]))
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE clock_runtime_activations SET status='done',result=%s WHERE id=%s",
                    (Jsonb(result), active["id"]),
                )
            return

        async def execute(packet):
            with budget_context(run_id=run_id, owner_id=lane["owner_id"]):
                result = await graph.advance(envelope, UUID(packet["job_id"]), "model")
            return AgentPhaseOutput(
                output_tokens=result["output_tokens"],
                until_seconds=horizon + 86400,
                continue_ready=True,
            )

        async def recovery(job):
            # Reconcile only a durable AI checkpoint; never regenerate an unknown response.
            async with self.pool.connection() as conn:
                saved = await row(
                    conn,
                    "SELECT result FROM clock_graph_steps WHERE session_key=%s AND step_id=%s",
                    (graph.agent.session_key, UUID(job["id"])),
                )
            if not saved:
                return None
            result = await graph.advance(envelope, UUID(job["id"]), "model")
            return AgentPhaseOutput(
                output_tokens=result["output_tokens"],
                until_seconds=horizon + 86400,
                continue_ready=True,
            )

        await self._dispatch(run_id, job, execute, recovery)

    async def _tools(self, run_id, lane, active, graph, envelope, boundary):
        batch_id = uuid5(active["id"], "tools:" + boundary["checkpoint_id"])
        async with self.director._locked(run_id) as (conn, binding, clock, run):
            batch = await row(conn, "SELECT * FROM clock_runtime_tools WHERE id=%s", (batch_id,))
            if not batch:
                available = graph.agent.environment.tools

                calls = []
                for call in boundary["tool_calls"]:
                    args = call["args"]
                    if call["name"] in available:
                        args = (
                            available[call["name"]][0]
                            .model_validate(args)
                            .model_dump(mode="json", exclude_none=True)
                        )
                    calls.append({**call, "args": args})
                timed = await self.director.clock.execute(
                    clock["id"],
                    batch_id,
                    Work(process_id=lane["process_id"], kind="tool", tool="graph_tools"),
                    connection=conn,
                    manager_run_id=run_id,
                )
                duration = clock["contract"]["tool_seconds"]["graph_tools"]
                await conn.execute(
                    "INSERT INTO clock_runtime_tools VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'timed')",
                    (
                        batch_id,
                        active["id"],
                        run_id,
                        clock["id"],
                        lane["process_id"],
                        boundary["checkpoint_id"],
                        Jsonb(calls),
                        uuid5(graph.agent._session_namespace, str(active["id"])),
                        timed["event_key"],
                        clock["now_seconds"] + duration,
                    ),
                )
                return
            if batch["status"] == "timed":
                return
        result = await graph.advance(envelope, batch_id, "tools")
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE clock_runtime_tools SET status='done' WHERE id=%s", (batch_id,)
            )
        if result["phase"] == "done" and result["result"]["pending_ids"]:
            # The transfer owns the busy lane until its native completion. Do not
            # replace its work with a sleep or consume the coming receipt early.
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE clock_runtime_activations SET status='done',result=%s WHERE id=%s",
                    (Jsonb(result["result"]), active["id"]),
                )
                await release_paired_role(
                    conn, self.pool, run_id, lane["owner_id"], "agent", active["id"]
                )

    async def _defer_for_peer(self, run_id, lane):
        async with self.director._locked(run_id) as (conn, binding, clock, run):
            own_job = await row(
                conn,
                "SELECT id FROM director_clock_jobs WHERE run_id=%s AND "
                "process_id=%s AND status<>'applied'",
                (run_id, lane["process_id"]),
            )
            own_active = await row(
                conn,
                "SELECT id FROM clock_runtime_activations WHERE run_id=%s AND "
                "process_id=%s AND status='active'",
                (run_id, lane["process_id"]),
            )
            if own_job or own_active:
                return False
            peer = await row(
                conn,
                "SELECT * FROM simulation_clock_processes WHERE clock_id=%s AND "
                "owner_id=%s AND role<>%s",
                (clock["id"], lane["owner_id"], lane["role"]),
            )
            busy_job = await row(
                conn,
                "SELECT id FROM director_clock_jobs WHERE run_id=%s AND "
                "process_id=%s AND status<>'applied'",
                (run_id, peer["process_id"]),
            )
            busy_activation = await row(
                conn,
                "SELECT id FROM clock_runtime_activations WHERE run_id=%s AND "
                "process_id=%s AND status='active'",
                (run_id, peer["process_id"]),
            )
            blocked = bool(
                busy_job
                or busy_activation
                or (lane["role"] == "agent" and peer["state"] == "ready")
            )
            if not blocked:
                return False
            await self.director.clock.execute(
                clock["id"],
                uuid5(run_id, f"pair-defer:{lane['process_id']}:{clock['revision']}"),
                Sleep(
                    process_id=lane["process_id"],
                    until_seconds=clock["contract"]["horizon_seconds"] + 86400,
                    interrupt_on=(PAIRED_RELEASE,),
                ),
                connection=conn,
                manager_run_id=run_id,
            )
            return True

    async def advance(self, run_id, *, max_actions=1000):
        if type(max_actions) is not int or not 1 <= max_actions <= 100000:
            raise ValueError("Use a bounded positive action count.")
        run_id = UUID(str(run_id))
        async with self.pool.connection() as lease:
            key = lock_key("clock-runtime:" + str(run_id))
            locked = await row(lease, "SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            await lease.commit()
            if not locked["ok"]:
                raise DirectorError("This elapsed run already has a conductor.")
            try:
                for _ in range(max_actions):
                    async with self.pool.connection() as conn:
                        profile = await row(
                            conn, "SELECT * FROM clock_runtime_profiles WHERE run_id=%s", (run_id,)
                        )
                        clock = await row(
                            conn,
                            "SELECT * FROM simulation_clocks WHERE manager_run_id=%s",
                            (run_id,),
                        )
                        lanes = await rows(
                            conn,
                            "SELECT * FROM simulation_clock_processes WHERE clock_id=%s AND "
                            "state='ready' ORDER BY process_id",
                            (clock["id"],),
                        )
                    if not profile:
                        raise DirectorError("Enable and freeze the elapsed runtime first.")
                    if profile["configuration"]["bindings"] != self.bindings():
                        raise ClockConflict(
                            "The resumed role models, prompts or Agent sessions differ."
                        )
                    if clock["status"] != "active":
                        return await self.director.clock.snapshot(clock["id"])
                    # Deliver queued same-second notifications before opening new input.
                    step = await self.director.step(run_id, uuid4())
                    if step.get("event") or step["reason"] == "horizon_cutoff":
                        continue
                    if not lanes:
                        raise DirectorError("The Clock made no progress without a ready lane.")
                    lane = lanes[0]
                    if await self._defer_for_peer(run_id, lane):
                        continue
                    if lane["role"] == "actor":
                        await self._actor(run_id, lane, clock["contract"]["horizon_seconds"])
                    else:
                        await self._agent(run_id, lane, clock["contract"]["horizon_seconds"])
                return await self.director.clock.snapshot(clock["id"])
            except Exception as error:
                async with self.pool.connection() as conn:
                    await emit(
                        conn,
                        run_id,
                        "elapsed_runtime_stopped",
                        0,
                        {
                            "failure_kind": type(error).__name__,
                            "simulated_seconds": clock["now_seconds"],
                            "actor_diagnostics": {
                                o: getattr(a, "last_attempts", []) for o, a in self.actors.items()
                            },
                        },
                        "elapsed_stop:" + uuid4().hex,
                    )
                raise
            finally:
                await lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
                await lease.commit()
