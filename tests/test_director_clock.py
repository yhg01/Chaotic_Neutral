"""Real PostgreSQL dispatch/Clock recovery; every executor response is supplied."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from agentrpg.director import Director, DirectorError, row, rows
from agentrpg.director_clock import DirectorClock, TaskOutput
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.simulation_clock import (
    ClockConflict,
    ClockContract,
    ClockError,
    Control,
    ProcessSpec,
    Schedule,
    Sleep,
)


async def setup(pool, *, pairs=2, horizon=100, max_jobs=1000):
    spec = vehicle_scenario(pairs)
    manifest = await Director(pool).create_run(spec)
    run = UUID(manifest["run_id"])
    contract = ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=horizon,
        output_tokens_per_minute=600,
        processes=tuple(
            ProcessSpec(id=f"{role}-{i}", owner_id=o.id, role=role)
            for i, o in enumerate(spec.owners)
            for role in ["actor", "agent"]
        ),
    )
    service = DirectorClock(pool)
    clock = await service.bind(run, contract, max_jobs=max_jobs)
    return service, run, clock, contract


async def park(service, run, contract, *, except_process="agent-0", until=None):
    for p in contract.processes:
        if p.id != except_process:
            await service.control(
                run,
                uuid4(),
                Sleep(
                    process_id=p.id,
                    until_seconds=until or contract.horizon_seconds,
                    interrupt_on=("owner_message", "assistant_reply", "mail"),
                ),
            )


def output(text="A supplied private reply.", tokens=10, until=100, interrupt_on=("mail",)):
    return TaskOutput(
        text=text, output_tokens=tokens, until_seconds=until, interrupt_on=interrupt_on
    )


async def job(service, run, process="agent-0"):
    return await service.prepare(run, process, uuid4())


async def supplied(packet):
    return output()


async def messages(pool, run):
    return await Director(pool).messages(run, "owner-1")


@pytest.mark.parametrize("pairs", [2, 15])
async def test_bind_real_director_exact_pairs_and_block_legacy_execution(pool, pairs):
    service, run, clock, contract = await setup(pool, pairs=pairs)
    assert await service.bind(run, contract) == clock
    state = await service.clock.snapshot(clock)
    assert state["world_run_id"] == str(run) and len(state["processes"]) == 2 * pairs
    assert (await Director(pool).get_run(run))["status"] == "paused"
    with pytest.raises(ClockError):
        await service.clock.execute(clock, uuid4(), Control(operation="step"))
    with pytest.raises(DirectorError):
        await Director(pool).control(run, "resume")
    with pytest.raises(DirectorError):
        await RunEngine(pool, actors={}, agents={}).advance(run)
    with pytest.raises(ClockConflict):
        await service.bind(run, contract, max_jobs=10)


async def test_bind_refuses_started_world_and_rolls_back_new_clock(pool):
    service, run, clock, contract = await setup(pool)
    other = UUID((await Director(pool).create_run(vehicle_scenario(2)))["run_id"])
    await Director(pool).control(other, "start")
    with pytest.raises(DirectorError):
        await service.bind(other, contract)
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM simulation_clocks"))["n"] == 1


async def test_prepare_freezes_input_and_consumes_only_targeted_notices(pool):
    service, run, clock, contract = await setup(pool)
    await service.control(
        run,
        uuid4(),
        Schedule(
            event_key="own",
            at_seconds=0,
            kind="mail",
            recipients=("agent-0",),
            payload={"synthetic": "own"},
        ),
    )
    await service.control(
        run,
        uuid4(),
        Schedule(
            event_key="peer",
            at_seconds=0,
            kind="mail",
            recipients=("agent-1",),
            payload={"synthetic": "peer-private"},
        ),
    )
    await service.step(run, uuid4())
    await service.step(run, uuid4())
    request = uuid4()
    first = await service.prepare(run, "agent-0", request)
    assert [n["event_key"] for n in first["input"]["notifications"]] == ["own"]
    assert "scenario" not in first["input"] and "actor_view" not in first["input"]
    await service.control(
        run,
        uuid4(),
        Schedule(event_key="z-later", at_seconds=0, kind="mail", recipients=("agent-0",)),
    )
    await service.step(run, uuid4())
    assert await service.prepare(run, "agent-0", request) == first
    with pytest.raises(ClockConflict):
        await service.prepare(run, "actor-0", request)
    with pytest.raises(DirectorError):
        await job(service, run)
    actor = await job(service, run, "actor-1")
    assert actor["input"]["actor_view"]["owner_id"] == "owner-2"
    assert "token" not in str(actor["input"]) and "scenario" not in actor["input"]


async def test_saved_generation_is_delivered_at_due_time_exactly_once(pool):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract)
    task = await job(service, run)
    assert (await service.dispatch(run, task["id"], supplied))["status"] == "timed"
    assert await messages(pool, run) == []
    request = uuid4()
    result = await service.step(run, request)
    assert result["now_seconds"] == 1 and result["applied_job_id"] == task["id"]
    assert await DirectorClock(pool).step(run, request) == result
    saved = await messages(pool, run)
    assert len(saved) == 1 and saved[0]["source"] == "agent"
    assert saved[0]["content"] == "A supplied private reply."
    notice = await service.step(run, uuid4())
    assert notice["event"]["kind"] == "assistant_reply" and notice["woken_process_ids"] == [
        "actor-0"
    ]


async def test_only_one_live_worker_and_unresolved_dispatch_blocks_time(pool):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract)
    task = await job(service, run)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow(packet):
        calls.append(packet["job_id"])
        entered.set()
        await release.wait()
        return output()

    pending = asyncio.create_task(service.dispatch(run, task["id"], slow))
    await entered.wait()
    try:
        with pytest.raises(DirectorError):
            await service.dispatch(run, task["id"], slow)
        assert (await service.recover(run, task["id"]))["status"] == "running"
        assert (await service.step(run, uuid4()))["reason"] == "ready_process_requires_decision"
        with pytest.raises(DirectorError):
            await service.control(run, uuid4(), Sleep(process_id="agent-0", until_seconds=20))
    finally:
        release.set()
        await pending
    assert calls == [task["id"]]


async def test_executor_runs_without_open_transaction_or_clock_row_lock(pool):
    service, run, clock, contract = await setup(pool)
    task = await job(service, run)

    async def inspect(packet):
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SET LOCAL lock_timeout='500ms'")
            await conn.execute("SELECT id FROM simulation_clocks WHERE id=%s FOR UPDATE", (clock,))
            idle = await row(
                conn,
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND state='idle in transaction'",
            )
            assert idle["n"] == 0
        return output()

    await service.dispatch(run, task["id"], inspect)


async def test_pause_during_dispatch_saves_output_and_resume_makes_no_second_call(pool):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract)
    task = await job(service, run)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow(packet):
        calls.append(1)
        entered.set()
        await release.wait()
        return output()

    pending = asyncio.create_task(service.dispatch(run, task["id"], slow))
    await entered.wait()
    await service.control(run, uuid4(), Control(operation="pause"))
    release.set()
    assert (await pending)["status"] == "saved"
    assert (await service.step(run, uuid4()))["reason"] == "paused"
    assert (await service.dispatch(run, task["id"], slow))["status"] == "saved"
    await service.control(run, uuid4(), Control(operation="resume"))
    assert (await DirectorClock(pool).arm(run, task["id"]))["status"] == "timed"
    await service.step(run, uuid4())
    assert len(await messages(pool, run)) == 1 and calls == [1]


async def test_saved_output_survives_failure_before_arm_without_redispatch(pool, monkeypatch):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract)
    task = await job(service, run)
    calls = []

    async def once(packet):
        calls.append(1)
        return output()

    async def fail(*args):
        raise RuntimeError("injected after saved output")

    monkeypatch.setattr(service, "arm", fail)
    with pytest.raises(RuntimeError):
        await service.dispatch(run, task["id"], once)
    fresh = DirectorClock(pool)
    assert (await fresh.arm(run, task["id"]))["status"] == "timed"
    await fresh.step(run, uuid4())
    assert calls == [1] and len(await messages(pool, run)) == 1


async def install_failure(pool, status):
    async with pool.connection() as conn:
        await conn.execute("""CREATE FUNCTION injected_clock_job_failure() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'injected job commit failure'; END;
            $$ LANGUAGE plpgsql""")
        await conn.execute(
            "CREATE TRIGGER injected_clock_job_failure BEFORE UPDATE ON director_clock_jobs "
            f"FOR EACH ROW WHEN (NEW.status='{status}') "
            "EXECUTE FUNCTION injected_clock_job_failure()"
        )


async def remove_failure(pool):
    async with pool.connection() as conn:
        await conn.execute("DROP TRIGGER injected_clock_job_failure ON director_clock_jobs")


async def test_timing_and_job_state_commit_atomically(pool):
    service, run, clock, contract = await setup(pool)
    task = await job(service, run)
    await install_failure(pool, "timed")
    with pytest.raises(psycopg.Error):
        await service.dispatch(run, task["id"], supplied)
    snap = await service.clock.snapshot(clock)
    assert all(p["state"] == "ready" for p in snap["processes"]) and snap["event_counts"] == {}
    assert (await service.snapshot(run))["jobs"][0]["status"] == "saved"
    await remove_failure(pool)
    assert (await service.arm(run, task["id"]))["status"] == "timed"


async def test_clock_delivery_chat_notification_and_job_commit_roll_back_together(pool):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract)
    task = await job(service, run)
    await service.dispatch(run, task["id"], supplied)
    before = await service.clock.snapshot(clock)
    await install_failure(pool, "applied")
    request = uuid4()
    with pytest.raises(psycopg.Error):
        await service.step(run, request)
    assert await service.clock.snapshot(clock) == before and await messages(pool, run) == []
    await remove_failure(pool)
    await service.step(run, request)
    assert len(await messages(pool, run)) == 1


async def test_unknown_outcome_requires_evidence_and_cannot_automatically_retry(pool):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract)
    task = await job(service, run)
    calls = []

    async def failed(packet):
        calls.append(1)
        raise RuntimeError("supplied lost response")

    with pytest.raises(RuntimeError):
        await service.dispatch(run, task["id"], failed)
    assert (await service.recover(run, task["id"]))["status"] == "uncertain"
    with pytest.raises(DirectorError):
        await service.dispatch(run, task["id"], failed)
    assert (await service.step(run, uuid4()))["now_seconds"] == 0
    with pytest.raises(DirectorError):
        await service.reconcile(run, task["id"], output(), "")
    recovered = await service.reconcile(
        run, task["id"], output(), "supplied-local-fixture-response"
    )
    assert recovered["attempts"] == 1 and recovered["status"] == "saved"
    assert (
        await service.reconcile(run, task["id"], output(), "supplied-local-fixture-response")
        == recovered
    )
    with pytest.raises(ClockConflict):
        await service.reconcile(
            run, task["id"], output("changed"), "supplied-local-fixture-response"
        )
    await service.arm(run, task["id"])
    await service.step(run, uuid4())
    assert calls == [1] and len(await messages(pool, run)) == 1
    async with pool.connection() as conn:
        history = await rows(
            conn,
            "SELECT payload FROM run_events WHERE run_id=%s "
            "AND kind='elapsed_clock_dispatch_uncertain'",
            (run,),
        )
        assert len(history) == 1
        assert history[0]["payload"]["reason"] == "dispatch_output_not_durably_saved"


async def test_busy_notification_is_not_lost_when_result_selects_a_new_wait(pool):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract)
    task = await job(service, run)

    async def slow_time(packet):
        return output(tokens=100)

    await service.dispatch(run, task["id"], slow_time)
    await service.control(
        run,
        uuid4(),
        Schedule(event_key="during-work", at_seconds=3, kind="mail", recipients=("agent-0",)),
    )
    assert (await service.step(run, uuid4()))["woken_process_ids"] == []
    assert (await service.step(run, uuid4()))["now_seconds"] == 10
    lane = next(
        p
        for p in (await service.clock.snapshot(clock))["processes"]
        if p["process_id"] == "agent-0"
    )
    assert lane["state"] == "ready"
    followup = await job(service, run)
    assert [n["event_key"] for n in followup["input"]["notifications"]] == ["during-work"]


async def test_horizon_keeps_saved_timed_output_without_delivering_a_fake_reply(pool):
    service, run, clock, contract = await setup(pool, horizon=5)
    await park(service, run, contract, until=20)
    task = await job(service, run)

    async def long_time(packet):
        return output(tokens=100, until=20)

    await service.dispatch(run, task["id"], long_time)
    result = await service.step(run, uuid4())
    assert result["reason"] == "horizon_cutoff" and result["now_seconds"] == 5
    assert await messages(pool, run) == []
    state = await service.snapshot(run)
    assert state["jobs"][0]["status"] == "timed" and state["task_success"] is None


async def test_job_identity_is_run_scoped_and_capacity_failure_is_atomic(pool):
    service, run, clock, contract = await setup(pool, max_jobs=1)
    task = await job(service, run)
    with pytest.raises(DirectorError):
        await job(service, run, "actor-0")
    other = UUID((await Director(pool).create_run(vehicle_scenario(2)))["run_id"])
    await service.bind(other, contract)
    with pytest.raises(DirectorError):
        await service.arm(other, task["id"])


async def test_cancellation_retains_unknown_original_attempt_and_fences_retry(pool):
    service, run, clock, contract = await setup(pool)
    task = await job(service, run)
    entered = asyncio.Event()

    async def cancelled(packet):
        entered.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(service.dispatch(run, task["id"], cancelled))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    state = await service.recover(run, task["id"])
    assert state["status"] == "uncertain" and state["attempts"] == 1
    with pytest.raises(DirectorError):
        await service.dispatch(run, task["id"], supplied)


async def test_actor_chat_source_and_scoped_participant_session_are_preserved(pool):
    service, run, clock, contract = await setup(pool)
    await park(service, run, contract, except_process="actor-0")
    task = await job(service, run, "actor-0")
    participant = next(
        p for p in await Director(pool).participants(run) if p["owner_id"] == "owner-1"
    )
    assert task["input"]["participant_id"] == str(participant["agent_id"])
    assert task["input"]["session_id"] == str(participant["session_id"])
    await service.dispatch(run, task["id"], supplied)
    await service.step(run, uuid4())
    chat = await messages(pool, run)
    assert len(chat) == 1 and chat[0]["source"] == "owner"
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM transfer_grants"))["n"] == 0


async def test_notification_capacity_stops_before_any_delivery_receipt_or_new_job(pool):
    service, run, clock, contract = await setup(pool)
    for i in range(101):
        await service.control(
            run,
            uuid4(),
            Schedule(
                event_key=f"notice-{i:03}", at_seconds=0, kind="mail", recipients=("agent-0",)
            ),
        )
    for _ in range(101):
        await service.step(run, uuid4())
    with pytest.raises(DirectorError):
        await job(service, run)
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM director_clock_jobs"))["n"] == 0
        assert (await row(conn, "SELECT count(*) AS n FROM director_clock_deliveries"))["n"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"output_tokens": True},
        {"output_tokens": None},
        {"until_seconds": 1.5},
        {"transfer_consent": {"amount": 10}},
        {"text": "a" * 12001},
    ],
)
def test_output_cannot_invent_usage_or_grant_authority(change):
    with pytest.raises(ValidationError):
        TaskOutput.model_validate({**output().model_dump(), **change})


async def test_private_director_cue_is_prominent_and_keeps_peer_boundaries(pool):
    service, run, clock, contract = await setup(pool)
    for key, target, text in [
        ("cue-own", "actor-0", "Ask your assistant to discuss a new meeting time."),
        ("cue-peer", "actor-1", "Private peer-only need."),
    ]:
        await service.control(
            run,
            uuid4(),
            Schedule(
                event_key=key,
                at_seconds=0,
                kind="private_cue",
                recipients=(target,),
                payload={"text": text, "owner_response_required": True},
            ),
        )
    await service.step(run, uuid4())
    await service.step(run, uuid4())
    prepared = await job(service, run, "actor-0")
    view = prepared["input"]["actor_view"]
    assert "Ask your assistant to discuss a new meeting time." in view["cue"]
    assert [o["id"] for o in view["observations"]] == ["cue-own"]
    assert view["observations"][0]["owner_response_required"] is True
    assert "Private peer-only" not in str(view)
