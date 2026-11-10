"""Elapsed Clock mechanics use supplied decisions and real isolated PostgreSQL."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.simulation_clock import (
    ClockConflict,
    ClockContract,
    ClockError,
    Control,
    ProcessSpec,
    Schedule,
    SimulationClock,
    Sleep,
    Work,
    generation_seconds,
)


def contract(*, pairs=1, **changes):
    return ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=changes.pop("horizon_seconds", 604800),
        processes=tuple(
            ProcessSpec(id=f"{role}-{i}", owner_id=f"owner-{i}", role=role)
            for i in range(pairs)
            for role in ["actor", "agent"]
        ),
        tool_seconds={"read": 2, "transfer": 7},
        output_tokens_per_minute=600,
        **changes,
    )


async def make(pool, **changes):
    service = SimulationClock(pool)
    spec = contract(**changes)
    clock = await service.create(spec)
    return service, clock, spec


async def run(service, clock, command, request_id=None):
    return await service.execute(clock, request_id or uuid4(), command)


async def park(service, clock, spec, *, until=None, interrupt_on=("mail",)):
    for process in spec.processes:
        await run(
            service,
            clock,
            Sleep(
                process_id=process.id,
                until_seconds=until or spec.horizon_seconds,
                interrupt_on=interrupt_on,
            ),
        )


@pytest.mark.parametrize(
    "tokens,rate,seconds",
    [(0, 600, 1), (1, 600, 1), (10, 600, 1), (11, 600, 2), (600, 600, 60), (60, 1, 3600)],
)
def test_generation_duration_has_exact_integer_ceiling(tokens, rate, seconds):
    assert generation_seconds(tokens, rate) == seconds


@pytest.mark.parametrize(
    "changes",
    [
        {"horizon_seconds": True},
        {"horizon_seconds": 0},
        {"horizon_seconds": 1.5},
        {"horizon_seconds": 31_536_001},
        {"output_tokens_per_minute": 0},
        {"epoch": "2026-11-09T09:00:00"},
        {"epoch": "2026-11-09T09:00:00.1Z"},
        {"epoch": 1_794_211_200},
        {"max_events": 100001},
        {"version": "windows-v1"},
    ],
)
def test_contract_rejects_ambiguous_or_unsupported_time(changes):
    data = contract().model_dump(mode="json")
    data.update(changes)
    with pytest.raises(ValidationError):
        ClockContract.model_validate(data)


@pytest.mark.parametrize(
    "bad",
    [
        [{"id": "p", "owner_id": "o", "role": "agent"}],
        [{"id": "p", "owner_id": "o", "role": "agent"}] * 2,
        [
            {"id": "p", "owner_id": "o", "role": "agent"},
            {"id": "p", "owner_id": "o", "role": "actor"},
        ],
    ],
)
def test_contract_requires_distinct_paired_roles(bad):
    data = contract().model_dump(mode="json")
    data["processes"] = bad
    with pytest.raises(ValidationError):
        ClockContract.model_validate(data)


def test_timezone_normalization_and_legacy_scenario_hash_are_independent():
    spec = vehicle_scenario(2)
    before = spec.canonical_json(), spec.content_hash()
    data = contract().model_dump(mode="json")
    data["epoch"] = "2026-11-09T10:00:00+01:00"
    assert ClockContract.model_validate(data).model_dump(mode="json") == contract().model_dump(
        mode="json"
    )
    assert (spec.canonical_json(), spec.content_hash()) == before


async def test_frozen_contract_replay_conflict_and_fifteen_pairs(pool):
    service, clock, spec = await make(pool, pairs=15)
    assert await service.create(spec, clock_id=clock) == clock
    assert len((await service.snapshot(clock))["processes"]) == 30
    data = spec.model_dump(mode="json")
    data["horizon_seconds"] = 3600
    with pytest.raises(ClockConflict):
        await service.create(ClockContract.model_validate(data), clock_id=clock)
    assert (await service.snapshot(clock))["contract_hash"]


async def test_ready_process_blocks_future_time_but_not_current_time_delivery(pool):
    service, clock, spec = await make(pool)
    await run(
        service,
        clock,
        Schedule(event_key="now", at_seconds=0, kind="mail", recipients=("agent-0",)),
    )
    assert (await run(service, clock, Control(operation="step")))["event"]["event_key"] == "now"
    result = await run(service, clock, Control(operation="step"))
    assert result["reason"] == "ready_process_requires_decision" and result["now_seconds"] == 0
    await park(service, clock, spec)
    result = await run(service, clock, Control(operation="step"))
    assert result["reason"] == "horizon_cutoff" and result["now_seconds"] == 604800


async def test_time_queue_order_cannot_be_retroactively_changed(pool):
    service, clock, spec = await make(pool)
    await park(service, clock, spec, interrupt_on=())
    for key, priority in [("z", 0), ("a", 0), ("first", -1), ("tomorrow", 1)]:
        await run(
            service,
            clock,
            Schedule(
                event_key=key,
                at_seconds=10,
                priority=priority,
                kind="mail",
                recipients=("agent-0",),
            ),
        )
    delivered = []
    for _ in range(3):
        delivered.append(
            (await run(service, clock, Control(operation="step")))["event"]["event_key"]
        )
    assert delivered == ["first", "a", "z"]
    with pytest.raises(ClockError):
        await run(
            service,
            clock,
            Schedule(event_key="b", at_seconds=10, kind="mail", recipients=("agent-0",)),
        )
    with pytest.raises(ClockError):
        await run(
            service,
            clock,
            Schedule(event_key="past", at_seconds=9, kind="mail", recipients=("agent-0",)),
        )
    assert (await run(service, clock, Control(operation="step")))["event"][
        "event_key"
    ] == "tomorrow"


async def test_interrupt_cancels_only_own_timer_and_retry_cannot_repeat_delivery(pool):
    service, clock, spec = await make(pool, pairs=2)
    await park(service, clock, spec, until=3600)
    await run(
        service,
        clock,
        Schedule(event_key="mail", at_seconds=60, kind="mail", recipients=("agent-0",)),
    )
    request = uuid4()
    delivered = await run(service, clock, Control(operation="step"), request)
    assert delivered["woken_process_ids"] == ["agent-0"] and delivered["now_seconds"] == 60
    assert await run(SimulationClock(pool), clock, Control(operation="step"), request) == delivered
    state = await service.snapshot(clock)
    assert state["event_counts"] == {"cancelled": 1, "delivered": 1, "pending": 3}
    assert [p["process_id"] for p in state["processes"] if p["state"] == "ready"] == ["agent-0"]
    await run(service, clock, Sleep(process_id="agent-0", until_seconds=7200))
    first = (await run(service, clock, Control(operation="step")))["event"]
    assert first["at_seconds"] == 3600 and first["recipients"] != ["agent-0"]


async def test_parallel_work_uses_minimum_ready_time_not_sum_and_busy_mail_is_retained(pool):
    service, clock, spec = await make(pool)
    await run(service, clock, Work(process_id="actor-0", kind="generation", output_tokens=100))
    await run(service, clock, Work(process_id="agent-0", kind="tool", tool="transfer"))
    await run(
        service,
        clock,
        Schedule(event_key="notice", at_seconds=3, kind="mail", recipients=("agent-0",)),
    )
    notice = await run(service, clock, Control(operation="step"))
    assert notice["now_seconds"] == 3 and notice["woken_process_ids"] == []
    assert all(p["state"] == "busy" for p in (await service.snapshot(clock))["processes"])
    transfer = await run(service, clock, Control(operation="step"))
    assert transfer["now_seconds"] == 7 and transfer["woken_process_ids"] == ["agent-0"]
    assert (await run(service, clock, Control(operation="step")))[
        "reason"
    ] == "ready_process_requires_decision"
    await run(service, clock, Sleep(process_id="agent-0", until_seconds=100))
    generated = await run(service, clock, Control(operation="step"))
    assert generated["now_seconds"] == 10 and generated["woken_process_ids"] == ["actor-0"]
    # Wall-clock sleeps and database timestamps never determine these values.


async def test_exact_and_post_horizon_events_remain_pending_without_fake_success(pool):
    service, clock, spec = await make(pool, horizon_seconds=100)
    await park(service, clock, spec, until=101, interrupt_on=())
    for key, at in [("before", 99), ("boundary", 100), ("after", 101)]:
        await run(
            service,
            clock,
            Schedule(event_key=key, at_seconds=at, kind="reminder", recipients=("agent-0",)),
        )
    assert (await run(service, clock, Control(operation="step")))["event"]["event_key"] == "before"
    cutoff = await run(service, clock, Control(operation="step"))
    assert cutoff["now_seconds"] == 100 and cutoff["reason"] == "horizon_cutoff"
    snap = await service.snapshot(clock)
    assert snap["simulated_at"] == "2026-11-09T09:01:40+00:00"
    assert not snap["queue_drained"] and snap["task_success"] is None
    assert {e["event_key"] for e in snap["pending_events"]} >= {"boundary", "after"}
    with pytest.raises(ClockError):
        await run(service, clock, Control(operation="resume"))


async def test_concurrent_step_same_request_is_exact_replay_distinct_requests_deliver_once(pool):
    service, clock, spec = await make(pool)
    await park(service, clock, spec, interrupt_on=())
    for key in ["a", "b"]:
        await run(
            service,
            clock,
            Schedule(event_key=key, at_seconds=2, kind="mail", recipients=("agent-0",)),
        )
    request = uuid4()
    results = await asyncio.gather(
        *(run(service, clock, Control(operation="step"), request) for _ in range(5))
    )
    assert all(r == results[0] for r in results)
    assert (await service.snapshot(clock))["event_counts"]["delivered"] == 1
    results = await asyncio.gather(
        *(run(service, clock, Control(operation="step")) for _ in range(2))
    )
    assert sum(r["reason"] == "event_delivered" for r in results) == 1
    assert (await service.snapshot(clock))["event_counts"]["delivered"] == 2


async def test_cross_clock_recipient_and_conflicting_operation_are_atomic(pool):
    service, clock, spec = await make(pool)
    before = await service.snapshot(clock)
    with pytest.raises(ClockError):
        await run(
            service,
            clock,
            Schedule(event_key="x", at_seconds=1, kind="mail", recipients=("foreign-agent",)),
        )
    assert await service.snapshot(clock) == before
    request = uuid4()
    await run(service, clock, Sleep(process_id="agent-0", until_seconds=20), request)
    with pytest.raises(ClockConflict):
        await run(service, clock, Sleep(process_id="agent-0", until_seconds=21), request)
    second = await service.create(spec)
    await run(service, second, Sleep(process_id="agent-0", until_seconds=21), request)
    assert (await service.snapshot(second))["now_seconds"] == 0


async def test_event_identity_replays_even_after_delivery_but_changed_payload_conflicts(pool):
    service, clock, spec = await make(pool)
    await park(service, clock, spec, interrupt_on=())
    event = Schedule(
        event_key="x", at_seconds=10, kind="mail", recipients=("agent-0",), payload={"a": 1}
    )
    await run(service, clock, event)
    await run(service, clock, Control(operation="step"))
    await run(service, clock, event)
    with pytest.raises(ClockConflict):
        await run(service, clock, event.model_copy(update={"payload": {"a": 2}}))
    assert (await service.snapshot(clock))["event_counts"]["delivered"] == 1


async def test_capacity_and_invalid_work_leave_process_ready(pool):
    service, clock, spec = await make(pool, max_events=1)
    await run(service, clock, Sleep(process_id="actor-0", until_seconds=10))
    for command in [
        Work(process_id="agent-0", kind="tool", tool="unknown"),
        Sleep(process_id="agent-0", until_seconds=0),
        Sleep(process_id="agent-0", until_seconds=10),
    ]:
        with pytest.raises(ClockError):
            await run(service, clock, command)
    assert (
        next(
            p for p in (await service.snapshot(clock))["processes"] if p["process_id"] == "agent-0"
        )["state"]
        == "ready"
    )


async def test_pause_is_durable_and_new_pool_resume_preserves_identity(test_database_url, pool):
    service, clock, spec = await make(pool)
    await park(service, clock, spec)
    await run(service, clock, Control(operation="pause"))
    async with AsyncConnectionPool(test_database_url, kwargs={"row_factory": dict_row}) as fresh:
        recreated = SimulationClock(fresh)
        assert (await run(recreated, clock, Control(operation="step")))["reason"] == "paused"
        await run(recreated, clock, Control(operation="resume"))
        assert (await run(recreated, clock, Control(operation="step")))["now_seconds"] == 604800


@pytest.mark.parametrize(
    "command",
    [
        {"operation": "work", "kind": "generation", "process_id": "agent-0"},
        {
            "operation": "work",
            "kind": "tool",
            "process_id": "agent-0",
            "tool": "read",
            "output_tokens": 1,
        },
        {"operation": "sleep", "process_id": "agent-0", "until_seconds": True},
        {
            "operation": "schedule",
            "event_key": "x",
            "at_seconds": 1,
            "kind": "timer",
            "recipients": ["agent-0"],
        },
        {
            "operation": "schedule",
            "event_key": "x",
            "at_seconds": 1,
            "kind": "mail",
            "recipients": ["agent-0", "agent-0"],
        },
        {
            "operation": "schedule",
            "event_key": "x",
            "at_seconds": 1,
            "kind": "mail",
            "recipients": ["agent-0"],
            "payload": {"x": "a" * 8193},
        },
    ],
)
async def test_invalid_commands_are_rejected_before_mutation(pool, command):
    service, clock, spec = await make(pool)
    before = await service.snapshot(clock)
    with pytest.raises(ValidationError):
        await run(service, clock, command)
    assert await service.snapshot(clock) == before


async def test_failure_before_receipt_commit_rolls_back_time_delivery_and_wakeup(pool):
    service, clock, spec = await make(pool)
    await park(service, clock, spec)
    await run(
        service,
        clock,
        Schedule(
            event_key="mail",
            at_seconds=10,
            kind="mail",
            recipients=("agent-0",),
        ),
    )
    before = await service.snapshot(clock)
    async with pool.connection() as conn:
        await conn.execute("""CREATE FUNCTION fail_clock_receipt() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'injected receipt failure'; END;
            $$ LANGUAGE plpgsql""")
        await conn.execute("""CREATE TRIGGER fail_clock_receipt BEFORE INSERT
            ON simulation_clock_operations FOR EACH ROW EXECUTE FUNCTION fail_clock_receipt()""")
    request = uuid4()
    with pytest.raises(psycopg.Error):
        await run(service, clock, Control(operation="step"), request)
    assert await service.snapshot(clock) == before
    async with pool.connection() as conn:
        await conn.execute("DROP TRIGGER fail_clock_receipt ON simulation_clock_operations")
    result = await run(service, clock, Control(operation="step"), request)
    assert result["now_seconds"] == 10 and result["woken_process_ids"] == ["agent-0"]
    assert (await service.snapshot(clock))["event_counts"]["delivered"] == 1


async def test_one_clock_lock_does_not_block_another_clock(pool):
    service, first, spec = await make(pool)
    second = await service.create(spec)
    async with pool.connection() as blocker, blocker.transaction():
        await blocker.execute("SELECT id FROM simulation_clocks WHERE id=%s FOR UPDATE", (first,))
        result = await asyncio.wait_for(run(service, second, Control(operation="pause")), 2)
        assert result["status"] == "paused"
