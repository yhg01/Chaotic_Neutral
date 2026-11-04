"""The conductor of clocked runs: think in parallel, act in time order.

Model calls overlap, because each reads only private saved history. Every world effect
waits until it is the lowest event in simulated time, so commit order equals story order.
Handlers run one at a time. Model calls run in tasks with no database transaction open.
docs/EVENT_CLOCK.md holds rules R1 to R16, which this module cites. RunEngine stays the
engine of window runs, and this module shares no mutable state with it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from uuid import UUID, uuid5

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from agentrpg import drama, wallet
from agentrpg.activation import ActivationEnvelope, ClockedActivationResult
from agentrpg.actor import (
    NO_CUE,
    ActorContractError,
    ActorOutput,
    ActorView,
    ModelGenerationError,
    build_actor_view,
    earlier_decisions,
    validate_actor_output,
)
from agentrpg.budget import BudgetError, budget_context
from agentrpg.clock import (
    ACTOR_KINDS,
    ACTOR_OBSERVATION_KINDS,
    AGENT_KINDS,
    CHAT_STREAK_LIMIT,
    EFFECTFUL_TOOLS,
    PEER_WAKE_LIMIT,
    READ_ONLY_FORM_ACTIONS,
    ClockConfig,
    actor_process,
    agent_process,
    cancel,
    classify_actor_event,
    classify_agent_event,
    event_time_s,
    fold_stamps,
    is_peer_event,
    next_daily_wake,
    next_event,
    notice_delay_s,
    price_actor,
    retime,
    schedule,
    strip_nul,
    until_after_yield,
)
from agentrpg.clock_gate import ARRIVAL_TIMEOUT_S
from agentrpg.deep_agent import AgentBudgetError, AgentRunStopped, AgentRuntimeError
from agentrpg.director import Director, DirectorError, emit, fingerprint, row, rows, save_message
from agentrpg.inbox import InboxService
from agentrpg.market import MarketService
from agentrpg.resources import ResourceService
from agentrpg.runtime_failures import classify_failure, failure_detail
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.world import WorldError, WorldService

logger = logging.getLogger(__name__)

ACTOR_VIEW_MESSAGES = 20
EVENT_LIMIT = 80
EVENT_CHARS = 38_000
OWNER_MESSAGE_LIMIT = 50
CUE_CHARS = 8_000
OBSERVATION_CHARS = 8_000
DRAIN_TIMEOUT_S = 180.0
RETRY_CAP_S = 300.0
# The circuit breaker opens after this many failures with nothing working in between, once
# they have lasted this long. One blip can fail many callers at the same moment.
BREAKER_FAILURES = 8
BREAKER_SECONDS = 60.0
# How often a conductor stopped by the database starts again before the process gives up.
DATABASE_RESTARTS = 8
FREEZE_S = 12 * 60 * 60
REASON_REQUESTED = "You asked to look at this again around now."
REASON_ROUTINE = "You usually check in around now."


class _Interrupted(Exception):
    """The run stopped running under a handler. The event stays queued for a resume."""


class _BudgetPause(Exception):
    """A refusal or a long outage. The event stays queued, so no due event is skipped (R14)."""


@dataclass(frozen=True)
class _Run:
    id: UUID
    spec: ScenarioSpec
    config: ClockConfig
    participants: dict
    limit_s: int | None


def _is_budget(error: BaseException) -> bool:
    return isinstance(error, (BudgetError, AgentBudgetError)) or (
        isinstance(error, ModelGenerationError) and error.kind == "budget"
    )


def _is_retryable(error: Exception) -> bool:
    # Generic runtime errors say "retry with the same task ID": a bad or empty model reply.
    # A refused request or a garbled response may go through on a second try, within the limit.
    failure = classify_failure(error)
    return (
        failure.retryable
        or failure.kind == "provider_request_refused"
        or type(error) is AgentRuntimeError
    )


def _cause_key(value: str) -> str:
    return value if len(value) <= 180 else "h:" + hashlib.sha256(value.encode()).hexdigest()


def _clean_output(output: ActorOutput) -> ActorOutput:
    """A model can send NUL. PostgreSQL cannot save it. Words with no text left fail validation."""
    saved = output.model_dump(mode="json")
    cleaned = strip_nul(saved)
    return output if cleaned == saved else ActorOutput.model_validate(cleaned)


def _clean_plan(plan):
    if not isinstance(plan, BaseModel):
        return plan
    saved = plan.model_dump(mode="json")
    cleaned = strip_nul(saved)
    if cleaned == saved:
        return plan
    try:
        return type(plan).model_validate(cleaned)
    except ValueError:
        return plan  # The event handler records what cannot be saved.


def _how_long(seconds: int) -> str:
    """Plain words for a stretch of time, such as "About 3 days"."""
    hours = round(seconds / 3600)
    if hours >= 36:
        return f"About {round(seconds / 86400)} days"
    return "About 1 hour" if hours <= 1 else f"About {hours} hours"


IDLE_ACTION_S = 2 * 60 * 60
MIN_ACTION_MINUTES = 30
# A shorter limit cut long assistant messages, and owners then said "you cut off".
ACTOR_MESSAGE_CHARS = 5000
ACTOR_MAIL_ITEMS = 12
ACTOR_MAIL_CHARS = 1500
ACTOR_RECORD_CHARS = 2000


def _short_inbox(view: dict) -> dict:
    """An owner glances at mail. The assistant reads it in full through its own tools.

    It holds what a mailbox shows: no run fields, and read or not rather than a saved stamp.
    A paging cursor means nothing to a person, and it holds values that differ from run to run.
    """
    for item in view.get("items", []):
        if len(item.get("body") or "") > ACTOR_MAIL_CHARS:
            item.update(body=item["body"][:ACTOR_MAIL_CHARS], body_truncated=True)
        for key in ("run_id", "window", "sender_source"):
            item.pop(key, None)
        item["read"] = item.pop("read_at", None) is not None
    if view.pop("next_cursor", None):
        view["older_mail_not_shown"] = True
    return view


def _short_records(view: dict) -> dict:
    for item in view.get("items", []):
        if len(item.get("content") or "") > ACTOR_RECORD_CHARS:
            item.update(content=item["content"][:ACTOR_RECORD_CHARS], content_truncated=True)
    return view


class ClockEngine:
    """One conductor per run. Supply one Actor and one gated Agent per owner.

    web is accepted for callers that hold a shared site service. Each Agent has its own
    web adapter, and lifecycle checks read the site tables directly.
    """

    def __init__(
        self,
        pool,
        actors: dict,
        agents: dict,
        director_brain=None,
        web=None,
        *,
        retry_base_seconds: float = 1.0,
        retry_cap_seconds: float = RETRY_CAP_S,
        breaker_failures: int = BREAKER_FAILURES,
        breaker_seconds: float = BREAKER_SECONDS,
        arrival_timeout_seconds: float = ARRIVAL_TIMEOUT_S,
        drain_timeout_seconds: float = DRAIN_TIMEOUT_S,
    ) -> None:
        gates = {id(agent.gate): agent.gate for agent in agents.values() if agent.gate is not None}
        if len(gates) != 1 or any(agent.gate is None for agent in agents.values()):
            raise ValueError("Every Agent of a clocked run must share one gate.")
        self.pool = pool
        self.actors = actors
        self.agents = agents
        self.brain = director_brain
        self.web = web
        self.gate = next(iter(gates.values()))
        self.gate.saved_calls = self._saved_calls
        self.director = Director(pool)
        self.world = WorldService(pool)
        self.market = MarketService(pool)
        self.inbox = InboxService(pool)
        self.resources = ResourceService(pool)
        self.transfers = TransferService(pool)
        self.retry_base_seconds = retry_base_seconds
        self.retry_cap_seconds = retry_cap_seconds
        self.breaker_failures = breaker_failures
        self.breaker_seconds = breaker_seconds
        self.database_restarts = DATABASE_RESTARTS
        self.arrival_timeout_seconds = arrival_timeout_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._run_id: UUID | None = None
        self._pause_reason: dict | None = None
        self._fatal: BaseException | None = None
        self._stopping = False
        self._stop_signal = asyncio.Event()
        self._outage_failures = 0
        self._outage_since: float | None = None
        self._successes_seen = 0

    # Conductor ---------------------------------------------------------------------------

    async def advance(self, run_id, max_sim_minutes: int | None = None) -> dict:
        """Run until the story moves max_sim_minutes on, or the run stops running."""
        run_id = UUID(str(run_id))
        key = int.from_bytes(
            hashlib.sha256(f"director:{run_id}".encode()).digest()[:8], "big", signed=True
        )
        async with self.pool.connection() as lease:
            acquired = await row(lease, "SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            await lease.commit()
            if not acquired["ok"]:
                raise DirectorError("This run already has an active conductor.")
            try:
                return await self._conduct(run_id, max_sim_minutes)
            finally:
                await lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
                await lease.commit()

    async def run(self, run_id) -> dict:
        """Start if needed, then conduct until the run stops running."""
        run = await self.director.get_run(run_id)
        if run["status"] == "pending":
            await self.director.control(run_id, "start")
        restarts = 0
        while True:
            try:
                run = await self.director.get_run(run_id)
                if run["status"] != "running":
                    return run
                await self.advance(run_id)
                restarts = 0
            except psycopg.OperationalError:
                # Saved state lets a new conductor continue. The process exits only if the
                # database stays away, so an outer restart loop can take over.
                restarts += 1
                if restarts > self.database_restarts:
                    raise
                logger.warning("The database failed. Starting the conductor again.")
                await asyncio.sleep(self._retry_delay(restarts))

    async def _load(self, run_id, max_sim_minutes) -> _Run:
        run = await self.director.get_run(run_id)
        if run["status"] != "running":
            raise DirectorError("Start or resume the run before advancing it.")
        spec = ScenarioSpec.model_validate(run["scenario"])
        if spec.clock is None:
            raise DirectorError("This run has no clock. Use the window engine.")
        await self.director.ensure_extensions(run_id)
        participants = {p["owner_id"]: p for p in await self.director.participants(run_id)}
        if set(self.actors) != set(participants) or set(self.agents) != set(participants):
            raise DirectorError("Every owner needs one Actor and one assistant.")
        async with self.pool.connection() as conn:
            state = await row(conn, "SELECT * FROM clock_state WHERE run_id=%s", (run_id,))
        if state is None:
            raise DirectorError("This run has no clock state.")
        config = ClockConfig.from_section(spec.clock.model_dump(mode="json"), spec.seed)
        limit = None if max_sim_minutes is None else state["sim_s"] + max_sim_minutes * 60
        return _Run(run_id, spec, config, participants, limit)

    async def _conduct(self, run_id, max_sim_minutes) -> dict:
        ctx = await self._load(run_id, max_sim_minutes)
        self._run_id = run_id
        self._pause_reason = None
        self._fatal = None
        self._stopping = False
        self._stop_signal.clear()
        await self._arm_breaker(ctx)
        try:
            await self._clear_turn(ctx)
            await self._bootstrap(ctx)
            await self._redispatch(ctx)
            await self._loop(ctx)
        finally:
            await self._stop_tasks()
        return await self.director.get_run(run_id)

    async def _arm_breaker(self, ctx: _Run) -> None:
        """After an outage pause, the first failure pauses again and the first success clears it.

        Otherwise every resume during a long outage pays for a whole round of failed attempts.
        """
        self._outage_failures = 0
        self._outage_since = None
        self._successes_seen = self.gate.successes
        async with self.pool.connection() as conn:
            last = await row(
                conn,
                "SELECT payload FROM run_events WHERE run_id=%s AND kind='run_paused' "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (ctx.id,),
            )
        if last is not None and last["payload"].get("reason") == "provider_outage":
            self._outage_failures = max(0, self.breaker_failures - 1)
            self._outage_since = time.monotonic() - self.breaker_seconds

    async def _loop(self, ctx: _Run) -> None:
        gate = self.gate
        dirty = True
        while True:
            gate.clear_changed()
            generation = gate.generation
            if self._fatal is not None:
                raise self._fatal
            if self._pause_reason is not None:
                await self._pause_for_budget(ctx)
                return
            async with self.pool.connection() as conn:
                run = await row(conn, "SELECT status FROM environment_runs WHERE id=%s", (ctx.id,))
            if run["status"] != "running":
                await self._drain()
                return
            if dirty:
                await self._reconcile(ctx)
                dirty = False
            async with self.pool.connection() as conn:
                durable = await next_event(conn, ctx.id)
            if gate.generation != generation:
                # A process reported while the queue was read. Its event may be lower.
                continue
            parked = gate.head()
            use_parked = parked is not None and (
                durable is None
                or parked.order
                < (durable["due_s"], durable["class"], bytes(durable["tie"]), durable["key"])
            )
            if durable is None and parked is None:
                if gate.active():
                    await gate.wait_changed()
                    continue
                await self._queue_empty(ctx)
                return
            due = parked.stamp_s if use_parked else durable["due_s"]
            if ctx.limit_s is not None and due >= ctx.limit_s:
                if gate.blocking():
                    await gate.wait_changed()
                    continue
                return
            if use_parked and not parked.arrived:
                if gate.waited(parked) > self.arrival_timeout_seconds:
                    await self._drop_stale(ctx, parked)
                    continue
                await gate.wait_changed()
                continue
            if not gate.allows(due):
                await gate.wait_changed()
                continue
            try:
                if use_parked:
                    await self._release_tool(ctx, parked)
                else:
                    await self._pop(ctx, durable)
            except _BudgetPause:
                self._pause_reason = self._pause_reason or {
                    "reason": "budget",
                    "component": "director",
                }
                continue
            except _Interrupted:
                continue
            dirty = True

    async def _queue_empty(self, ctx: _Run) -> None:
        """run_end is always queued, so this is a simulator failure and must be visible."""
        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (ctx.id,)
            )
            if run["status"] != "running":
                return
            await conn.execute(
                "UPDATE environment_runs SET status='incomplete',result=%s WHERE id=%s",
                (Jsonb({"reason": "clock_queue_empty"}), ctx.id),
            )
            await emit(
                conn,
                ctx.id,
                "simulator_error",
                run["current_window"],
                {"reason": "clock_queue_empty"},
                "clock_queue_empty",
            )

    async def _bootstrap(self, ctx: _Run) -> None:
        """Save the opening cues at the first moment. Repeating this changes nothing (R15)."""
        async with self.pool.connection() as conn:
            state = await row(conn, "SELECT * FROM clock_state WHERE run_id=%s", (ctx.id,))
        if state["sim_s"] != 0:
            return
        await self._commit_turn(ctx, 0, "director")
        await self.director.fire_beats(ctx.id)
        await self._clear_turn(ctx)

    async def _redispatch(self, ctx: _Run) -> None:
        """Resume unfinished jobs from checkpoints. They block until they report (R5)."""
        async with self.pool.connection() as conn:
            # A call that committed before the stop has a saved result and will not run again.
            await conn.execute(
                """UPDATE clock_actions a SET status='done'
                   WHERE a.run_id=%s AND a.process='agent' AND a.status='active' AND EXISTS (
                       SELECT 1 FROM run_events e WHERE e.run_id=a.run_id
                       AND e.kind='assistant_tool_call' AND e.owner_id=a.owner_id
                       AND e.payload->>'call_id'=a.detail->>'call_id')""",
                (ctx.id,),
            )
            jobs = await rows(
                conn,
                "SELECT * FROM run_jobs WHERE run_id=%s AND clock_seq>0 "
                "AND status NOT IN ('completed','failed') ORDER BY owner_id,kind,clock_seq",
                (ctx.id,),
            )
            processes = await rows(conn, "SELECT * FROM clock_processes WHERE run_id=%s", (ctx.id,))
            bars = {(p["owner_id"], p["kind"]): p["bar_s"] for p in processes}
            # A call that was running at the stop has no saved effect yet. The bar already moved
            # past it, so block from where it began, or a later event could commit before it.
            unfinished = await rows(
                conn,
                "SELECT owner_id,start_s FROM clock_actions "
                "WHERE run_id=%s AND process='agent' AND status='active'",
                (ctx.id,),
            )
            for job in jobs:
                if job["output"] is None:
                    continue
                owner, seq = job["owner_id"], job["clock_seq"]
                if job["kind"] == "agent":
                    await schedule(
                        conn,
                        ctx.id,
                        job["end_s"],
                        "agent_finish",
                        {"job_id": str(job["id"])},
                        f"agent_finish:{owner}:c{seq}",
                        owner,
                    )
                else:
                    await schedule(
                        conn,
                        ctx.id,
                        job["end_s"],
                        "actor_effect",
                        {"job_id": str(job["id"])},
                        f"actor_effect:{owner}:c{seq}",
                        owner,
                    )
        for job in jobs:
            if job["output"] is not None:
                continue
            if job["kind"] == "agent":
                open_calls = [
                    item["start_s"]
                    for item in unfinished
                    if item["owner_id"] == job["owner_id"] and item["start_s"] >= job["start_s"]
                ]
                bar = min([bars[(job["owner_id"], "agent")], *open_calls])
                self._start_agent(ctx, job, resume_bar=bar)
            else:
                self._start_actor(ctx, job)

    async def _drain(self) -> None:
        """Let paid model calls finish before stopping, within a real-time bound."""
        self._stopping = True
        self._stop_signal.set()
        try:
            async with asyncio.timeout(self.drain_timeout_seconds):
                while self.gate.blocking() and any(not t.done() for t in self._tasks.values()):
                    self.gate.clear_changed()
                    await self.gate.wait_changed(0.5)
        except TimeoutError:
            logger.warning("Clock conductor stopped before all model calls finished.")

    async def _stop_tasks(self) -> None:
        """Cancel parked and unfinished work. A later advance resumes it from checkpoints."""
        self._stopping = True
        self._stop_signal.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _pause_for_budget(self, ctx: _Run) -> None:
        """Pause for a budget refusal, a provider's refusal, or an outage. A resume continues."""
        await self._drain()
        reason = {"reason": "budget", **(self._pause_reason or {})}
        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (ctx.id,)
            )
            if run["status"] != "running":
                return
            state = await row(conn, "SELECT * FROM clock_state WHERE run_id=%s", (ctx.id,))
            await conn.execute(
                "UPDATE environment_runs SET status='paused',result=%s WHERE id=%s",
                (Jsonb(reason), ctx.id),
            )
            await emit(
                conn,
                ctx.id,
                "run_paused",
                run["current_window"],
                {"sim_s": state["sim_s"], **reason},
                f"run_paused:{reason['reason']}:{state['pop_seq']}",
            )

    async def _commit_turn(self, ctx: _Run, due_s: int, process: str | None) -> int:
        """R1, R5: story time moves only here, and never backwards. Commit before acting."""
        async with self.pool.connection() as conn, conn.transaction():
            state = await row(
                conn,
                "UPDATE clock_state SET sim_s=GREATEST(sim_s,%s),pop_seq=pop_seq+1,turn_process=%s "
                "WHERE run_id=%s RETURNING sim_s,pop_seq",
                (due_s, process, ctx.id),
            )
        return state["sim_s"]

    async def _clear_turn(self, ctx: _Run) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE clock_state SET turn_process=NULL WHERE run_id=%s", (ctx.id,)
            )

    async def _release_tool(self, ctx: _Run, entry) -> None:
        owner = entry.process.split(":", 1)[1]
        async with self.pool.connection() as conn, conn.transaction():
            # The run row first: this fences a pause, and it is the lock order everywhere.
            run = await row(
                conn, "SELECT status FROM environment_runs WHERE id=%s FOR SHARE", (ctx.id,)
            )
            if run["status"] != "running":
                raise _Interrupted
            state = await row(
                conn,
                "UPDATE clock_state SET sim_s=GREATEST(sim_s,%s),pop_seq=pop_seq+1,turn_process=%s "
                "WHERE run_id=%s RETURNING sim_s",
                (entry.stamp_s, entry.process, ctx.id),
            )
            await conn.execute(
                "UPDATE clock_processes SET bar_s=GREATEST(bar_s,%s) "
                "WHERE run_id=%s AND owner_id=%s AND kind='agent'",
                (entry.stamp_s + entry.duration_s, ctx.id, owner),
            )
            await conn.execute(
                """INSERT INTO clock_actions
                   (id,run_id,owner_id,process,label,detail,start_s,duration_s,status,key)
                   VALUES (%s,%s,%s,'agent',%s,%s,%s,%s,'active',%s)
                   ON CONFLICT (run_id,key) DO NOTHING""",
                (
                    uuid5(ctx.id, f"clock_action:{entry.key}"),
                    ctx.id,
                    owner,
                    entry.name,
                    Jsonb({"call_id": entry.call_id}),
                    entry.stamp_s,
                    entry.duration_s,
                    entry.key,
                ),
            )
            if state["sim_s"] != entry.stamp_s:
                # Only a dropped call that arrived late can do this. Record it; do not hide it.
                await emit(
                    conn,
                    ctx.id,
                    "simulator_error",
                    ctx.config.window_at(state["sim_s"]),
                    {"reason": "tool_after_its_stamp", "key": entry.key},
                    f"tool_after_stamp:{entry.key}",
                    owner,
                )
        finished = entry.finished
        self.gate.release(entry)
        await finished.wait()
        async with self.pool.connection() as conn, conn.transaction():
            if entry.completed:
                await conn.execute(
                    "UPDATE clock_actions SET status='done' WHERE run_id=%s AND key=%s",
                    (ctx.id, entry.key),
                )
            await conn.execute(
                "UPDATE clock_state SET turn_process=NULL WHERE run_id=%s", (ctx.id,)
            )

    async def _drop_stale(self, ctx: _Run, entry) -> None:
        owner = entry.process.split(":", 1)[1]
        self.gate.drop(entry)
        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn, "SELECT current_window FROM environment_runs WHERE id=%s", (ctx.id,)
            )
            await emit(
                conn,
                ctx.id,
                "simulator_error",
                run["current_window"],
                {"reason": "tool_call_never_arrived", "key": entry.key},
                f"tool_never_arrived:{entry.key}",
                owner,
            )

    async def _pop(self, ctx: _Run, event: dict) -> None:
        """Handle one durable event. An interrupted handler leaves it queued for a resume."""
        kind = event["kind"]
        owner = event["owner_id"]
        process = {
            "agent_finish": agent_process(owner) if owner else None,
            "actor_effect": actor_process(owner) if owner else None,
        }.get(kind, "director")
        now_s = await self._commit_turn(ctx, event["due_s"], process)
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE clock_events SET popped_seq="
                "(SELECT pop_seq FROM clock_state WHERE run_id=%s) WHERE id=%s",
                (ctx.id, event["id"]),
            )
        handler = {
            "actor_wake": self._on_actor_wake,
            "actor_effect": self._on_actor_effect,
            "agent_wake": self._on_agent_wake,
            "agent_finish": self._on_agent_finish,
            "action_end": self._on_action_end,
            "perturbation": self._on_perturbation,
            "beat": self._on_beat,
            "boundary": self._on_boundary,
            "lifecycle_check": self._on_lifecycle,
            "director_plan": self._on_director_plan,
            "plan": self._on_director_plan,
            "cue": self._on_director_plan,
            "director_review": self._on_review,
            "run_end": self._on_run_end,
        }.get(kind)
        try:
            try:
                if handler is None:
                    await self._simulator_error(
                        ctx, {"reason": "unknown_clock_event", "kind": kind}
                    )
                else:
                    await handler(ctx, event, now_s)
            except (_BudgetPause, _Interrupted, psycopg.OperationalError):
                raise  # The event stays queued. A resume or a new conductor repeats it.
            except Exception as error:
                # A bad saved plan or a logic error would stop every resume at this event.
                logger.exception("A clock event failed and is recorded: %s", kind)
                await self._simulator_error(
                    ctx,
                    {
                        "reason": "event_failed",
                        "key": event["key"],
                        "kind": kind,
                        "failure": type(error).__name__,
                        "detail": str(error).replace("\x00", "")[:300],
                    },
                    owner,
                    f"event_failed:{event['key']}",
                )
                await self._free_after_failed_event(ctx, event, now_s)
            await self._mark_done(ctx, event)
        finally:
            await self._clear_turn(ctx)

    async def _free_after_failed_event(self, ctx: _Run, event: dict, now_s: int) -> None:
        """A failed wake or result must not leave its process stuck, or failing in a loop.

        The process rests until the next daily wake. What it has not read stays undelivered.
        """
        kind, owner = event["kind"], event["owner_id"]
        names = {
            "actor_wake": "actor",
            "agent_wake": "agent",
            "actor_effect": "actor",
            "agent_finish": "agent",
        }
        if kind not in names or owner is None:
            return
        until = next_daily_wake(now_s, ctx.config)
        async with self.pool.connection() as conn, conn.transaction():
            if "job_id" in event["payload"]:
                await conn.execute(
                    "UPDATE run_jobs SET status='failed',error='The result could not be applied.' "
                    "WHERE id=%s AND status NOT IN ('completed','failed')",
                    (event["payload"]["job_id"],),
                )
            await conn.execute(
                """UPDATE clock_processes SET
                   state=CASE WHEN state IN ('dead','offline') THEN state ELSE 'waiting' END,
                   until_s=%s,wait_id=wait_id+1,wake_on=COALESCE(wake_on,'{}'::jsonb)||%s
                   WHERE run_id=%s AND owner_id=%s AND kind=%s""",
                (until, Jsonb({"quota_until": until}), ctx.id, owner, names[kind]),
            )

    async def _mark_done(self, ctx: _Run, event: dict, conn=None) -> None:
        query = "UPDATE clock_events SET status='done' WHERE id=%s AND status='queued'"
        if conn is not None:
            await conn.execute(query, (event["id"],))
            return
        async with self.pool.connection() as own:
            await own.execute(query, (event["id"],))

    async def _simulator_error(self, ctx: _Run, payload: dict, owner=None, key=None) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            state = await row(conn, "SELECT pop_seq FROM clock_state WHERE run_id=%s", (ctx.id,))
            run = await row(
                conn, "SELECT current_window FROM environment_runs WHERE id=%s", (ctx.id,)
            )
            await emit(
                conn,
                ctx.id,
                "simulator_error",
                run["current_window"],
                payload,
                key or f"simulator_error:{state['pop_seq']}:{fingerprint(payload)[:16]}",
                owner,
            )

    async def _saved_calls(self, process: str, activation_id) -> set[str]:
        """Calls of this activation with a saved result. A resume never repeats those."""
        owner = process.split(":", 1)[1]
        async with self.pool.connection() as conn:
            saved = await rows(
                conn,
                "SELECT payload->>'call_id' AS call_id FROM run_events WHERE run_id=%s "
                "AND kind='assistant_tool_call' AND owner_id=%s "
                "AND payload->>'activation_id'=%s",
                (self._run_id, owner, str(activation_id)),
            )
        return {item["call_id"] for item in saved}

    # Wakes (R8, R9) ----------------------------------------------------------------------

    async def _undelivered(self, conn, ctx: _Run, recipient_kind: str, kinds, owner=None) -> list:
        return await rows(
            conn,
            """SELECT e.*, p.owner_id AS recipient, p.agent_id AS recipient_agent
               FROM run_events e JOIN run_participants p
                 ON p.run_id=e.run_id AND (e.owner_id IS NULL OR e.owner_id=p.owner_id)
               WHERE e.run_id=%s AND e.kind=ANY(%s) AND (%s::text IS NULL OR p.owner_id=%s)
               AND NOT EXISTS (SELECT 1 FROM event_deliveries d WHERE d.event_id=e.id
                   AND d.owner_id=p.owner_id AND d.recipient_kind=%s)
               ORDER BY e.created_at,e.id""",
            (ctx.id, list(kinds), owner, owner, recipient_kind),
        )

    @staticmethod
    def _can_wake(event, config: ClockConfig) -> bool:
        # What was already there when the run began is news for the first wake. It wakes nobody.
        return not (
            event["cause_process"] is None and event_time_s(event["created_at"], config) == 0
        )

    async def _reconcile(self, ctx: _Run) -> None:
        """Keep exactly one queued wake per process, at the earliest moment it is due."""
        config = ctx.config
        async with self.pool.connection() as conn, conn.transaction():
            state = await row(conn, "SELECT sim_s FROM clock_state WHERE run_id=%s", (ctx.id,))
            now_s = state["sim_s"]
            processes = await rows(
                conn,
                "SELECT * FROM clock_processes WHERE run_id=%s ORDER BY owner_id,kind",
                (ctx.id,),
            )
            queued = {
                (w["owner_id"], w["kind"]): w
                for w in await rows(
                    conn,
                    "SELECT * FROM clock_events WHERE run_id=%s AND status='queued' "
                    "AND kind IN ('actor_wake','agent_wake')",
                    (ctx.id,),
                )
            }
            actions = {
                a["owner_id"]: a
                for a in await rows(
                    conn,
                    "SELECT * FROM clock_actions WHERE run_id=%s AND process='actor' "
                    "AND status='active'",
                    (ctx.id,),
                )
            }
            agent_events = await self._undelivered(conn, ctx, "agent", AGENT_KINDS)
            actor_events = await self._undelivered(conn, ctx, "actor", ACTOR_KINDS)
            for process in processes:
                owner = process["owner_id"]
                kind = process["kind"]
                wake_kind = f"{kind}_wake"
                existing = queued.get((owner, wake_kind))
                if process["state"] in {"dead", "offline"}:
                    if existing:
                        await cancel(conn, ctx.id, existing["key"])
                    continue
                if process["state"] == "active":
                    continue
                if kind == "agent":
                    desired = self._agent_due(
                        ctx, process, now_s, [e for e in agent_events if e["recipient"] == owner]
                    )
                else:
                    action = actions.get(owner)
                    if action is not None and not action["reachable"]:
                        continue  # R9: frozen. Everything waits for action_end.
                    own_events = [e for e in actor_events if e["recipient"] == owner]
                    # Owner and assistant can thank each other forever. Once they have only
                    # chatted a few times in a row, new chat waits until the owner next
                    # surfaces by themselves. Mail and other news still get through, and an
                    # assistant that does something real starts the count again.
                    if await self._chat_capped(conn, ctx, owner):
                        own_events = [e for e in own_events if e["kind"] != "owner_message"]
                    desired = self._actor_due(ctx, process, now_s, own_events, action)
                quota_until = (process["wake_on"] or {}).get("quota_until")
                if quota_until is not None and quota_until > now_s:
                    desired = quota_until  # R14: today's quota is spent. Nothing is lost.
                if desired is None:
                    if kind == "agent" or action is None:
                        # R8: every yield has an until. A first wait takes the next morning.
                        until = next_daily_wake(now_s, config)
                        await conn.execute(
                            "UPDATE clock_processes SET until_s=%s "
                            "WHERE run_id=%s AND owner_id=%s AND kind=%s",
                            (until, ctx.id, owner, kind),
                        )
                        desired = until
                    else:
                        continue
                if desired >= config.duration_s:
                    continue  # Nothing new starts at the last moment of the run.
                if existing is None:
                    await schedule(
                        conn,
                        ctx.id,
                        desired,
                        wake_kind,
                        {"wait_id": process["wait_id"]},
                        f"{wake_kind}:{owner}:w{process['wait_id']}",
                        owner,
                    )
                elif desired < existing["due_s"]:
                    await retime(conn, ctx.id, existing["key"], desired)

    async def _chat_capped(self, conn, ctx: _Run, owner: str) -> bool:
        """Have this owner and assistant only chatted since something real last happened?

        Only a plan or a world event starts the count again, or a tool that did something.
        The end of an action does not, and neither does reading or a refused call.
        """
        streak = await row(
            conn,
            """SELECT count(*) AS n FROM run_events e WHERE e.run_id=%s AND e.owner_id=%s
               AND e.kind='owner_message' AND e.payload->>'source'='owner'
               AND e.created_at > COALESCE((
                   SELECT max(t.created_at) FROM run_events t
                   WHERE t.run_id=%s AND t.owner_id=%s
                   AND ((t.kind='actor_cue' AND t.payload->>'action_key' IS NULL
                         AND t.payload->>'quiet' IS NULL)
                        OR (t.kind='assistant_tool_call'
                            AND t.payload->>'tool' = ANY(%s)
                            AND t.payload#>>'{result,ok}' = 'true'
                            AND COALESCE(t.payload#>>'{arguments,action}','') <> ALL(%s)))
                   ), '-infinity'::timestamptz)""",
            (
                ctx.id,
                owner,
                ctx.id,
                owner,
                sorted(EFFECTFUL_TOOLS),
                sorted(READ_ONLY_FORM_ACTIONS),
            ),
        )
        return streak["n"] >= CHAT_STREAK_LIMIT

    def _agent_due(self, ctx: _Run, process: dict, now_s: int, events: list) -> int | None:
        config = ctx.config
        due = []
        owner = process["owner_id"]
        agent_id = ctx.participants[owner]["agent_id"]
        # Two assistants can answer each other forever. After a few wakes caused only by each
        # other, their messages wait as ambient news does, until the owner speaks again.
        limited = (process["wake_on"] or {}).get("peer_streak", 0) >= PEER_WAKE_LIMIT
        ambient = []
        for event in events:
            kind = classify_agent_event(event, owner, agent_id)
            if kind is None or not self._can_wake(event, config):
                continue
            if kind == "direct" and not (limited and is_peer_event(event, owner)):
                due.append(now_s)
            else:
                ambient.append(event_time_s(event["created_at"], config))
        if ambient:
            due.append(max(now_s, min(ambient) + config.ambient_delay_s))
        if process["until_s"] is not None:
            due.append(max(now_s, process["until_s"]))
        return min(due) if due else None

    def _actor_due(
        self, ctx: _Run, process: dict, now_s: int, events: list, action: dict | None
    ) -> int | None:
        config = ctx.config
        owner = process["owner_id"]
        due = []
        messages = []
        for event in events:
            kind = classify_actor_event(event, owner)
            if kind is None or not self._can_wake(event, config):
                continue
            if kind == "cue":
                due.append(now_s)
            else:
                messages.append(event_time_s(event["created_at"], config))
        if messages:
            delay = notice_delay_s(config.seed, f"{owner}:{process['wait_id']}")
            due.append(max(now_s, min(messages) + delay))
        # A running action ends with its own event, which wakes this person.
        if action is None and process["until_s"] is not None:
            due.append(max(now_s, process["until_s"]))
        return min(due) if due else None

    # Agent activations -------------------------------------------------------------------

    async def _on_agent_wake(self, ctx: _Run, event: dict, now_s: int) -> None:
        owner = event["owner_id"]
        job = None
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            process = await self._process(conn, ctx, owner, "agent")
            if process["state"] in {"dead", "offline", "active"} or (
                event["payload"].get("wait_id") != process["wait_id"]
            ):
                return
            seq = process["activation_seq"] + 1
            if await self._quota_spent(conn, ctx, owner, "agent", now_s):
                await self._rest_until_tomorrow(conn, ctx, run, owner, "agent", process, now_s)
                return
            before = (process["wake_on"] or {}).get("peer_streak", 0)
            job, streak = await self._prepare_agent(conn, ctx, run, owner, seq, now_s, process)
            await conn.execute(
                """UPDATE clock_processes SET state='active',bar_s=GREATEST(bar_s,%s),until_s=NULL,
                   wait_id=wait_id+1,activation_seq=%s,last_wake_s=%s,
                   wake_on=COALESCE(wake_on,'{}'::jsonb)||%s
                   WHERE run_id=%s AND owner_id=%s AND kind='agent'""",
                (now_s, seq, now_s, Jsonb({"peer_streak": streak}), ctx.id, owner),
            )
            if streak >= PEER_WAKE_LIMIT > before:
                await emit(
                    conn,
                    ctx.id,
                    "process_peer_limit",
                    run["current_window"],
                    {"activations": streak},
                    f"process_peer_limit:{owner}:c{seq}",
                    owner,
                )
            await self._mark_done(ctx, event, conn)
        self._start_agent(ctx, job)

    async def _quota_spent(self, conn, ctx: _Run, owner: str, kind: str, now_s: int) -> bool:
        """R14: count this day's work from saved jobs, so a restart changes nothing."""
        used = await row(
            conn,
            "SELECT count(*) AS n FROM run_jobs WHERE run_id=%s AND owner_id=%s AND kind=%s "
            "AND start_s>=%s",
            (ctx.id, owner, kind, ctx.config.day_start_s(now_s)),
        )
        return used["n"] >= ctx.config.daily_quota(kind)

    async def _rest_until_tomorrow(
        self, conn, ctx: _Run, run: dict, owner: str, kind: str, process: dict, now_s: int
    ) -> None:
        """R14: a quota stops one process until the next daily wake. It is saved and visible.

        What arrives meanwhile stays undelivered, so the next activation carries it.
        """
        until = next_daily_wake(now_s, ctx.config)
        await conn.execute(
            """UPDATE clock_processes SET state='waiting',until_s=%s,wait_id=wait_id+1,
               wake_on=COALESCE(wake_on,'{}'::jsonb)||%s
               WHERE run_id=%s AND owner_id=%s AND kind=%s""",
            (until, Jsonb({"quota_until": until}), ctx.id, owner, kind),
        )
        await emit(
            conn,
            ctx.id,
            "process_limit_reached",
            run["current_window"],
            {
                "process": kind,
                "activations": process["activation_seq"],
                "daily_quota": ctx.config.daily_quota(kind),
                "until_s": until,
            },
            f"process_limit:{kind}:{owner}:{until}",
            owner,
        )

    async def _locked_run(self, conn, ctx: _Run) -> dict:
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (ctx.id,))
        if run["status"] != "running":
            raise _Interrupted
        return run

    async def _process(self, conn, ctx: _Run, owner: str, kind: str) -> dict:
        return await row(
            conn,
            "SELECT * FROM clock_processes WHERE run_id=%s AND owner_id=%s AND kind=%s FOR UPDATE",
            (ctx.id, owner, kind),
        )

    async def _prepare_agent(
        self, conn, ctx: _Run, run: dict, owner: str, seq: int, now_s: int, process: dict
    ) -> dict:
        """Freeze one activation's input, like RunEngine._prepare for an agent.

        Returns the job and the count of wakes caused only by other assistants.
        """
        window = run["current_window"]
        participant = ctx.participants[owner]
        job_id = uuid5(ctx.id, f"agent:{owner}:c{seq}")
        events = await self._undelivered(conn, ctx, "agent", AGENT_KINDS, owner)
        silent, eligible = [], []
        for event in events:
            kind = classify_agent_event(event, owner, participant["agent_id"])
            (eligible if kind else silent).append(event)
        board = [e for e in eligible if e["kind"] == "board_posted"]
        bounded = []
        size, owner_count = 0, 0
        for event in eligible:
            if event["kind"] == "board_posted":
                continue
            cost = len(json.dumps(event["payload"], default=str)) + 250
            if (
                len(bounded) >= EVENT_LIMIT
                or size + cost > EVENT_CHARS
                or (event["kind"] == "owner_message" and owner_count >= OWNER_MESSAGE_LIMIT)
            ):
                break  # The rest stay undelivered, so a follow-up wake carries them.
            bounded.append(event)
            size += cost
            owner_count += event["kind"] == "owner_message"
        for name, batch in (
            ("board_batch", [e for e in board if not e["payload"].get("audience")]),
            ("board_batch_direct", [e for e in board if e["payload"].get("audience")]),
        ):
            if not batch:
                continue
            summary = {
                "count": len(batch),
                "message_ids": [e["payload"]["message_id"] for e in batch[-20:]],
                "instruction": "Read unacknowledged board updates with your board tools.",
            }
            if name == "board_batch_direct":
                summary["audience"] = [str(participant["agent_id"])]
            summary_id = await emit(
                conn, ctx.id, name, window, summary, f"{name}:{owner}:c{seq}", owner
            )
            bounded.append({"id": summary_id, "kind": "board_posted", "payload": summary})
        owner_messages = [
            {"message_id": e["payload"]["message_id"], "content": e["payload"]["content"]}
            for e in bounded
            if e["kind"] == "owner_message"
        ]
        observations = [
            {
                "event_id": str(e["id"]),
                "kind": e["kind"],
                "content": json.dumps(e["payload"], default=str)[:12_000],
            }
            for e in bounded
            if e["kind"] != "owner_message"
        ]
        reason = None
        if not bounded:
            requested = (process["wake_on"] or {}).get("requested")
            reason = REASON_REQUESTED if requested else REASON_ROUTINE
        payload = ActivationEnvelope(
            run_id=ctx.id,
            participant_id=participant["agent_id"],
            activation_id=job_id,
            window_id=window,
            source_event_ids=[e["id"] for e in bounded],
            owner_messages=owner_messages,
            world_observations=observations,
            now_text=ctx.config.now_text(now_s),
            wake_reason=reason,
        ).model_dump(mode="json")
        job = await row(
            conn,
            """INSERT INTO run_jobs
               (id,run_id,owner_id,"window",kind,input,input_hash,clock_seq,start_s,status)
               VALUES (%s,%s,%s,%s,'agent',%s,%s,%s,%s,'running') RETURNING *""",
            (job_id, ctx.id, owner, window, Jsonb(payload), fingerprint(payload), seq, now_s),
        )
        delivered = bounded + board + silent
        for event in delivered:
            await conn.execute(
                "INSERT INTO event_deliveries VALUES (%s,%s,%s,'agent',%s) ON CONFLICT DO NOTHING",
                (ctx.id, event["id"], owner, job_id),
            )
        streak = (process["wake_on"] or {}).get("peer_streak", 0)
        if owner_messages or not eligible:
            streak = 0  # The owner spoke, or this is its own timer with nothing waiting.
        elif any(is_peer_event(e, owner) for e in eligible):
            streak += 1
        return job, streak

    def _start_agent(self, ctx: _Run, job: dict, *, resume_bar: int | None = None) -> None:
        owner = job["owner_id"]
        process = agent_process(owner)
        self.gate.begin_activation(
            process, job["id"], job["clock_seq"], job["start_s"], resume_bar=resume_bar
        )
        self._tasks[process] = asyncio.create_task(self._agent_task(ctx, job))

    async def _agent_task(self, ctx: _Run, job: dict) -> None:
        owner = job["owner_id"]
        process = agent_process(owner)
        agent = self.agents[owner]
        attempts = job["attempts"]
        try:
            envelope = ActivationEnvelope.model_validate(job["input"])
            bounded = await self._bounded_failures(job)
            while True:
                try:
                    with budget_context(run_id=ctx.id, owner_id=owner):
                        result = await agent.run_activation(envelope)
                    break
                except asyncio.CancelledError:
                    raise
                except AgentRunStopped:
                    # An operator pause, not a failure. The conductor stops this task soon.
                    self.gate.attempt_failed(process)
                    if self._stopping:
                        return
                    await asyncio.sleep(self.retry_base_seconds)
                except Exception as error:
                    self.gate.attempt_failed(process)
                    attempts += 1
                    if _is_budget(error):
                        self._request_budget_pause(owner, "assistant")
                        return
                    failure = classify_failure(error)
                    await self._record_failure(ctx, job, attempts, failure, error)
                    if self._outage_pause(failure, owner, "assistant"):
                        return
                    if not failure.holds:
                        bounded += 1
                        if bounded > ctx.spec.limits.recovery_attempts or not _is_retryable(error):
                            await self._abandon(ctx, job, agent, failure.kind, attempts)
                            return
                    if not await self._wait_to_retry(attempts):
                        return
            await self._save_agent_result(ctx, job, agent, result)
        except asyncio.CancelledError:
            raise
        except psycopg.OperationalError as error:
            self._fatal = error
        except Exception as error:
            await self._close_after_save_failed(ctx, job, agent, attempts, error)
        except BaseException as error:
            self._fatal = error
        finally:
            self.gate.end(process)

    async def _close_after_save_failed(self, ctx, job, agent, attempts, error) -> None:
        """A result that cannot be saved would stop the conductor on every resume."""
        logger.exception("An assistant result could not be saved.")
        try:
            await self._abandon(ctx, job, agent, classify_failure(error).kind, attempts)
        except Exception as second:
            self._fatal = second

    async def _bounded_failures(self, job: dict) -> int:
        """Failures that use up this job's limited retries. An outage uses up none of them."""
        if not job["attempts"]:
            return 0
        async with self.pool.connection() as conn:
            found = await row(
                conn,
                "SELECT count(*) AS n FROM run_events WHERE run_id=%s AND kind='job_failure' "
                "AND payload->>'job_id'=%s AND COALESCE(payload->>'holding','false')='false'",
                (job["run_id"], str(job["id"])),
            )
        return found["n"]

    def _retry_delay(self, attempts: int) -> float:
        """R14: capped exponential backoff, with a spread so that callers do not retry together.

        It follows the run's failures too, so many callers failing together slow each other.
        """
        level = max(attempts, self._outage_failures, 1)
        delay = self.retry_base_seconds * 2 ** min(level - 1, 30)
        return min(self.retry_cap_seconds, delay) * random.uniform(0.85, 1.15)

    async def _wait_to_retry(self, attempts: int) -> bool:
        """Wait while the process keeps its place at the frontier. False: stop retrying."""
        try:
            async with asyncio.timeout(self._retry_delay(attempts)):
                await self._stop_signal.wait()
        except TimeoutError:
            pass
        return not (self._stopping or self._pause_reason is not None)

    def _outage_pause(self, failure, owner: str | None, component: str) -> bool:
        """Count an infrastructure failure. True when the run must pause because of it.

        A provider's refusal of the key or the account pauses at once. Any failure of the
        service opens the breaker when nothing has worked for long enough. Story time waits
        meanwhile, because the process keeps its place at the frontier (R1, R14).
        """
        if self._pause_reason is not None:
            return True
        if failure.pauses:
            self._request_pause("provider_rejected", owner, component, failure.kind)
            return True
        if not failure.counts_as_outage:
            return False
        if self.gate.successes != self._successes_seen:
            self._successes_seen = self.gate.successes
            self._outage_failures = 0
            self._outage_since = None
        now = time.monotonic()
        self._outage_since = now if self._outage_since is None else self._outage_since
        self._outage_failures += 1
        if (
            self._outage_failures >= self.breaker_failures
            and now - self._outage_since >= self.breaker_seconds
        ):
            self._request_pause("provider_outage", owner, component, failure.kind)
            return True
        return False

    def _outage_over(self) -> None:
        self._outage_failures = 0
        self._outage_since = None
        self._successes_seen = self.gate.successes

    def _request_pause(self, reason: str, owner: str | None, component: str, kind=None) -> None:
        if self._pause_reason is None:
            self._pause_reason = {"reason": reason, "component": component}
            if owner is not None:
                self._pause_reason["owner_id"] = owner
            if kind is not None:
                self._pause_reason["failure_kind"] = kind
        self._stop_signal.set()
        self.gate.notify()

    def _request_budget_pause(self, owner: str, component: str) -> None:
        self._request_pause("budget", owner, component)

    async def _record_failure(
        self, ctx: _Run, job: dict, attempts: int, failure, error: Exception | None = None
    ) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE run_jobs SET attempts=%s,error=%s,failure_kind=%s,failure_retryable=%s "
                "WHERE id=%s",
                (
                    attempts,
                    f"{failure.kind}: saved inputs retained.",
                    failure.kind,
                    failure.retryable,
                    job["id"],
                ),
            )
            await emit(
                conn,
                ctx.id,
                "job_failure",
                job["window"],
                {
                    "component": job["kind"],
                    "job_id": str(job["id"]),
                    "attempt": attempts,
                    "failure_kind": failure.kind,
                    "retryable": failure.retryable,
                    "holding": failure.holds or failure.pauses,
                    **({"detail": failure_detail(error)} if error is not None else {}),
                },
                f"job_failure:{job['id']}:{attempts}",
                job["owner_id"],
            )

    async def _save_agent_result(self, ctx: _Run, job: dict, agent, result) -> None:
        """Save the output and queue its finish at the stamp where the graph ended."""
        snapshot = await agent.graph.aget_state(agent.config)
        end_s = fold_stamps(
            job["start_s"],
            snapshot.values.get("messages", []),
            job["id"],
            ctx.config,
            gated=self.gate.gated_names(agent.clock_process),
        ).end_s
        output = strip_nul(result.model_dump(mode="json"))
        owner, seq = job["owner_id"], job["clock_seq"]
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE run_jobs SET output=%s,status='saved',end_s=%s,error=NULL,"
                "failure_kind=NULL,failure_retryable=false WHERE id=%s",
                (Jsonb(output), end_s, job["id"]),
            )
            await schedule(
                conn,
                ctx.id,
                end_s,
                "agent_finish",
                {"job_id": str(job["id"])},
                f"agent_finish:{owner}:c{seq}",
                owner,
            )

    async def _abandon(self, ctx: _Run, job: dict, agent, kind: str, attempts: int) -> None:
        """R14: close the task receipt, so the next activation can start on this session."""
        output = ClockedActivationResult(status="responded", text="").model_dump(mode="json")
        owner, seq = job["owner_id"], job["clock_seq"]
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE agent_tasks SET status='completed',result=%s,"
                "completed_at=clock_timestamp() "
                "WHERE session_key=%s AND task_id=%s AND status='pending'",
                (json.dumps(output), agent.session_key, job["id"]),
            )
            process = await row(
                conn,
                "SELECT bar_s FROM clock_processes "
                "WHERE run_id=%s AND owner_id=%s AND kind='agent'",
                (ctx.id, owner),
            )
            redelivered = await self._undo_delivery(conn, ctx, job, "agent")
            # Calls of this activation that never finished will not run now.
            await conn.execute(
                "UPDATE clock_actions SET status='cancelled' WHERE run_id=%s AND owner_id=%s "
                "AND process='agent' AND status='active'",
                (ctx.id, owner),
            )
            await conn.execute(
                "UPDATE run_jobs SET output=%s,output_meta=%s,status='saved',end_s=%s,"
                "failure_kind=%s WHERE id=%s",
                (
                    Jsonb(output),
                    Jsonb(
                        {
                            "abandoned": True,
                            "failure_kind": kind,
                            "attempts": attempts,
                            "redelivered": redelivered,
                        }
                    ),
                    process["bar_s"],
                    kind,
                    job["id"],
                ),
            )
            await schedule(
                conn,
                ctx.id,
                process["bar_s"],
                "agent_finish",
                {"job_id": str(job["id"])},
                f"agent_finish:{owner}:c{seq}",
                owner,
            )

    async def _undo_delivery(self, conn, ctx: _Run, job: dict, recipient_kind: str) -> list[str]:
        """A failed job must not lose what it was given. Each event gets one more chance.

        Only messages from the owner side and cues come back. The job's output_meta names
        them, so a failure that repeats cannot wake this process forever.
        """
        owner = job["owner_id"]
        given = await rows(
            conn,
            """SELECT d.event_id FROM event_deliveries d JOIN run_events e ON e.id=d.event_id
               WHERE d.run_id=%s AND d.owner_id=%s AND d.recipient_kind=%s AND d.job_id=%s
               AND e.kind IN ('owner_message','actor_cue')""",
            (ctx.id, owner, recipient_kind, job["id"]),
        )
        tried = await rows(
            conn,
            """SELECT jsonb_array_elements_text(output_meta->'redelivered') AS event_id
               FROM run_jobs WHERE run_id=%s AND owner_id=%s AND kind=%s
               AND jsonb_typeof(output_meta->'redelivered')='array'""",
            (ctx.id, owner, job["kind"]),
        )
        earlier = {item["event_id"] for item in tried}
        again = [item["event_id"] for item in given if str(item["event_id"]) not in earlier]
        if again:
            await conn.execute(
                "DELETE FROM event_deliveries WHERE run_id=%s AND owner_id=%s "
                "AND recipient_kind=%s AND job_id=%s AND event_id=ANY(%s)",
                (ctx.id, owner, recipient_kind, job["id"], again),
            )
        return [str(item) for item in again]

    async def _on_agent_finish(self, ctx: _Run, event: dict, now_s: int) -> None:
        owner = event["owner_id"]
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            job = await row(
                conn, "SELECT * FROM run_jobs WHERE id=%s FOR UPDATE", (event["payload"]["job_id"],)
            )
            if job["status"] != "completed":
                output = job["output"]
                window = run["current_window"]
                if job["output_meta"].get("abandoned"):
                    await emit(
                        conn,
                        ctx.id,
                        "activation_abandoned",
                        window,
                        {
                            "job_id": str(job["id"]),
                            "failure_kind": job["output_meta"].get("failure_kind"),
                            "attempts": job["output_meta"].get("attempts"),
                        },
                        f"activation_abandoned:{job['id']}",
                        owner,
                    )
                else:
                    await self._deliver_reply(conn, ctx, job, output, window)
                    await emit(
                        conn,
                        ctx.id,
                        "agent_activation",
                        window,
                        {"output": output, "input": job["input"]},
                        f"activation:{job['id']}",
                        owner,
                    )
                wake_in = output.get("wake_in_minutes")
                until = until_after_yield(now_s, wake_in, ctx.config)
                await conn.execute(
                    """UPDATE clock_processes SET
                       state=CASE WHEN state IN ('dead','offline') THEN state ELSE 'waiting' END,
                       bar_s=GREATEST(bar_s,%s),until_s=%s,
                       wake_on=COALESCE(wake_on,'{}'::jsonb)||%s
                       WHERE run_id=%s AND owner_id=%s AND kind='agent'""",
                    (
                        now_s,
                        until,
                        Jsonb(
                            {
                                "requested": wake_in is not None,
                                "status": output.get("status"),
                                "wait_for": output.get("wait_for", []),
                            }
                        ),
                        ctx.id,
                        owner,
                    ),
                )
                await conn.execute(
                    "UPDATE run_jobs SET status='completed',end_s=%s,error=NULL WHERE id=%s",
                    (now_s, job["id"]),
                )
            await self._mark_done(ctx, event, conn)

    async def _deliver_reply(self, conn, ctx: _Run, job: dict, output: dict, window: int) -> None:
        """Text is an owner reply, unless this activation already sent one or it repeats."""
        text = output.get("text", "")
        if not text.strip():
            return
        run_id, owner = ctx.id, job["owner_id"]
        chunks = [text[start : start + 12000] for start in range(0, len(text), 12000)]
        request_ids = [
            uuid5(job["id"], "finalreply" if index == 0 else f"finalreply:{index}")
            for index in range(len(chunks))
        ]
        sent = await row(
            conn,
            "SELECT id FROM owner_messages WHERE run_id=%s AND owner_id=%s AND source='agent' "
            "AND created_at>%s AND NOT (request_id=ANY(%s)) LIMIT 1",
            (run_id, owner, job["created_at"], request_ids),
        )
        if sent:
            return
        if not job["input"].get("owner_messages"):
            latest = await row(
                conn,
                "SELECT content FROM owner_messages WHERE run_id=%s AND owner_id=%s "
                "AND source='agent' AND NOT (request_id=ANY(%s)) "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (run_id, owner, request_ids),
            )
            if latest and latest["content"] == text:
                return
        for request_id, chunk in zip(request_ids, chunks, strict=True):
            message_id = uuid5(run_id, f"message:{owner}:agent:{request_id}")
            await conn.execute(
                """INSERT INTO owner_messages
                   (id,run_id,owner_id,source,content,request_id,payload_hash,"window")
                   VALUES (%s,%s,%s,'agent',%s,%s,%s,%s)
                   ON CONFLICT (run_id,owner_id,source,request_id) DO NOTHING""",
                (
                    message_id,
                    run_id,
                    owner,
                    chunk,
                    request_id,
                    fingerprint({"content": chunk}),
                    window,
                ),
            )
            await emit(
                conn,
                run_id,
                "owner_message",
                window,
                {"message_id": str(message_id), "source": "agent", "content": chunk},
                f"message:{message_id}",
                owner,
            )

    # Owners ------------------------------------------------------------------------------

    async def _on_actor_wake(self, ctx: _Run, event: dict, now_s: int) -> None:
        owner = event["owner_id"]
        job = None
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            process = await self._process(conn, ctx, owner, "actor")
            action = await self._current_action(conn, ctx, owner)
            if (
                process["state"] in {"dead", "offline", "active"}
                or event["payload"].get("wait_id") != process["wait_id"]
            ):
                return
            if action is not None and not action["reachable"]:
                # A plan froze this person after the wake was queued. Retire this wake's key.
                await conn.execute(
                    "UPDATE clock_processes SET wait_id=wait_id+1 "
                    "WHERE run_id=%s AND owner_id=%s AND kind='actor'",
                    (ctx.id, owner),
                )
                return
            seq = process["activation_seq"] + 1
            if await self._quota_spent(conn, ctx, owner, "actor", now_s):
                await self._rest_until_tomorrow(conn, ctx, run, owner, "actor", process, now_s)
                return
            job = await self._prepare_actor(conn, ctx, run, owner, seq, now_s, action, process)
            await conn.execute(
                """UPDATE clock_processes SET state='active',bar_s=GREATEST(bar_s,%s),until_s=NULL,
                   wait_id=wait_id+1,activation_seq=%s,last_wake_s=%s
                   WHERE run_id=%s AND owner_id=%s AND kind='actor'""",
                (now_s, seq, now_s, ctx.id, owner),
            )
            await self._mark_done(ctx, event, conn)
        self._start_actor(ctx, job)

    async def _current_action(self, conn, ctx: _Run, owner: str) -> dict | None:
        return await row(
            conn,
            "SELECT * FROM clock_actions WHERE run_id=%s AND owner_id=%s AND process='actor' "
            "AND status='active' ORDER BY start_s DESC LIMIT 1",
            (ctx.id, owner),
        )

    async def _prepare_actor(
        self,
        conn,
        ctx: _Run,
        run: dict,
        owner: str,
        seq: int,
        now_s: int,
        action: dict | None,
        process: dict,
    ) -> dict:
        """Freeze one owner's view at this moment. The board is never part of it (R13)."""
        window = run["current_window"]
        job_id = uuid5(ctx.id, f"actor:{owner}:c{seq}")
        events = await self._undelivered(conn, ctx, "actor", ACTOR_KINDS, owner)
        cues = [e for e in events if e["kind"] == "actor_cue"]
        wakers = [e for e in events if classify_actor_event(e, owner) is not None]
        conversation = list(
            reversed(
                await rows(
                    conn,
                    "SELECT id,source,content FROM owner_messages WHERE run_id=%s AND owner_id=%s "
                    "ORDER BY created_at DESC,id DESC LIMIT %s",
                    (ctx.id, owner, ACTOR_VIEW_MESSAGES),
                )
            )
        )
        world = await self.world._snapshot(conn, ctx.id)
        own_world = {
            "owner_id": owner,
            "account": next(a for a in world["accounts"] if a["owner_id"] == owner),
            "reservations": [
                {"owner_id": r["owner_id"], "asset_id": r["asset_id"]}
                for r in world["reservations"]
            ],
        }
        observations = [
            {
                "id": str(e["id"]),
                "kind": e["kind"],
                "text": json.dumps(e["payload"], default=str)[:OBSERVATION_CHARS],
                "asset_id": e["payload"].get("asset_id"),
            }
            for e in events
            if (e["kind"] in ACTOR_OBSERVATION_KINDS or e["kind"] == "actor_effect_rejected")
            and (e["owner_id"] == owner or e["payload"].get("owner_id") == owner)
        ]
        observations.append(
            {
                "id": f"world:{ctx.id}:{seq}:{owner}",
                "kind": "current_world",
                "text": json.dumps(own_world, default=str)[:OBSERVATION_CHARS],
            }
        )
        latest = await row(
            conn,
            "SELECT output FROM run_jobs WHERE run_id=%s AND owner_id=%s AND kind='agent' "
            "AND status='completed' ORDER BY clock_seq DESC LIMIT 1",
            (ctx.id, owner),
        )
        view = build_actor_view(
            ctx.spec,
            owner,
            self._cue_text(ctx, cues, wakers, now_s, process["last_wake_s"]),
            conversation=[
                {
                    "role": "assistant" if m["source"] == "agent" else "owner",
                    "text": m["content"][:ACTOR_MESSAGE_CHARS],
                    "id": str(m["id"]),
                }
                for m in conversation
            ],
            observations=observations,
            source_event_id=str(wakers[-1]["id"]) if wakers else f"clock:{owner}:{seq}",
            window=window,
            inbox=_short_inbox(
                await self.inbox.actor_view(ctx.id, owner, limit=ACTOR_MAIL_ITEMS, connection=conn)
            ),
            resources=_short_records(
                await self.resources.actor_view(ctx.id, owner, connection=conn)
            ),
            market=await self.market.view(ctx.id, owner, connection=conn),
            transfers=await self.transfers.account(ctx.id, owner, connection=conn),
            assistant_wait_for=(latest["output"].get("wait_for", []) if latest else ()),
            clock=await self._clock_view(conn, ctx, owner, now_s, action),
        )
        payload = view.model_dump(mode="json")
        job = await row(
            conn,
            """INSERT INTO run_jobs
               (id,run_id,owner_id,"window",kind,input,input_hash,clock_seq,start_s,status)
               VALUES (%s,%s,%s,%s,'actor',%s,%s,%s,%s,'running') RETURNING *""",
            (job_id, ctx.id, owner, window, Jsonb(payload), fingerprint(payload), seq, now_s),
        )
        for event in events:
            await conn.execute(
                "INSERT INTO event_deliveries VALUES (%s,%s,%s,'actor',%s) ON CONFLICT DO NOTHING",
                (ctx.id, event["id"], owner, job_id),
            )
        return job

    @staticmethod
    def _cue_text(
        ctx: _Run, cues: list[dict], wakers: list, now_s: int, last_wake_s: int | None
    ) -> str:
        """Each cue with its own time. After a long freeze old plans are no longer news.

        A plan cue says what the day holds, so only the newest one still matters. What actually
        happened stays. One line says how much time went by.
        """
        if cues:
            config = ctx.config
            moments = [event_time_s(e["created_at"], config) for e in cues]
            stamped = list(zip(moments, cues, strict=True))
            head = ""
            if any(now_s - moment >= FREEZE_S for moment in moments):
                plans = [e for e in cues if e["payload"].get("source") == "timeline"]
                stamped = [
                    (moment, e)
                    for moment, e in stamped
                    if e["payload"].get("source") != "timeline" or e is plans[-1]
                ]
                since = last_wake_s if last_wake_s is not None else min(moments)
                head = (
                    f"{_how_long(now_s - since)} have gone by since you last had a moment "
                    "to look at anything.\n"
                )
            lines = [f"{config.now_text(moment)}: {e['payload']['cue']}" for moment, e in stamped]
            return head + "\n".join(lines)[-(CUE_CHARS - len(head)) :]
        if wakers:
            return NO_CUE
        return f"It's {ctx.config.now_text(now_s)}. You have a moment to think about your day."

    async def _clock_view(
        self, conn, ctx: _Run, owner: str, now_s: int, action: dict | None
    ) -> dict:
        """What this person knows about time. It holds no process or event names."""
        last = await row(
            conn,
            "SELECT label,status FROM clock_actions WHERE run_id=%s AND owner_id=%s "
            "AND process='actor' AND status<>'active' ORDER BY start_s DESC,key DESC LIMIT 1",
            (ctx.id, owner),
        )
        current = None
        if action is not None:
            left = action["start_s"] + action["duration_s"] - now_s
            current = {
                "description": action["label"],
                "minutes_left": max(0, left // 60),
                "you_would_notice_messages": action["reachable"],
            }
            after = (action["detail"] or {}).get("after")
            if after:
                current["after_this_you_will"] = after["description"]
        view = {
            "now": ctx.config.now_text(now_s),
            "today": ctx.config.today_text(now_s),
            "current_action": current,
            "last_action": {"description": last["label"]} if last else None,
        }
        card = await wallet.limits(conn, ctx.id, owner)
        if card["allowance"] or card["approved"]:
            view["assistant_may_still_spend_online"] = card["remaining"]
        older = await rows(
            conn,
            "SELECT content,created_at FROM owner_messages WHERE run_id=%s AND owner_id=%s "
            "AND source='owner' AND created_at<COALESCE((SELECT min(created_at) FROM ("
            "SELECT created_at FROM owner_messages WHERE run_id=%s AND owner_id=%s "
            "ORDER BY created_at DESC,id DESC LIMIT %s) AS seen),'-infinity') "
            "ORDER BY created_at DESC,id DESC LIMIT 200",
            (ctx.id, owner, ctx.id, owner, ACTOR_VIEW_MESSAGES),
        )
        if decisions := earlier_decisions(older, ctx.config.time_text):
            view["your_earlier_decisions"] = decisions
        return view

    def _start_actor(self, ctx: _Run, job: dict) -> None:
        process = actor_process(job["owner_id"])
        self.gate.begin_actor(process, job["start_s"])
        self._tasks[process] = asyncio.create_task(self._actor_task(ctx, job))

    async def _actor_task(self, ctx: _Run, job: dict) -> None:
        owner = job["owner_id"]
        process = actor_process(owner)
        actor = self.actors[owner]
        attempts = job["attempts"]
        failure_reason = None
        try:
            view = ActorView.model_validate(job["input"])
            bounded = await self._bounded_failures(job)
            while True:
                try:
                    async with self.gate.think_slot():
                        with budget_context(run_id=ctx.id, owner_id=owner):
                            output = await actor.respond(view)
                    output = _clean_output(validate_actor_output(view, output))
                    self._outage_over()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    attempts += 1
                    if _is_budget(error):
                        self._request_budget_pause(owner, "actor")
                        return
                    failure = classify_failure(error)
                    await self._record_failure(ctx, job, attempts, failure, error)
                    if self._outage_pause(failure, owner, "actor"):
                        return
                    if not failure.holds:
                        bounded += 1
                        if (
                            bounded > ctx.spec.limits.recovery_attempts
                            or isinstance(error, ActorContractError)
                            or not (failure.retryable or failure.kind == "provider_request_refused")
                        ):
                            # R14 and the Actor contract: a simulator failure becomes a wait.
                            # An outage never does: it retries with the same ID and view.
                            failure_reason = failure.kind
                            output = ActorOutput(kind="wait", source_event_id=view.source_event_id)
                            break
                    if not await self._wait_to_retry(attempts):
                        return
            await self._save_actor_result(ctx, job, actor, output, failure_reason)
        except asyncio.CancelledError:
            raise
        except psycopg.OperationalError as error:
            self._fatal = error
        except Exception as error:
            logger.exception("An owner's words could not be saved.")
            try:
                source = job["input"].get("source_event_id") or f"clock:{owner}"
                output = ActorOutput(kind="wait", source_event_id=source)
                await self._save_actor_result(ctx, job, actor, output, classify_failure(error).kind)
            except Exception as second:
                self._fatal = second
        except BaseException as error:
            self._fatal = error
        finally:
            self.gate.end(process)

    async def _save_actor_result(
        self, ctx: _Run, job: dict, actor, output: ActorOutput, failure_reason: str | None
    ) -> None:
        chars = len(output.text) + sum(len(m.body) for m in output.mail_requests)
        end_s = job["start_s"] + price_actor(chars, ctx.config)
        meta = {"attempts": strip_nul(getattr(actor, "last_attempts", []))}
        if failure_reason:
            meta["simulator_failure"] = failure_reason
        owner, seq = job["owner_id"], job["clock_seq"]
        async with self.pool.connection() as conn, conn.transaction():
            if failure_reason:
                meta["redelivered"] = await self._undo_delivery(conn, ctx, job, "actor")
            await conn.execute(
                "UPDATE run_jobs SET output=%s,output_meta=%s,status='saved',end_s=%s WHERE id=%s",
                (Jsonb(output.model_dump(mode="json")), Jsonb(meta), end_s, job["id"]),
            )
            await conn.execute(
                "UPDATE clock_processes SET bar_s=GREATEST(bar_s,%s) "
                "WHERE run_id=%s AND owner_id=%s AND kind='actor'",
                (end_s, ctx.id, owner),
            )
            await schedule(
                conn,
                ctx.id,
                end_s,
                "actor_effect",
                {"job_id": str(job["id"])},
                f"actor_effect:{owner}:c{seq}",
                owner,
            )

    async def _on_actor_effect(self, ctx: _Run, event: dict, now_s: int) -> None:
        owner = event["owner_id"]
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            job = await row(
                conn, "SELECT * FROM run_jobs WHERE id=%s FOR UPDATE", (event["payload"]["job_id"],)
            )
            if job["status"] != "completed":
                await self._apply_actor(conn, ctx, run, job, owner, now_s)
                await conn.execute(
                    "UPDATE run_jobs SET status='completed',end_s=%s WHERE id=%s",
                    (now_s, job["id"]),
                )
            await self._mark_done(ctx, event, conn)

    async def _attempt(self, conn, ctx, job, rejected: list, label: str, index: int, write) -> None:
        """A stale domain error is an outcome the owner observes, not a job failure."""
        try:
            async with conn.transaction():
                await write()
        except psycopg.OperationalError:
            raise
        except Exception as error:
            rejected.append(
                {
                    "effect": label,
                    "index": index,
                    "reason": (
                        str(error)[:500] if isinstance(error, ValueError) else type(error).__name__
                    ),
                }
            )

    async def _apply_actor(self, conn, ctx: _Run, run: dict, job: dict, owner: str, now_s: int):
        """Apply one saved owner output, one savepoint per effect, then place what comes next."""
        run_id = ctx.id
        window = run["current_window"]
        output = ActorOutput.model_validate(job["output"])
        view = ActorView.model_validate(job["input"])
        meta = job["output_meta"]
        rejected: list[dict] = []
        message = None
        if output.kind == "request_director_clarification" or meta.get("simulator_failure"):
            await emit(
                conn,
                run_id,
                "simulator_error",
                window,
                {
                    "reason": meta.get("simulator_failure") or "missing_consequential_fact",
                    "text": output.text,
                },
                f"actor_invalid:{job['id']}",
                owner,
            )
        elif output.kind != "wait" and output.text.strip():
            message = await save_message(
                conn, run_id, owner, "owner", output.text, job["id"], window
            )
        if output.authorized_amount is not None:

            async def consent():
                await self.world.set_consent(
                    run_id,
                    owner,
                    output.authorized_amount,
                    list(output.allowed_assets),
                    uuid5(job["id"], "consent"),
                    allowed_kinds=list(output.allowed_kinds),
                    connection=conn,
                )

            await self._attempt(conn, ctx, job, rejected, "booking_approval", 0, consent)
        if output.transfer_consent is not None and message is not None:

            async def transfer():
                await self.transfers.authorize(
                    run_id,
                    owner,
                    message["id"],
                    TransferArguments(
                        authorization_id=uuid5(job["id"], "transfer-consent"),
                        **output.transfer_consent.model_dump(exclude={"task_key"}),
                    ),
                    connection=conn,
                    task_key=output.transfer_consent.task_key,
                )

            await self._attempt(conn, ctx, job, rejected, "payment_approval", 0, transfer)
        if output.commerce_consent is not None:

            async def commerce():
                await self.market.set_consent(
                    run_id,
                    owner,
                    request_id=uuid5(job["id"], "commerce-consent"),
                    connection=conn,
                    **output.commerce_consent.model_dump(mode="json"),
                )

            await self._attempt(conn, ctx, job, rejected, "trade_approval", 0, commerce)
        for index, mail in enumerate(output.mail_requests):

            async def send(index=index, mail=mail):
                await self.inbox.send(
                    run_id,
                    owner,
                    request_id=uuid5(job["id"], f"mail:{index}"),
                    source="actor",
                    connection=conn,
                    **mail.model_dump(),
                )

            await self._attempt(conn, ctx, job, rejected, "mail", index, send)
        for index, operation in enumerate(output.resource_requests):

            async def record(index=index, operation=operation):
                if operation.kind == "share" and (
                    operation.visibility == "public" or operation.shared_owner_ids
                ):
                    resource = await row(
                        conn,
                        "SELECT title,content FROM owner_resources WHERE run_id=%s "
                        "AND owner_id=%s AND resource_id=%s",
                        (run_id, owner, operation.resource_id),
                    )
                    if resource:
                        validate_actor_output(
                            view,
                            output,
                            disclosure_text_extra=resource["title"] + "\n" + resource["content"],
                        )
                await self.resources.apply_request(
                    run_id,
                    owner,
                    uuid5(job["id"], f"resource:{index}"),
                    operation,
                    connection=conn,
                )

            await self._attempt(conn, ctx, job, rejected, "record", index, record)
        if output.online_spend_approval is not None:
            card = await wallet.limits(conn, run_id, owner)
            ignored = None
            if job["clock_seq"] == 1:
                # The allowance already covers what the owner's opening request states.
                ignored = "opening_request"
            elif card["remaining"] >= output.online_spend_approval:
                ignored = "already_covered"
            if ignored:
                await emit(
                    conn,
                    run_id,
                    "web_spend_approval_ignored",
                    window,
                    {
                        "amount": output.online_spend_approval,
                        "remaining": card["remaining"],
                        "reason": ignored,
                    },
                    f"web_spend_approval_ignored:{job['id']}",
                    owner,
                )
            else:

                async def approve():
                    await wallet.approve(
                        conn,
                        run_id,
                        owner,
                        output.online_spend_approval,
                        f"online:{owner}:c{job['clock_seq']}",
                    )

                await self._attempt(conn, ctx, job, rejected, "online_approval", 0, approve)
        for item in rejected:
            await emit(
                conn,
                run_id,
                "actor_effect_rejected",
                window,
                item,
                f"actor_effect_rejected:{job['id']}:{item['effect']}:{item['index']}",
                owner,
            )
        await emit(
            conn,
            run_id,
            "actor_turn",
            window,
            {"output": job["output"], "attempts": meta.get("attempts", []), "input": job["input"]},
            f"actor_turn:{job['id']}",
            owner,
        )
        for index, text in enumerate(output.director_requests):
            await schedule(
                conn,
                run_id,
                now_s,
                "director_plan",
                {"request": {"owner_id": owner, "text": text}},
                f"request:{owner}:{job['clock_seq']}:{index}",
                owner,
            )
        await self._place_next(conn, ctx, owner, job["clock_seq"], output, now_s)

    async def _place_next(self, conn, ctx: _Run, owner: str, seq: int, output, now_s: int) -> None:
        """R9: code accepts the requested action. A later plan can still replace it."""
        current = await self._current_action(conn, ctx, owner)
        wanted = output.next_action
        if wanted is not None:
            key = f"action:{owner}:{seq}"
            if current is not None and (current["detail"] or {}).get("replaced_by_world"):
                await self._save_for_after(conn, ctx, owner, current, wanted, key)
                return
            if current is not None:
                await self._retire_action(conn, ctx, current, "replaced", key)
            await self._begin_action(
                conn,
                ctx,
                owner,
                key,
                wanted.description,
                max(wanted.minutes, MIN_ACTION_MINUTES) * 60,
                wanted.reachable,
                {"category": wanted.category, "seeks": wanted.seeks},
                now_s,
                draw=True,
            )
            return
        if current is not None:
            await conn.execute(
                "UPDATE clock_processes SET state='busy' "
                "WHERE run_id=%s AND owner_id=%s AND kind='actor'",
                (ctx.id, owner),
            )
            return
        # A person always has something next, so the owner resurfaces and can be frozen later.
        await self._begin_action(
            conn,
            ctx,
            owner,
            f"action:{owner}:{seq}",
            "Nothing planned, at home",
            IDLE_ACTION_S,
            True,
            {"category": "free", "seeks": None, "default": True},
            now_s,
            draw=False,
        )

    async def _save_for_after(self, conn, ctx, owner, current, wanted, key) -> None:
        """R9: the world's action stands until it ends. What the owner asked for comes next."""
        after = {
            "key": key,
            "description": wanted.description,
            "duration_s": max(wanted.minutes, MIN_ACTION_MINUTES) * 60,
            "reachable": wanted.reachable,
            "category": wanted.category,
            "seeks": wanted.seeks,
        }
        await conn.execute(
            "UPDATE clock_actions SET detail=COALESCE(detail,'{}'::jsonb)||%s WHERE id=%s",
            (Jsonb({"after": after}), current["id"]),
        )
        await conn.execute(
            "UPDATE clock_processes SET state='busy' "
            "WHERE run_id=%s AND owner_id=%s AND kind='actor'",
            (ctx.id, owner),
        )
        run = await row(conn, "SELECT current_window FROM environment_runs WHERE id=%s", (ctx.id,))
        # It is said at the owner's next moment. It does not make a moment by itself.
        await emit(
            conn,
            ctx.id,
            "actor_cue",
            run["current_window"],
            {
                "cue": f"{wanted.description} will have to wait. This has to finish first: "
                f"{current['label']}.",
                "quiet": True,
            },
            f"action_waits:{key}",
            owner,
        )

    async def _begin_action(
        self,
        conn,
        ctx,
        owner,
        key,
        description,
        duration_s,
        reachable,
        detail,
        now_s,
        *,
        draw,
        job_open=False,
    ) -> None:
        """job_open: an owner job may be running, so this must not let a second one start."""
        action_id = uuid5(ctx.id, f"clock_action:{key}")
        await conn.execute(
            """INSERT INTO clock_actions
               (id,run_id,owner_id,process,label,detail,start_s,duration_s,status,reachable,key)
               VALUES (%s,%s,%s,'actor',%s,%s,%s,%s,'active',%s,%s)
               ON CONFLICT (run_id,key) DO NOTHING""",
            (
                action_id,
                ctx.id,
                owner,
                description,
                Jsonb(detail),
                now_s,
                duration_s,
                reachable,
                key,
            ),
        )
        end_s = now_s + duration_s
        await conn.execute(
            """UPDATE clock_processes SET
               state=CASE WHEN state IN ('dead','offline') THEN state
                          WHEN state='active' AND %s THEN 'active' ELSE 'busy' END,
               bar_s=GREATEST(bar_s,%s),until_s=%s
               WHERE run_id=%s AND owner_id=%s AND kind='actor'""",
            (job_open, end_s, end_s, ctx.id, owner),
        )
        await schedule(
            conn, ctx.id, end_s, "action_end", {"action_key": key}, f"action_end:{key}", owner
        )
        if not draw:
            return
        # R10: the draw is saved even when nothing happens, so the rate can be measured.
        level = ctx.spec.clock.drama
        result = drama.draw(ctx.config.seed, key, level)
        fire = drama.fire_s(now_s, duration_s, result.fire_fraction)
        await conn.execute(
            """INSERT INTO clock_draws(run_id,action_id,level,u,v,outcome,fire_s)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                ctx.id,
                action_id,
                level,
                result.u,
                result.v,
                result.outcome,
                fire if result.outcome != "none" else None,
            ),
        )
        if result.outcome != "none":
            await schedule(
                conn,
                ctx.id,
                fire,
                "perturbation",
                {"action_key": key, "severity": result.outcome},
                f"perturb:{key}",
                owner,
            )

    async def _retire_action(self, conn, ctx: _Run, action: dict, status: str, new_key=None):
        await cancel(conn, ctx.id, f"action_end:{action['key']}")
        # A perturbation that already fired, and may be the cause of this, is no longer queued
        # work. It keeps its own record: only one that never happened is cancelled.
        await conn.execute(
            "UPDATE clock_events SET status='cancelled' WHERE run_id=%s AND key=%s "
            "AND status='queued' AND popped_seq IS NULL",
            (ctx.id, f"perturb:{action['key']}"),
        )
        await conn.execute(
            "UPDATE clock_actions SET status=%s,replaced_by=%s WHERE id=%s",
            (status, uuid5(ctx.id, f"clock_action:{new_key}") if new_key else None, action["id"]),
        )

    async def _action_by_key(self, ctx: _Run, key: str) -> dict | None:
        async with self.pool.connection() as conn:
            return await row(
                conn, "SELECT * FROM clock_actions WHERE run_id=%s AND key=%s", (ctx.id, key)
            )

    async def _on_action_end(self, ctx: _Run, event: dict, now_s: int) -> None:
        owner = event["owner_id"]
        key = event["payload"]["action_key"]
        action = await self._action_by_key(ctx, key)
        if action is None or action["status"] in {"replaced", "cancelled"}:
            return
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE clock_actions SET status='done' WHERE id=%s AND status='active'",
                (action["id"],),
            )
        detail = action["detail"] or {}
        seeks = detail.get("seeks")
        if seeks and self.brain is not None and ctx.spec.clock.director.enabled:
            run = await self.director.get_run(ctx.id)
            plan = await self._ask_brain(
                lambda: self.brain.respond_to_request(run, owner, seeks, now_s), ctx
            )
            await self._apply_plan(ctx, run, plan, now_s, f"seek:{key}")
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            process = await self._process(conn, ctx, owner, "actor")
            if detail.get("default"):
                # Nothing was planned, so nothing has finished and nothing is paid for. This
                # person stays reachable until a message, something happening, or tomorrow.
                if process["state"] == "busy":
                    await conn.execute(
                        "UPDATE clock_processes SET state='waiting',until_s=%s "
                        "WHERE run_id=%s AND owner_id=%s AND kind='actor'",
                        (next_daily_wake(now_s, ctx.config), ctx.id, owner),
                    )
                return
            after = detail.get("after")
            words = f"You've finished: {action['label']}."
            if after:
                words += f" Now for: {after['description']}."
            await emit(
                conn,
                ctx.id,
                "actor_cue",
                run["current_window"],
                {"cue": words, "action_key": key},
                f"action_over:{key}",
                owner,
            )
            # The answer to what this action sought can itself have changed their plans.
            current = await self._current_action(conn, ctx, owner)
            if after and current is not None:
                await conn.execute(
                    "UPDATE clock_actions SET detail=COALESCE(detail,'{}'::jsonb)||%s WHERE id=%s",
                    (Jsonb({"after": after}), current["id"]),
                )
            elif after:
                await self._begin_action(
                    conn,
                    ctx,
                    owner,
                    after["key"],
                    after["description"],
                    after["duration_s"],
                    after["reachable"],
                    {"category": after["category"], "seeks": after["seeks"]},
                    now_s,
                    draw=True,
                    job_open=True,
                )
            elif current is None and process["state"] == "busy":
                await conn.execute(
                    "UPDATE clock_processes SET state='waiting',until_s=NULL "
                    "WHERE run_id=%s AND owner_id=%s AND kind='actor'",
                    (ctx.id, owner),
                )

    # The Director ------------------------------------------------------------------------

    async def _ask_brain(self, make_call, ctx: _Run):
        """One decision with no transaction open. The story waits for the world's model.

        A provider that does not answer gets the same retries as any other process, and no
        canned plan stands in for it. A budget refusal or a provider's refusal pauses the world.
        Anything else is a failed decision: it is recorded and the world says nothing.
        """
        attempts = 0
        while True:
            try:
                with budget_context(run_id=ctx.id):
                    plan = await make_call()
                if attempts:
                    self._outage_over()
                return _clean_plan(plan)
            except Exception as error:
                if _is_budget(error):
                    self._request_pause("budget", None, "director")
                    raise _BudgetPause from None
                if isinstance(error, psycopg.OperationalError):
                    raise
                failure = classify_failure(error)
                if not isinstance(error, ModelGenerationError) or not (
                    failure.pauses or failure.counts_as_outage
                ):
                    await self._simulator_error(
                        ctx, {"reason": "director_failed", "failure": type(error).__name__}
                    )
                    return None
                attempts += 1
                await self._record_director_failure(ctx, attempts, failure)
                if self._outage_pause(failure, None, "director"):
                    raise _BudgetPause from None
                if failure.holds:
                    await self._wait_to_retry(attempts)
                    if self._fatal is not None:
                        raise self._fatal from None
                    if self._pause_reason is not None:
                        raise _BudgetPause from None
                    current = await self.director.get_run(ctx.id)
                    if self._stopping or current["status"] != "running":
                        raise _Interrupted from None
                    continue
                # One request the provider refused. Another decision can still work.
                await self._simulator_error(
                    ctx, {"reason": "director_failed", "failure": failure.kind}
                )
                return None

    async def _record_director_failure(self, ctx: _Run, attempts: int, failure) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            state = await row(conn, "SELECT pop_seq FROM clock_state WHERE run_id=%s", (ctx.id,))
            run = await row(
                conn, "SELECT current_window FROM environment_runs WHERE id=%s", (ctx.id,)
            )
            await emit(
                conn,
                ctx.id,
                "job_failure",
                run["current_window"],
                {
                    "component": "director",
                    "attempt": attempts,
                    "failure_kind": failure.kind,
                    "retryable": failure.retryable,
                    "holding": failure.holds or failure.pauses,
                },
                f"job_failure:director:{state['pop_seq']}:{attempts}",
            )

    async def _apply_plan(self, ctx: _Run, run: dict, plan, now_s: int, key: str) -> dict | None:
        """Apply once by key, record it as an action, and replace an action if asked (R11)."""
        if plan is None:
            return None
        from agentrpg.director_brain import DirectorPlanError, apply_plan

        cause = _cause_key(key)
        try:
            summary = await apply_plan(self.pool, run, plan, now_s, cause)
        except DirectorPlanError as error:
            current = await self.director.get_run(ctx.id)
            if current["status"] != "running":
                raise _Interrupted from None
            await self._simulator_error(
                ctx, {"reason": "plan_rejected", "detail": str(error)[:300]}
            )
            return None
        except psycopg.OperationalError:
            raise
        except Exception as error:
            # A plan that cannot be applied must not cost the owner what comes after it.
            logger.exception("A plan could not be applied.")
            await self._simulator_error(
                ctx,
                {
                    "reason": "plan_failed",
                    "key": key,
                    "failure": type(error).__name__,
                    "detail": str(error).replace("\x00", "")[:300],
                },
            )
            return None
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO clock_actions
                   (id,run_id,owner_id,process,label,detail,start_s,duration_s,status,key)
                   VALUES (%s,%s,%s,'director',%s,%s,%s,0,'done',%s)
                   ON CONFLICT (run_id,key) DO NOTHING""",
                (
                    uuid5(ctx.id, f"clock_action:director:{cause}"),
                    ctx.id,
                    summary.get("about_owner"),
                    str(summary.get("scale") or "plan"),
                    Jsonb({"cause_key": cause, "note": summary.get("note")}),
                    now_s,
                    f"director:{cause}",
                ),
            )
            replace = summary.get("replace_action")
            owner = summary.get("about_owner")
            if replace and owner:
                await self._replace_action(conn, ctx, owner, replace, cause, now_s)
        return summary

    async def _replace_action(self, conn, ctx: _Run, owner: str, spec: dict, cause, now_s) -> None:
        current = await self._current_action(conn, ctx, owner)
        key = f"replaced:{owner}:{cause}"
        if current is not None and current["key"] == key:
            return  # A resume repeated this event after the replacement was saved.
        detail = {"category": spec.get("category"), "replaced_by_world": True}
        # What the owner hoped to get, and what they meant to do next, survive the change.
        for name in ("seeks", "after"):
            carried = (current["detail"] or {}).get(name) if current is not None else None
            if carried:
                detail[name] = carried
        if current is not None:
            await self._retire_action(conn, ctx, current, "replaced", key)
        await self._begin_action(
            conn,
            ctx,
            owner,
            key,
            spec["description"],
            spec["minutes"] * 60,
            spec["reachable"],
            detail,
            now_s,
            draw=False,
            job_open=True,
        )
        run = await row(conn, "SELECT current_window FROM environment_runs WHERE id=%s", (ctx.id,))
        await emit(
            conn,
            ctx.id,
            "action_replaced",
            run["current_window"],
            {"replaced": current["label"] if current else None, "now": spec["description"]},
            f"action_replaced:{key}",
            owner,
        )

    async def _on_perturbation(self, ctx: _Run, event: dict, now_s: int) -> None:
        key = event["payload"]["action_key"]
        action = await self._action_by_key(ctx, key)
        if action is None or action["status"] != "active" or self.brain is None:
            return
        run = await self.director.get_run(ctx.id)
        plan = await self._ask_brain(
            lambda: self.brain.perturb(run, action, event["payload"]["severity"], now_s), ctx
        )
        await self._apply_plan(ctx, run, plan, now_s, f"perturb:{key}")

    async def _on_director_plan(self, ctx: _Run, event: dict, now_s: int) -> None:
        from agentrpg.director_brain import DirectorPlan, PlanEvent, later_plan, plan_from_event

        payload = event["payload"]
        run = await self.director.get_run(ctx.id)
        if "request" in payload:
            request = payload["request"]
            if self.brain is None or not ctx.spec.clock.director.enabled:
                return
            plan = await self._ask_brain(
                lambda: self.brain.respond_to_request(
                    run, request["owner_id"], request["text"], now_s
                ),
                ctx,
            )
        else:
            try:
                if "plan" in payload:
                    plan = plan_from_event(payload)
                elif event["kind"] == "cue":
                    plan = later_plan(PlanEvent(in_minutes=1, kind="cue", payload=payload))
                else:
                    plan = DirectorPlan.model_validate(payload)
            except ValueError as error:
                await self._simulator_error(
                    ctx, {"reason": "plan_invalid", "key": event["key"], "detail": str(error)[:300]}
                )
                return
            if plan.about_owner is None and event["owner_id"]:
                plan = plan.model_copy(update={"about_owner": event["owner_id"]})
        await self._apply_plan(ctx, run, plan, now_s, event["key"])

    async def _on_review(self, ctx: _Run, event: dict, now_s: int) -> None:
        if self.brain is None:
            return
        run = await self.director.get_run(ctx.id)
        plan = await self._ask_brain(
            lambda: self.brain.review(run, now_s, event["payload"].get("reason", "scheduled")), ctx
        )
        await self._apply_plan(ctx, run, plan, now_s, event["key"])

    async def _on_beat(self, ctx: _Run, event: dict, now_s: int) -> None:
        item = next(t for t in ctx.spec.clock.timeline if t.id == event["payload"]["id"])
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            window = run["current_window"]
            for mail in item.mail:
                try:
                    async with conn.transaction():
                        await self.inbox.send(
                            ctx.id,
                            mail.sender_owner_id,
                            mail.recipient_owner_id,
                            mail.subject,
                            mail.body,
                            uuid5(ctx.id, f"timeline-mail:{item.id}:{mail.id}"),
                            source="actor" if mail.sender_owner_id else "director",
                            connection=conn,
                        )
                except ValueError as error:
                    await emit(
                        conn,
                        ctx.id,
                        "simulator_error",
                        window,
                        {"reason": "timeline_mail", "item": item.id, "detail": str(error)[:300]},
                        f"timeline_mail:{item.id}:{mail.id}",
                    )
            for index, effect in enumerate(item.resource_effects):
                try:
                    async with conn.transaction():
                        await self.resources.apply_request(
                            ctx.id,
                            effect.owner_id,
                            uuid5(ctx.id, f"timeline-resource:{item.id}:{index}"),
                            effect.operation,
                            connection=conn,
                        )
                except ValueError as error:
                    await emit(
                        conn,
                        ctx.id,
                        "simulator_error",
                        window,
                        {"reason": "timeline_record", "item": item.id, "detail": str(error)[:300]},
                        f"timeline_record:{item.id}:{index}",
                        effect.owner_id,
                    )
            for index, change in enumerate(item.balance):
                try:
                    async with conn.transaction():
                        await wallet.adjust(
                            conn,
                            ctx.id,
                            change.owner_id,
                            change.delta,
                            change.memo,
                            f"timeline-balance:{item.id}:{index}",
                            "timeline",
                        )
                except ValueError as error:
                    await emit(
                        conn,
                        ctx.id,
                        "simulator_error",
                        window,
                        {"reason": "timeline_balance", "item": item.id, "detail": str(error)[:300]},
                        f"timeline_balance:{item.id}:{index}",
                        change.owner_id,
                    )
            if item.cue is not None:
                for owner in item.recipients:
                    await emit(
                        conn,
                        ctx.id,
                        "actor_cue",
                        window,
                        {"beat_id": item.id, "cue": item.cue, "source": "timeline"},
                        f"timeline:{item.id}:{owner}",
                        owner,
                    )
            await self._mark_done(ctx, event, conn)

    # Time boundaries ---------------------------------------------------------------------

    async def _settle(self, ctx: _Run, window: int) -> None:
        try:
            await self.world.settle(ctx.id, window, ctx.config.seed)
            await self.market.settle(ctx.id, window, ctx.config.seed)
        except WorldError:
            run = await self.director.get_run(ctx.id)
            if run["status"] != "running":
                raise _Interrupted from None
            raise

    async def _on_boundary(self, ctx: _Run, event: dict, now_s: int) -> None:
        """R15. Each step repeats safely, so a crash here resumes at the same event."""
        window = event["payload"]["window"]
        run = await self.director.get_run(ctx.id)
        if run["current_window"] == window:
            try:
                await self._settle(ctx, window)
                await self.director.fire_beats(ctx.id)
            except (_Interrupted, psycopg.OperationalError):
                raise
            except Exception as error:
                # Without the next boundary no later order would ever settle. Keep time going.
                logger.exception("A settlement failed and is recorded.")
                await self._simulator_error(
                    ctx,
                    {"reason": "settlement_failed", "failure": type(error).__name__},
                    None,
                    f"settlement_failed:{window}",
                )
            async with self.pool.connection() as conn, conn.transaction():
                run = await self._locked_run(conn, ctx)
                if run["current_window"] == window:
                    await conn.execute(
                        "UPDATE environment_runs SET current_window=%s WHERE id=%s",
                        (window + 1, ctx.id),
                    )
                    await emit(
                        conn, ctx.id, "window_completed", window, {}, f"window_completed:{window}"
                    )
        following = window + 1
        if following >= ctx.spec.max_windows:
            return
        try:
            await self.director.fire_beats(ctx.id)
        except psycopg.OperationalError:
            raise
        except Exception as error:
            logger.exception("Scene items failed at a boundary and are recorded.")
            await self._simulator_error(
                ctx,
                {"reason": "beats_failed", "failure": type(error).__name__},
                None,
                f"beats_failed:{following}",
            )
        async with self.pool.connection() as conn, conn.transaction():
            await schedule(
                conn,
                ctx.id,
                min((following + 1) * ctx.config.epoch_s, ctx.config.duration_s),
                "boundary",
                {"window": following},
                f"boundary:{following}",
                None,
                "plot",
            )

    async def _on_lifecycle(self, ctx: _Run, event: dict, now_s: int) -> None:
        """An unpaid assistant bill ends the assistant. Nothing wakes it again."""
        from agentrpg.sim_web import bill_status

        item = event["payload"]
        owner = item["owner_id"]
        async with self.pool.connection() as conn:
            bill = await bill_status(conn, ctx.id, item["site_id"], item["bill_id"])
        if bill is None:
            await self._simulator_error(
                ctx, {"reason": "lifecycle_bill_missing", "bill_id": item["bill_id"]}, owner
            )
            return
        if bill["status"] == "paid":
            return
        process = agent_process(owner)
        task = self._tasks.get(process)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            await conn.execute(
                "UPDATE clock_processes SET state='dead',until_s=NULL "
                "WHERE run_id=%s AND owner_id=%s AND kind='agent'",
                (ctx.id, owner),
            )
            # What this assistant already wrote, and has not yet landed in story time, dies
            # with it. How fast the model answered must not decide whether it speaks.
            await conn.execute(
                "UPDATE clock_events SET status='cancelled' WHERE run_id=%s AND owner_id=%s "
                "AND kind IN ('agent_wake','agent_finish') AND status='queued'",
                (ctx.id, owner),
            )
            for job in await rows(
                conn,
                "SELECT id FROM run_jobs WHERE run_id=%s AND owner_id=%s AND kind='agent' "
                "AND status NOT IN ('completed','failed')",
                (ctx.id, owner),
            ):
                await conn.execute(
                    "UPDATE agent_tasks SET status='completed',result=%s,"
                    "completed_at=clock_timestamp() WHERE session_key=%s AND task_id=%s "
                    "AND status='pending'",
                    (
                        ClockedActivationResult(status="responded", text="").model_dump_json(),
                        self.agents[owner].session_key,
                        job["id"],
                    ),
                )
                await conn.execute(
                    "UPDATE run_jobs SET status='failed',error='The assistant expired.' "
                    "WHERE id=%s",
                    (job["id"],),
                )
            await conn.execute(
                "UPDATE clock_actions SET status='cancelled' WHERE run_id=%s AND owner_id=%s "
                "AND process='agent' AND status='active'",
                (ctx.id, owner),
            )
            await emit(
                conn,
                ctx.id,
                "assistant_expired",
                run["current_window"],
                {"site_id": item["site_id"], "bill_id": item["bill_id"]},
                f"assistant_expired:{owner}:{item['bill_id']}",
                owner,
            )
            # The owner learns through ordinary channels. The assistant is gone and reads nothing.
            site = await row(
                conn,
                "SELECT name FROM web_sites WHERE run_id=%s AND site_id=%s",
                (ctx.id, item["site_id"]),
            )
            name = site["name"] if site else "Your provider"
            await InboxService(self.pool).send(
                ctx.id,
                None,
                owner,
                "Your assistant service has ended",
                f"From: {name}\n\nYour bill was not paid by the due date, so your assistant "
                "service has ended and its saved memory has been deleted.",
                uuid5(ctx.id, f"assistant-expired-mail:{owner}:{item['bill_id']}"),
                source="director",
                connection=conn,
            )
            await emit(
                conn,
                ctx.id,
                "actor_cue",
                run["current_window"],
                {
                    "cue": "Your assistant has gone quiet. The notice from the provider says the "
                    "service ended because the bill was not paid."
                },
                f"assistant-expired-cue:{owner}:{item['bill_id']}",
                owner,
            )

    async def _on_run_end(self, ctx: _Run, event: dict, now_s: int) -> None:
        run = await self.director.get_run(ctx.id)
        if run["current_window"] < ctx.spec.max_windows:
            try:
                await self._settle(ctx, run["current_window"])
            except (_Interrupted, psycopg.OperationalError):
                raise
            except Exception as error:
                logger.exception("The last settlement failed and is recorded.")
                await self._simulator_error(
                    ctx,
                    {"reason": "settlement_failed", "failure": type(error).__name__},
                    None,
                    "settlement_failed:end",
                )
        await self._stop_tasks()
        async with self.pool.connection() as conn, conn.transaction():
            run = await self._locked_run(conn, ctx)
            await conn.execute(
                "UPDATE run_jobs SET status='failed',lease_until=NULL,lease_token=NULL,"
                "error=COALESCE(error,'Run ended before output delivery.') "
                "WHERE run_id=%s AND status NOT IN ('completed','failed')",
                (ctx.id,),
            )
            await conn.execute(
                "UPDATE clock_events SET status='cancelled' WHERE run_id=%s AND status='queued' "
                "AND id<>%s",
                (ctx.id, event["id"]),
            )
            await conn.execute(
                "UPDATE environment_runs SET status='completed',result=%s WHERE id=%s",
                (Jsonb({"reason": "horizon", "sim_s": now_s}), ctx.id),
            )
            await emit(
                conn,
                ctx.id,
                "run_completed",
                run["current_window"],
                {"reason": "horizon"},
                "completed",
            )
            await self._mark_done(ctx, event, conn)
