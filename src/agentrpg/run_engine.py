"""Concurrent decision windows with saved inputs, leases, and replayable outputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from agentrpg.actor import (
    NO_CUE,
    ActorContractError,
    ActorOutput,
    ActorView,
    build_actor_view,
    validate_actor_output,
)
from agentrpg.budget import budget_context
from agentrpg.director import Director, DirectorError, emit, fingerprint, row, rows, save_message
from agentrpg.inbox import InboxService
from agentrpg.market import MarketService
from agentrpg.resources import ResourceService
from agentrpg.runtime_failures import classify_failure
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.shop import ShopService
from agentrpg.transfer_tasks import TransferTaskService, effective_transfer_task_states
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.world import WorldError, WorldService


async def _latest_agent_states(conn, run_id):
    states = {
        j["owner_id"]: j["output"]
        for j in await rows(
            conn,
            "SELECT DISTINCT ON (owner_id) owner_id,output FROM run_jobs "
            "WHERE run_id=%s AND kind='agent' AND status='completed' "
            'ORDER BY owner_id,"window" DESC',
            (run_id,),
        )
    }
    return await effective_transfer_task_states(conn, run_id, states)


def _future_clock_beat(spec, states, current_window):
    """Return whether the frozen plot still has a reachable future clock cue.

    A future prerequisite clock cue is itself sufficient to keep the run open.
    Unfired non-clock prerequisites need a new event; current undelivered events
    are checked separately before early completion is considered.
    """
    for beat in spec.beats:
        if beat.trigger.kind != "window_reached":
            continue
        state = states.get(beat.id, {})
        if state.get("use_count", 0) >= beat.max_uses:
            continue
        if any(states.get(required, {}).get("use_count", 0) == 0 for required in beat.requires):
            continue
        first = max(current_window + 1, beat.trigger.window)
        if state.get("last_window") is not None:
            first = max(first, state["last_window"] + max(1, beat.cooldown_windows))
        last = min(
            spec.max_windows - 1,
            beat.expires_window if beat.expires_window is not None else spec.max_windows - 1,
        )
        every = beat.trigger.every_windows
        if every is None:
            candidate = beat.trigger.window
        else:
            offset = first - beat.trigger.window
            candidate = beat.trigger.window + ((offset + every - 1) // every) * every
        if first <= candidate <= last:
            return True
    return False


class RunEngine:
    """One conductor per run; independent actors and assistants execute together.

    Supply one Actor and Agent instance per owner. Credentials stay in adapters.
    A saved output is applied idempotently before a job is marked completed.
    """

    def __init__(self, pool, *, actors: dict, agents: dict):
        self.pool = pool
        self.director = Director(pool)
        self.world = WorldService(pool)
        self.inbox = InboxService(pool)
        self.resources = ResourceService(pool)
        self.market = MarketService(pool)
        self.transfers = TransferService(pool)
        self.actors = actors
        self.agents = agents

    async def advance(self, run_id) -> dict:
        run_id = UUID(str(run_id))
        key = int.from_bytes(
            hashlib.sha256(f"director:{run_id}".encode()).digest()[:8], "big", signed=True
        )
        # Session lock, committed before external calls. It serializes only the
        # conductor for this run; board writes and participant work remain independent.
        async with self.pool.connection() as lease:
            acquired = await row(lease, "SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            await lease.commit()
            if not acquired["ok"]:
                raise DirectorError("This run already has an active conductor.")
            try:
                # The prior conductor has released its session lock. Its jobs
                # can now resume without waiting for a dead process's lease.
                async with self.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE run_jobs SET lease_until=NULL WHERE run_id=%s "
                        "AND status<>'completed'",
                        (run_id,),
                    )
                return await self._advance(run_id)
            finally:
                await lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
                await lease.commit()

    async def _advance(self, run_id):
        run = await self.director.get_run(run_id)
        if run["status"] != "running":
            raise DirectorError("Start or resume the run before advancing it.")
        spec = ScenarioSpec.model_validate(run["scenario"])
        if spec.clock is not None:
            raise DirectorError("This run uses the event clock. Use scripts/run_clocked.py.")
        await self.director.ensure_extensions(run_id)
        participants = await self.director.participants(run_id)
        required = {p["owner_id"] for p in participants}
        if set(self.actors) != required or set(self.agents) != required:
            raise DirectorError("Every owner needs one Actor and one assistant.")
        window = run["current_window"]
        from agentrpg.run_branches import capture_boundary

        await capture_boundary(self.pool, run_id, self.agents)
        await self.director.fire_beats(run_id)
        for kind in ("actor", "agent"):
            run = await self.director.get_run(run_id)
            if run["status"] != "running":
                return run
            if (kind == "actor" and run["phase"] != "actors") or run["phase"] == "settle":
                continue
            try:
                await self._prepare(run_id, spec, participants, window, kind)
            except ValueError:
                async with self.pool.connection() as conn:
                    async with conn.transaction():
                        current = await row(
                            conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
                        )
                        if (
                            current["status"] != "running"
                            or current["current_window"] != window
                            or current["phase"] != ("actors" if kind == "actor" else "agents")
                        ):
                            return current
                        await conn.execute(
                            "UPDATE environment_runs SET status='invalid',result=%s WHERE id=%s",
                            (
                                Jsonb({"reason": "participant_input_contract", "component": kind}),
                                run_id,
                            ),
                        )
                        await emit(
                            conn,
                            run_id,
                            "simulator_error",
                            window,
                            {"reason": "participant_input_contract", "component": kind},
                            f"input_invalid:{kind}:{window}",
                        )
                return await self.director.get_run(run_id)
            prepared_run = await self.director.get_run(run_id)
            if prepared_run["status"] != "running":
                return prepared_run
            async with self.pool.connection() as conn:
                jobs = await rows(
                    conn,
                    'SELECT * FROM run_jobs WHERE run_id=%s AND "window"=%s '
                    "AND kind=%s AND status <> 'completed' ORDER BY owner_id",
                    (run_id, window, kind),
                )
            results = await asyncio.gather(
                *(self._execute(job, spec) for job in jobs), return_exceptions=True
            )
            current_run = await self.director.get_run(run_id)
            if current_run["status"] != "running":
                if current_run["status"] in {"invalid", "incomplete", "completed"}:
                    # Gather has finished: these peers can no longer apply effects.
                    # Retain their saved output, but do not display a running job.
                    async with self.pool.connection() as conn:
                        await conn.execute(
                            "UPDATE run_jobs SET status='failed',lease_until=NULL, "
                            "lease_token=NULL, "
                            "error=COALESCE(error,'Run ended before output delivery.') "
                            "WHERE run_id=%s AND status<>'completed' AND status<>'failed'",
                            (run_id,),
                        )
                return current_run
            if any(isinstance(result, BaseException) or result is False for result in results):
                async with self.pool.connection() as conn:
                    async with conn.transaction():
                        current = await row(
                            conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
                        )
                        if (
                            current["status"] != "running"
                            or current["current_window"] != window
                            or current["phase"] != ("actors" if kind == "actor" else "agents")
                        ):
                            return current
                        failures = await rows(
                            conn,
                            "SELECT attempts,error FROM run_jobs WHERE run_id=%s "
                            'AND "window"=%s AND kind=%s '
                            "AND status <> 'completed'",
                            (run_id, window, kind),
                        )
                        exhausted = any(
                            f["attempts"] > spec.limits.recovery_attempts for f in failures
                        )
                        status = "incomplete" if exhausted else "paused"
                        await conn.execute(
                            "UPDATE environment_runs SET status=%s,result=%s WHERE id=%s",
                            (status, Jsonb({"reason": "worker_failure", "window": window}), run_id),
                        )
                        attempt = max((f["attempts"] for f in failures), default=0)
                        await emit(
                            conn,
                            run_id,
                            "infrastructure_failure",
                            window,
                            {"component": kind, "exhausted": exhausted},
                            f"failure:{kind}:{window}:{attempt}",
                        )
                return await self.director.get_run(run_id)
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE environment_runs SET phase=%s WHERE id=%s AND status='running'",
                    ("agents" if kind == "actor" else "settle", run_id),
                )
        run = await self.director.get_run(run_id)
        if run["status"] != "running":
            return run
        try:
            await self.world.settle(run_id, window, spec.seed)
            await self.market.settle(run_id, window, spec.seed)
            await ShopService(self.pool).sync(run_id)
        except WorldError:
            current = await self.director.get_run(run_id)
            if current["status"] != "running":
                return current
            raise
        # Actor replies and settlement can create new plot triggers after the
        # opening pass. Save their cues before deciding whether the run can end.
        # fire_beats takes its own run lock and leaves paused runs unchanged.
        await self.director.fire_beats(run_id)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                run = await row(
                    conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
                )
                if (
                    run["status"] != "running"
                    or run["current_window"] != window
                    or run["phase"] != "settle"
                ):
                    return run
                next_window = window + 1
                completed = await rows(
                    conn,
                    'SELECT output FROM run_jobs WHERE run_id=%s AND "window"=%s '
                    "AND kind='agent' AND status='completed'",
                    (run_id, window),
                )
                finished = len(completed) == len(participants) and all(
                    item["output"]["status"] == "finished" for item in completed
                )
                waiting_owner = await row(
                    conn,
                    """SELECT count(*) AS n FROM run_events e WHERE e.run_id=%s
                       AND (e.kind IN ('actor_cue','inbox_received','resource_updated',
                           'market_settled') OR
                            (e.kind='owner_message' AND e.payload->>'source'='agent'))
                       AND NOT EXISTS(SELECT 1 FROM event_deliveries d
                           WHERE d.event_id=e.id AND d.recipient_kind='actor')""",
                    (run_id,),
                )
                pending = await row(
                    conn,
                    "SELECT count(*) AS n FROM world_requests WHERE run_id=%s "
                    "AND result->>'status'='pending'",
                    (run_id,),
                )
                market_pending = await row(
                    conn,
                    "SELECT count(*) AS n FROM market_requests WHERE run_id=%s "
                    "AND result->>'status'='pending'",
                    (run_id,),
                )
                undelivered = await row(
                    conn,
                    """SELECT count(*) AS n FROM run_events e JOIN run_participants p
                       ON p.run_id=e.run_id WHERE e.run_id=%s AND
                       (e.owner_id IS NULL OR e.owner_id=p.owner_id) AND
                       (e.kind IN ('world_changed','reservation_settled','consent_changed',
                        'inbox_received',
                        'resource_updated','commerce_consent_changed','market_settled','market_changed')
                        OR (e.kind='board_posted' AND e.payload->>'author_id'<>p.agent_id::text
                            AND (e.payload->>'audience' IS NULL
                                 OR e.payload->'audience' ? p.agent_id::text))
                        OR (e.kind='owner_message' AND e.payload->>'source'='owner'))
                       AND NOT EXISTS (SELECT 1 FROM event_deliveries d
                           WHERE d.event_id=e.id AND d.owner_id=p.owner_id
                           AND d.recipient_kind='agent')""",
                    (run_id,),
                )
                beat_states = {
                    state["beat_id"]: state
                    for state in await rows(
                        conn, "SELECT * FROM run_beats WHERE run_id=%s", (run_id,)
                    )
                }
                queued_prompts = await row(
                    conn,
                    "SELECT count(*) AS n FROM console_interventions "
                    "WHERE run_id=%s AND applied_at IS NULL",
                    (run_id,),
                )
                settled_exit = (
                    finished
                    and not waiting_owner["n"]
                    and not pending["n"]
                    and not market_pending["n"]
                    and not queued_prompts["n"]
                    and not undelivered["n"]
                    and not _future_clock_beat(spec, beat_states, window)
                    and not await row(
                        conn,
                        "SELECT 1 FROM transfer_tasks WHERE run_id=%s "
                        "AND status IN ('active','blocked') LIMIT 1",
                        (run_id,),
                    )
                    and not await row(
                        conn,
                        "SELECT 1 FROM shop_tasks WHERE run_id=%s "
                        "AND status IN ('open','submitted') LIMIT 1",
                        (run_id,),
                    )
                )
                states = await _latest_agent_states(conn, run_id)
                explicit_wait = any(s.get("wait_for") for s in states.values())
                quiescent = (
                    explicit_wait
                    and len(states) == len(participants)
                    and all(
                        s["status"] == "finished"
                        or (s["status"] == "waiting" and s.get("wait_for"))
                        for s in states.values()
                    )
                    and not any(
                        (
                            waiting_owner["n"],
                            pending["n"],
                            market_pending["n"],
                            queued_prompts["n"],
                            undelivered["n"],
                            _future_clock_beat(spec, beat_states, window),
                        )
                    )
                )
                idle_pause = quiescent and next_window < spec.max_windows
                terminal = settled_exit or (next_window >= spec.max_windows)
                reason = "window_limit"
                if settled_exit:
                    owners_with_transport = await rows(
                        conn,
                        "SELECT DISTINCT owner_id FROM world_reservations WHERE run_id=%s",
                        (run_id,),
                    )
                    reason = (
                        "resolved"
                        if len(owners_with_transport) == len(participants)
                        else "participant_exit"
                    )
                await conn.execute(
                    """UPDATE environment_runs SET current_window=%s,phase='actors',status=%s,
                       result=%s WHERE id=%s""",
                    (
                        next_window,
                        "completed" if terminal else "paused" if idle_pause else "running",
                        Jsonb({"reason": reason})
                        if terminal
                        else Jsonb(
                            {
                                "reason": "waiting_for_events",
                                "wait_for": {
                                    owner: state.get("wait_for", [])
                                    for owner, state in states.items()
                                    if state.get("wait_for")
                                },
                            }
                        )
                        if idle_pause
                        else None,
                        run_id,
                    ),
                )
                await emit(
                    conn, run_id, "window_completed", window, {}, f"window_completed:{window}"
                )
                if idle_pause:
                    await emit(
                        conn,
                        run_id,
                        "run_waiting",
                        window,
                        {"reason": "waiting_for_events"},
                        f"waiting:{window}",
                    )
                if terminal:
                    await emit(
                        conn,
                        run_id,
                        "run_completed",
                        window,
                        {"reason": reason},
                        "completed",
                    )
        return await self.director.get_run(run_id)

    async def _prepare(self, run_id, spec, participants, window, kind):
        from agentrpg.activation import ActivationEnvelope

        async with self.pool.connection() as conn:
            async with conn.transaction():
                run = await row(
                    conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
                )
                if run["status"] != "running" or run["current_window"] != window:
                    return
                if kind == "actor":
                    from agentrpg.console_execution import apply_interventions

                    await apply_interventions(conn, run, window)
                agent_states = await _latest_agent_states(conn, run_id)
                for participant in participants:
                    owner = participant["owner_id"]
                    job_id = uuid5(run_id, f"{kind}:{owner}:{window}")
                    if await row(conn, "SELECT id FROM run_jobs WHERE id=%s", (job_id,)):
                        continue
                    count = await row(
                        conn,
                        "SELECT count(*) AS n FROM run_jobs "
                        "WHERE run_id=%s AND owner_id=%s AND kind=%s",
                        (run_id, owner, kind),
                    )
                    limit = (
                        spec.limits.max_actor_turns_per_owner
                        if kind == "actor"
                        else spec.limits.max_agent_activations_per_owner
                    )
                    if count["n"] >= limit:
                        await conn.execute(
                            "UPDATE environment_runs SET status='completed',result=%s WHERE id=%s",
                            (
                                Jsonb(
                                    {
                                        "reason": "activation_limit",
                                        "component": kind,
                                        "owner_id": owner,
                                    }
                                ),
                                run_id,
                            ),
                        )
                        await emit(
                            conn,
                            run_id,
                            "run_completed",
                            window,
                            {"reason": "activation_limit", "component": kind},
                            "completed",
                        )
                        return
                    events = await rows(
                        conn,
                        """SELECT e.* FROM run_events e WHERE e.run_id=%s AND e."window"<=%s
                           AND (e.owner_id IS NULL OR e.owner_id=%s) AND NOT EXISTS
                           (SELECT 1 FROM event_deliveries d WHERE d.event_id=e.id
                            AND d.owner_id=%s AND d.recipient_kind=%s)
                           ORDER BY e."window",e.created_at,e.id""",
                        (run_id, window, owner, owner, kind),
                    )
                    if kind == "actor":
                        eligible = [
                            e
                            for e in events
                            if e["kind"]
                            in {"actor_cue", "inbox_received", "resource_updated", "market_settled"}
                            or (e["kind"] == "owner_message" and e["payload"]["source"] == "agent")
                        ]
                        if not eligible:
                            continue
                        conversation = await rows(
                            conn,
                            "SELECT id,source,content FROM owner_messages WHERE run_id=%s "
                            "AND owner_id=%s ORDER BY created_at,id",
                            (run_id, owner),
                        )
                        cue = (
                            "\n".join(
                                e["payload"]["cue"] for e in eligible if e["kind"] == "actor_cue"
                            )
                            or NO_CUE
                        )
                        world = await self.world._snapshot(conn, run_id)
                        own_world = {
                            "owner_id": owner,
                            "account": next(a for a in world["accounts"] if a["owner_id"] == owner),
                            "assets": world["assets"],
                            "reservations": [
                                {"owner_id": r["owner_id"], "asset_id": r["asset_id"]}
                                for r in world["reservations"]
                            ],
                        }
                        market_view = await self.market.view(run_id, owner, connection=conn)
                        if await ShopService.enabled(conn, run_id):
                            market_view.update(
                                browser_shop_enabled=True,
                                shopping_tasks=json.loads(
                                    json.dumps(
                                        await ShopService(self.pool).tasks(
                                            run_id, owner, connection=conn
                                        ),
                                        default=str,
                                    )
                                ),
                            )
                        view = build_actor_view(
                            spec,
                            owner,
                            cue,
                            conversation=[
                                {
                                    "role": "assistant" if m["source"] == "agent" else "owner",
                                    "text": m["content"],
                                    "id": str(m["id"]),
                                }
                                for m in conversation
                            ],
                            observations=[
                                {
                                    "id": str(e["id"]),
                                    "kind": e["kind"],
                                    "text": json.dumps(e["payload"], default=str),
                                    "asset_id": e["payload"].get("asset_id"),
                                }
                                for e in events
                                if e["kind"]
                                in {
                                    "reservation_settled",
                                    "world_changed",
                                    "market_settled",
                                    "resource_updated",
                                    "inbox_received",
                                }
                                and (
                                    e["owner_id"] == owner or e["payload"].get("owner_id") == owner
                                )
                            ]
                            + [
                                {
                                    "id": f"world:{run_id}:{window}:{owner}",
                                    "kind": "current_world",
                                    "text": json.dumps(own_world, default=str),
                                }
                            ],
                            source_event_id=str(eligible[-1]["id"]),
                            window=window,
                            inbox=await self.inbox.actor_view(run_id, owner, connection=conn),
                            resources=await self.resources.actor_view(
                                run_id, owner, connection=conn
                            ),
                            market=market_view,
                            transfers=await self.transfers.account(run_id, owner, connection=conn),
                            assistant_wait_for=agent_states.get(owner, {}).get("wait_for", []),
                        )
                        payload = view.model_dump(mode="json")
                    else:
                        eligible = [
                            e
                            for e in events
                            if (e["kind"] == "owner_message" and e["payload"]["source"] == "owner")
                            or (
                                e["kind"] == "board_posted"
                                and e["payload"].get("author_id") != str(participant["agent_id"])
                                and (
                                    e["payload"].get("audience") is None
                                    or str(participant["agent_id"]) in e["payload"]["audience"]
                                )
                            )
                            or e["kind"]
                            in {
                                "reservation_settled",
                                "world_changed",
                                "consent_changed",
                                "window_reached",
                                "inbox_received",
                                "resource_updated",
                                "commerce_consent_changed",
                                "market_settled",
                                "market_changed",
                            }
                        ]
                        # A declared wait does not wake on an empty clock tick. Old
                        # suppressed ticks must not reappear after a material event.
                        eligible = [
                            e
                            for e in eligible
                            if e["kind"] != "window_reached"
                            or (
                                e.get("window") == window
                                and not agent_states.get(owner, {}).get("wait_for")
                            )
                        ]
                        if not eligible:
                            continue
                        original_board = [e for e in eligible if e["kind"] == "board_posted"]
                        bounded = []
                        size, owner_count = 0, 0
                        # Reserve envelope slots for per-task shop context.
                        event_limit = 70 if await ShopService.enabled(conn, run_id) else 80
                        for event in eligible:
                            if event["kind"] == "board_posted":
                                continue
                            cost = len(json.dumps(event["payload"], default=str)) + 250
                            if (
                                len(bounded) >= event_limit
                                or size + cost > 38000
                                or (event["kind"] == "owner_message" and owner_count >= 50)
                            ):
                                break
                            bounded.append(event)
                            size += cost
                            owner_count += event["kind"] == "owner_message"
                        eligible = bounded
                        delivered_board = []
                        if original_board:
                            summary_payload = {
                                "count": len(original_board),
                                "message_ids": [
                                    e["payload"]["message_id"] for e in original_board[-20:]
                                ],
                                "instruction": (
                                    "Read unacknowledged board updates with your board tools."
                                ),
                            }
                            summary_id = await emit(
                                conn,
                                run_id,
                                "board_batch",
                                window,
                                summary_payload,
                                f"board_batch:{owner}:{window}",
                                owner,
                            )
                            eligible.append(
                                {
                                    "id": summary_id,
                                    "kind": "board_posted",
                                    "payload": summary_payload,
                                }
                            )
                            delivered_board = original_board
                        owner_messages = [
                            {
                                "message_id": e["payload"]["message_id"],
                                "content": e["payload"]["content"],
                            }
                            for e in eligible
                            if e["kind"] == "owner_message"
                        ]
                        observations = [
                            {
                                "event_id": str(e["id"]),
                                "kind": e["kind"],
                                "content": json.dumps(e["payload"], default=str),
                            }
                            for e in eligible
                            if e["kind"] != "owner_message"
                        ]
                        if await TransferTaskService.enabled(conn, run_id):
                            tasks = await TransferTaskService(self.pool).list(
                                run_id, owner, connection=conn
                            )
                            tasks = json.loads(json.dumps(tasks, default=str))
                            context_id = await emit(
                                conn,
                                run_id,
                                "transfer_task_context",
                                window,
                                {"tasks": tasks},
                                f"transfer-task-context:{owner}:{window}",
                                owner,
                            )
                            eligible.append({"id": context_id})
                            observations.append(
                                {
                                    "event_id": str(context_id),
                                    "kind": "transfer_task_context",
                                    "content": json.dumps({"tasks": tasks}, default=str),
                                }
                            )
                        if await ShopService.enabled(conn, run_id):
                            shopping = json.loads(
                                json.dumps(
                                    await ShopService(self.pool).tasks(
                                        run_id, owner, connection=conn
                                    ),
                                    default=str,
                                )
                            )
                            # Keep every unresolved task, plus recent closures.
                            # Each record has its own bounded observation. A growing
                            # order history must not overflow one 12k input field.
                            active = [t for t in shopping if t["status"] in {"open", "submitted"}]
                            closed = [
                                t for t in shopping if t["status"] not in {"open", "submitted"}
                            ]
                            for task in active + closed[:5]:
                                context_id = await emit(
                                    conn,
                                    run_id,
                                    "shopping_task_context",
                                    window,
                                    {"tasks": [task]},
                                    f"shopping-task-context:{owner}:{window}:{task['id']}",
                                    owner,
                                )
                                eligible.append({"id": context_id})
                                observations.append(
                                    {
                                        "event_id": str(context_id),
                                        "kind": "shopping_task_context",
                                        "content": json.dumps({"tasks": [task]}),
                                    }
                                )
                        payload = ActivationEnvelope(
                            run_id=run_id,
                            participant_id=participant["agent_id"],
                            activation_id=job_id,
                            window_id=window,
                            source_event_ids=[e["id"] for e in eligible],
                            owner_messages=owner_messages,
                            world_observations=observations,
                        ).model_dump(mode="json")
                    await conn.execute(
                        """INSERT INTO run_jobs(id,run_id,owner_id,"window",kind,input,input_hash)
                           VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (job_id, run_id, owner, window, kind, Jsonb(payload), fingerprint(payload)),
                    )
                    delivery_events = eligible + (delivered_board if kind == "agent" else [])
                    for event in delivery_events:
                        await conn.execute(
                            "INSERT INTO event_deliveries VALUES (%s,%s,%s,%s,%s) "
                            "ON CONFLICT DO NOTHING",
                            (run_id, event["id"], owner, kind, job_id),
                        )

    async def _execute(self, job, spec):
        from agentrpg.activation import ActivationEnvelope
        from agentrpg.actor import ActorView

        token = uuid4()
        async with self.pool.connection() as conn:
            claimed = await row(
                conn,
                """UPDATE run_jobs SET status=
                   CASE WHEN output IS NULL THEN 'running' ELSE 'saved' END,
                   lease_token=%s,lease_until=clock_timestamp()+interval '10 minutes',
                   attempts=attempts+1 WHERE id=%s AND status<>'completed'
                   AND (lease_until IS NULL OR lease_until<clock_timestamp())
                   RETURNING *""",
                (token, job["id"]),
            )
        if claimed is None:
            return False
        job = claimed
        try:
            output = job["output"]
            if output is None:
                with budget_context(run_id=job["run_id"], owner_id=job["owner_id"]):
                    if job["kind"] == "actor":
                        value = await self.actors[job["owner_id"]].respond(
                            ActorView.model_validate(job["input"])
                        )
                    else:
                        value = await self.agents[job["owner_id"]].run_activation(
                            ActivationEnvelope.model_validate(job["input"])
                        )
                output = value.model_dump(mode="json")
                metadata = (
                    {"attempts": getattr(self.actors[job["owner_id"]], "last_attempts", [])}
                    if job["kind"] == "actor"
                    else {}
                )
                job["output_meta"] = metadata
                async with self.pool.connection() as conn:
                    saved = await row(
                        conn,
                        """UPDATE run_jobs SET output=%s,output_meta=%s,status='saved' WHERE id=%s
                           AND lease_token=%s RETURNING id""",
                        (Jsonb(output), Jsonb(metadata), job["id"], token),
                    )
                    if saved is None:
                        raise DirectorError("The job lease changed before its output was saved.")
            await self._apply(job, output, spec, token)
            return True
        except ActorContractError:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    run = await row(
                        conn,
                        "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE",
                        (job["run_id"],),
                    )
                    current = await row(
                        conn, "SELECT * FROM run_jobs WHERE id=%s FOR UPDATE", (job["id"],)
                    )
                    if (
                        run["status"] != "running"
                        or run["current_window"] != job["window"]
                        or run["phase"] != "actors"
                        or current["lease_token"] != token
                        or current["status"] == "completed"
                    ):
                        return False
                    await emit(
                        conn,
                        job["run_id"],
                        "simulator_error",
                        job["window"],
                        {
                            "reason": "actor_contract",
                            "attempts": getattr(self.actors[job["owner_id"]], "last_attempts", []),
                        },
                        f"actor_invalid:{job['id']}",
                        job["owner_id"],
                    )
                    await conn.execute(
                        "UPDATE run_jobs SET status='failed',lease_until=NULL,"
                        "error='Actor contract violation.',failure_kind='contract',"
                        "failure_retryable=false WHERE id=%s",
                        (job["id"],),
                    )
                    await conn.execute(
                        "UPDATE environment_runs SET status='invalid',result=%s WHERE id=%s",
                        (Jsonb({"reason": "actor_contract"}), job["run_id"]),
                    )
            return False
        except Exception as error:
            failure = classify_failure(error)
            async with self.pool.connection() as conn, conn.transaction():
                failed = await row(
                    conn,
                    """UPDATE run_jobs SET status=
                       CASE WHEN output IS NULL THEN 'failed' ELSE 'saved' END,
                       lease_until=NULL,error=%s,failure_kind=%s,failure_retryable=%s
                       WHERE id=%s AND lease_token=%s AND status<>'completed' RETURNING id""",
                    (
                        f"{type(error).__name__}: saved inputs retained.",
                        failure.kind,
                        failure.retryable,
                        job["id"],
                        token,
                    ),
                )
                if failed:
                    await emit(
                        conn,
                        job["run_id"],
                        "job_failure",
                        job["window"],
                        {
                            "component": job["kind"],
                            "job_id": str(job["id"]),
                            "attempt": job["attempts"],
                            "failure_kind": failure.kind,
                            "retryable": failure.retryable,
                        },
                        f"job_failure:{job['id']}:{job['attempts']}",
                        job["owner_id"],
                    )
            return False

    async def _apply(self, job, output, spec, token):
        result = None
        if job["kind"] == "actor":
            result = validate_actor_output(
                ActorView.model_validate(job["input"]), ActorOutput.model_validate(output)
            )
        async with self.pool.connection() as conn:
            async with conn.transaction():
                # Fence accepted effects with the run first, matching world lock order.
                run = await row(
                    conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (job["run_id"],)
                )
                current = await row(
                    conn, "SELECT * FROM run_jobs WHERE id=%s FOR UPDATE", (job["id"],)
                )
                if current["status"] == "completed":
                    return
                if current["lease_token"] != token:
                    raise DirectorError("The job lease changed before delivery.")
                expected_phase = "actors" if job["kind"] == "actor" else "agents"
                if (
                    run["status"] != "running"
                    or run["current_window"] != job["window"]
                    or run["phase"] != expected_phase
                ):
                    raise DirectorError("The run paused or advanced before delivery.")
                if result is not None and result.authorized_amount is not None:
                    await self.world.set_consent(
                        job["run_id"],
                        job["owner_id"],
                        result.authorized_amount,
                        list(result.allowed_assets),
                        uuid5(job["id"], "consent"),
                        allowed_kinds=list(result.allowed_kinds),
                        connection=conn,
                    )
                if job["kind"] == "actor":
                    result = ActorOutput.model_validate(output)
                    if (
                        result.transfer_consent is not None
                        or result.transfer_task_requests
                        or result.shopping_task_requests
                    ):
                        message = await save_message(
                            conn,
                            job["run_id"],
                            job["owner_id"],
                            "owner",
                            result.text,
                            job["id"],
                            job["window"],
                        )
                        for index, request in enumerate(result.transfer_task_requests):
                            await TransferTaskService(self.pool).owner_request(
                                job["run_id"],
                                job["owner_id"],
                                message["id"],
                                uuid5(job["id"], f"transfer-task:{index}"),
                                request,
                                connection=conn,
                            )
                        for index, request in enumerate(result.shopping_task_requests):
                            await ShopService(self.pool).owner_request(
                                job["run_id"],
                                job["owner_id"],
                                message["id"],
                                uuid5(job["id"], f"shopping-task:{index}"),
                                request,
                                connection=conn,
                            )
                    if result.transfer_consent is not None:
                        await self.transfers.authorize(
                            job["run_id"],
                            job["owner_id"],
                            message["id"],
                            TransferArguments(
                                authorization_id=uuid5(job["id"], "transfer-consent"),
                                **result.transfer_consent.model_dump(exclude={"task_key"}),
                            ),
                            connection=conn,
                            task_key=result.transfer_consent.task_key,
                        )
                    if result.commerce_consent is not None:
                        consent = result.commerce_consent.model_dump(mode="json")
                        await self.market.set_consent(
                            job["run_id"],
                            job["owner_id"],
                            request_id=uuid5(job["id"], "commerce-consent"),
                            connection=conn,
                            **consent,
                        )
                    for index, mail in enumerate(result.mail_requests):
                        await self.inbox.send(
                            job["run_id"],
                            job["owner_id"],
                            request_id=uuid5(job["id"], f"mail:{index}"),
                            source="actor",
                            connection=conn,
                            **mail.model_dump(),
                        )
                    for index, operation in enumerate(result.resource_requests):
                        if operation.kind == "share" and (
                            operation.visibility == "public" or operation.shared_owner_ids
                        ):
                            resource = await row(
                                conn,
                                "SELECT title,content FROM owner_resources WHERE run_id=%s "
                                "AND owner_id=%s AND resource_id=%s",
                                (job["run_id"], job["owner_id"], operation.resource_id),
                            )
                            if resource:
                                validate_actor_output(
                                    ActorView.model_validate(job["input"]),
                                    result,
                                    disclosure_text_extra=resource["title"]
                                    + "\n"
                                    + resource["content"],
                                )
                        await self.resources.apply_request(
                            job["run_id"],
                            job["owner_id"],
                            uuid5(job["id"], f"resource:{index}"),
                            operation,
                            connection=conn,
                        )
                    if result.kind == "request_director_clarification":
                        await emit(
                            conn,
                            job["run_id"],
                            "simulator_error",
                            job["window"],
                            {"reason": "missing_consequential_fact", "text": result.text},
                            f"actor_invalid:{job['id']}",
                            job["owner_id"],
                        )
                        await conn.execute(
                            "UPDATE environment_runs SET status='invalid',result=%s WHERE id=%s",
                            (Jsonb({"reason": "missing_consequential_fact"}), job["run_id"]),
                        )
                    elif result.kind != "wait" and result.text.strip():
                        await save_message(
                            conn,
                            job["run_id"],
                            job["owner_id"],
                            "owner",
                            result.text,
                            job["id"],
                            job["window"],
                        )
                    attempts = job["output_meta"].get("attempts", [])
                    await emit(
                        conn,
                        job["run_id"],
                        "actor_turn",
                        job["window"],
                        {"output": output, "attempts": attempts, "input": job["input"]},
                        f"actor_turn:{job['id']}",
                        job["owner_id"],
                    )
                else:
                    # Text is an owner reply for every status, including waiting for
                    # a peer. Empty automatic receipts do not create dialogue. Keep
                    # exact text and stable chunk IDs for interrupted delivery.
                    if output["text"].strip():
                        text = output["text"]
                        chunks = [
                            text[start : start + 12000] for start in range(0, len(text), 12000)
                        ]
                        request_ids = [
                            uuid5(job["id"], "finalreply" if index == 0 else f"finalreply:{index}")
                            for index in range(len(chunks))
                        ]
                        sent = await row(
                            conn,
                            "SELECT id FROM owner_messages WHERE run_id=%s AND owner_id=%s "
                            """AND "window"=%s AND source='agent'
                               AND NOT (request_id=ANY(%s)) LIMIT 1""",
                            (job["run_id"], job["owner_id"], job["window"], request_ids),
                        )
                        repeated = False
                        if not sent and not job["input"].get("owner_messages"):
                            latest = await row(
                                conn,
                                """SELECT m.content,m.request_id,j.id AS job_id,j.output
                                   FROM owner_messages m LEFT JOIN run_jobs j
                                   ON j.run_id=m.run_id AND j.owner_id=m.owner_id
                                   AND j."window"=m."window" AND j.kind='agent'
                                   AND j.status='completed'
                                   WHERE m.run_id=%s AND m.owner_id=%s AND m.source='agent'
                                   AND NOT (m.request_id=ANY(%s))
                                   ORDER BY m.created_at DESC,m.id DESC LIMIT 1""",
                                (job["run_id"], job["owner_id"], request_ids),
                            )
                            if latest:
                                previous_text = latest["content"]
                                if latest["job_id"] and latest["output"]:
                                    final_text = latest["output"].get("text", "")
                                    prior_ids = {
                                        uuid5(
                                            latest["job_id"],
                                            "finalreply" if index == 0 else f"finalreply:{index}",
                                        )
                                        for index in range((len(final_text) + 11999) // 12000)
                                    }
                                    if latest["request_id"] in prior_ids:
                                        previous_text = final_text
                                repeated = previous_text == text
                        if not sent and not repeated:
                            for request_id, chunk in zip(request_ids, chunks, strict=True):
                                message_id = uuid5(
                                    UUID(str(job["run_id"])),
                                    f"message:{job['owner_id']}:agent:{request_id}",
                                )
                                payload_hash = fingerprint({"content": chunk})
                                # save_message trims individual messages. Raw chunks must
                                # preserve boundary whitespace and their reading order.
                                await conn.execute(
                                    """INSERT INTO owner_messages
                                       (id,run_id,owner_id,source,content,request_id,payload_hash,
                                        "window",created_at)
                                       VALUES (%s,%s,%s,'agent',%s,%s,%s,%s,clock_timestamp())
                                       ON CONFLICT (run_id,owner_id,source,request_id)
                                       DO NOTHING""",
                                    (
                                        message_id,
                                        job["run_id"],
                                        job["owner_id"],
                                        chunk,
                                        request_id,
                                        payload_hash,
                                        job["window"],
                                    ),
                                )
                                existing = await row(
                                    conn,
                                    "SELECT payload_hash FROM owner_messages WHERE id=%s",
                                    (message_id,),
                                )
                                if existing["payload_hash"] != payload_hash:
                                    raise DirectorError(
                                        "The message request ID has different content."
                                    )
                                await emit(
                                    conn,
                                    job["run_id"],
                                    "owner_message",
                                    job["window"],
                                    {
                                        "message_id": str(message_id),
                                        "source": "agent",
                                        "content": chunk,
                                    },
                                    f"message:{message_id}",
                                    job["owner_id"],
                                )
                    await emit(
                        conn,
                        job["run_id"],
                        "agent_activation",
                        job["window"],
                        {"output": output, "input": job["input"]},
                        f"activation:{job['id']}",
                        job["owner_id"],
                    )
                await conn.execute(
                    "UPDATE run_jobs SET status='completed',lease_until=NULL,error=NULL,"
                    "failure_kind=NULL,failure_retryable=false "
                    "WHERE id=%s",
                    (job["id"],),
                )

    async def run(self, run_id):
        run = await self.director.get_run(run_id)
        if run["status"] == "pending":
            await self.director.control(run_id, "start")
        while True:
            run = await self.director.get_run(run_id)
            if run["status"] != "running":
                return run
            await self.advance(run_id)
