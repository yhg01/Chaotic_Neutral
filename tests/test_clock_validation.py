"""Clock boundary, concurrent-conductor and temporal-validation audit probes.

All decisions are supplied. Each integration test uses its own PostgreSQL schema.
Optional artifacts contain synthetic events only, never database credentials.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput
from agentrpg.director import Director, DirectorError, rows
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario


def scenario(*, windows=6, owners=2, extra=()):
    data = vehicle_scenario(owners).model_dump(mode="json")
    data["max_windows"] = windows
    data["beats"] = [b for b in data["beats"] if b["trigger"]["kind"] == "run_started"]
    data["beats"].extend(extra)
    return ScenarioSpec.model_validate(data)


def clock_beat(identifier, window, *, requires=(), every=None, expiry=None):
    return {
        "id": identifier,
        "trigger": {"kind": "window_reached", "window": window, "every_windows": every},
        "recipients": ["owner-1"],
        "cue": "A declared clock reminder arrived.",
        "requires": list(requires),
        "expires_window": expiry,
    }


@pytest.fixture
def record(request):
    def write(**observations):
        directory = os.environ.get("CLOCK_AUDIT_OUTPUT")
        if not directory:
            return
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", request.node.name)
        payload = {
            "test": request.node.name,
            "decision_mode": "supplied_control",
            "provider_calls": 0,
            "storage": "isolated PostgreSQL test schema; removed after the test",
            **observations,
        }
        (folder / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    return write


class Owner:
    def __init__(self, *, silent=False, entered=None, release=None):
        self.silent, self.entered, self.release = silent, entered, release
        self.windows = []

    async def respond(self, view):
        self.windows.append(view.window)
        if self.entered:
            self.entered.set()
            await self.release.wait()
        return ActorOutput(
            kind="wait" if self.silent else "reply",
            text="" if self.silent else "Please review this update.",
            source_event_id=view.source_event_id,
        )


class Assistant:
    def __init__(self, *, wait=False, fail=False):
        self.wait, self.fail, self.windows = wait, fail, []

    async def run_activation(self, envelope):
        self.windows.append(envelope.window_id)
        if self.fail:
            raise RuntimeError("Injected local failure; no provider request.")
        return (
            ActivationResult(status="waiting", wait_for=["inbox_received"])
            if self.wait
            else ActivationResult(status="responded")
        )


async def setup(pool, spec, *, actors=None, agents=None):
    director = Director(pool)
    manifest = await director.create_run(spec)
    run = UUID(manifest["run_id"])
    actors = actors or {o.id: Owner() for o in spec.owners}
    agents = agents or {o.id: Assistant() for o in spec.owners}
    await director.control(run, "start")
    return director, run, RunEngine(pool, actors=actors, agents=agents), actors, agents


async def test_concurrent_conductor_cannot_double_advance_clock(pool, record):
    entered, release = asyncio.Event(), asyncio.Event()
    actors = {"owner-1": Owner(entered=entered, release=release), "owner-2": Owner()}
    director, run, engine, actors, agents = await setup(pool, scenario(), actors=actors)
    task = asyncio.create_task(engine.advance(run))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await director.get_run(run))["current_window"] == 0
        competitor = RunEngine(pool, actors=actors, agents=agents)
        with pytest.raises(DirectorError, match="active conductor"):
            await competitor.advance(run)
    finally:
        release.set()
        state = await asyncio.wait_for(task, 5)
    trace = await director.trace(run)
    completed = [e["window"] for e in trace if e["kind"] == "window_completed"]
    assert state["current_window"] == 1
    assert completed == [0]
    record(current_window=1, completed_windows=completed, slow_participant_held_clock=True)


async def test_agent_failure_and_new_engine_preserve_successful_peer_and_clock(pool, record):
    agents = {"owner-1": Assistant(), "owner-2": Assistant(fail=True)}
    director, run, engine, actors, agents = await setup(pool, scenario(), agents=agents)
    failed = await engine.advance(run)
    assert failed["status"] == "paused" and failed["current_window"] == 0
    async with pool.connection() as conn:
        before = await rows(
            conn,
            "SELECT id,owner_id,status,attempts,input_hash FROM run_jobs WHERE run_id=%s "
            "AND kind='agent' ORDER BY owner_id",
            (run,),
        )
    replacement_actors = {o: Owner() for o in actors}
    replacement_agents = {o: Assistant() for o in agents}
    fresh = RunEngine(pool, actors=replacement_actors, agents=replacement_agents)
    await director.control(run, "resume")
    restored = await fresh.advance(run)
    assert restored["current_window"] == 1
    assert all(not a.windows for a in replacement_actors.values())
    assert replacement_agents["owner-1"].windows == []
    assert replacement_agents["owner-2"].windows == [0]
    async with pool.connection() as conn:
        after = await rows(
            conn,
            "SELECT id,owner_id,status,attempts,input_hash FROM run_jobs WHERE run_id=%s "
            "AND kind='agent' ORDER BY owner_id",
            (run,),
        )
    assert [j["id"] for j in before] == [j["id"] for j in after]
    assert [j["input_hash"] for j in before] == [j["input_hash"] for j in after]
    assert [j["attempts"] for j in after] == [1, 2]
    completed = [e["window"] for e in await director.trace(run) if e["kind"] == "window_completed"]
    assert completed == [0]
    record(
        failed_window=0,
        resumed_window=1,
        preserved_job_ids=True,
        peer_reexecuted=False,
        completed_windows=completed,
    )


async def test_operator_pause_during_actor_preserves_window_and_saved_output(pool, record):
    entered, release = asyncio.Event(), asyncio.Event()
    actors = {"owner-1": Owner(entered=entered, release=release), "owner-2": Owner()}
    director, run, engine, _, _ = await setup(pool, scenario(), actors=actors)
    task = asyncio.create_task(engine.advance(run))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await director.control(run, "pause")
    finally:
        release.set()
        state = await asyncio.wait_for(task, 5)
    assert state["status"] == "paused" and state["current_window"] == 0
    assert state["result"]["reason"] == "operator_pause"
    async with pool.connection() as conn:
        saved = await rows(
            conn,
            "SELECT owner_id,output FROM run_jobs WHERE run_id=%s AND kind='actor'",
            (run,),
        )
    assert all(j["output"] is not None for j in saved)
    record(current_window=0, pause_reason="operator_pause", actor_outputs_retained=True)


async def test_last_supported_window_is_reached_without_polling_waiting_assistants(pool, record):
    spec = scenario(windows=100, extra=[clock_beat("last-clock", 99)])
    agents = {o.id: Assistant(wait=True) for o in spec.owners}
    director, run, engine, actors, agents = await setup(pool, spec, agents=agents)
    states = []
    for _ in range(100):
        state = await engine.advance(run)
        states.append(state["current_window"])
        if state["status"] != "running":
            break
    assert states == list(range(1, 101))
    assert state["result"]["reason"] == "window_limit"
    assert agents["owner-1"].windows == [0, 99]
    assert agents["owner-2"].windows == [0]
    trace = await director.trace(run)
    cues = [
        e["window"]
        for e in trace
        if e["kind"] == "actor_cue" and e["payload"]["beat_id"] == "last-clock"
    ]
    assert cues == [99]
    assert len([e for e in trace if e["kind"] == "window_completed"]) == 100
    async with pool.connection() as conn:
        snapshots = await rows(
            conn, "SELECT window_index FROM run_boundaries WHERE run_id=%s", (run,)
        )
    assert sorted(s["window_index"] for s in snapshots) == list(range(100))
    record(
        completed_windows=100,
        terminal_reason="window_limit",
        clock_cue_windows=cues,
        assistant_calls={o: a.windows for o, a in agents.items()},
        interpretation="100 logical windows; not seven simulated days or proof of task completion",
    )


async def test_last_window_post_settlement_events_are_explicitly_truncated(pool, record):
    data = scenario(windows=2).model_dump(mode="json")
    data["beats"].append(clock_beat("last-owner-cue", 1))
    data["beats"].append(
        {
            "id": "after-last-message",
            "trigger": {"kind": "owner_message", "source": "actor"},
            "recipients": ["owner-2"],
            "cue": "A new message needs a reply.",
            "requires": ["last-owner-cue"],
        }
    )
    spec = ScenarioSpec.model_validate(data)

    class LateOwner(Owner):
        async def respond(self, view):
            self.silent = view.window == 0
            return await super().respond(view)

    director, run, engine, _, _ = await setup(
        pool, spec, actors={o.id: LateOwner() for o in spec.owners}
    )
    await engine.advance(run)
    state = await engine.advance(run)
    trace = await director.trace(run)
    late = [
        e
        for e in trace
        if e["kind"] == "actor_cue" and e["payload"]["beat_id"] == "after-last-message"
    ]
    assert len(late) == 1
    async with pool.connection() as conn:
        deliveries = await rows(
            conn, "SELECT * FROM event_deliveries WHERE event_id=%s", (late[0]["id"],)
        )
    assert not deliveries
    assert state["status"] == "completed" and state["result"]["reason"] == "window_limit"
    record(
        finding="CLOCK-002",
        classification="horizon truncation; not a settled-success result",
        status=state["status"],
        reason=state["result"]["reason"],
        undelivered_final_cues=len(late),
    )


@pytest.mark.parametrize("recurring_parent", [False, True])
def test_impossible_clock_prerequisite_is_rejected(recurring_parent, record):
    extra = [clock_beat("late-parent", 5), clock_beat("early-child", 2, requires=["late-parent"])]
    if recurring_parent:
        extra = [
            clock_beat("late-root", 5),
            clock_beat("late-parent", 1, every=2, requires=["late-root"]),
            clock_beat("early-child", 3, requires=["late-parent"]),
        ]
    record(
        finding="CLOCK-001",
        requirement="Reject a cue whose clock deadline precedes its earliest prerequisite",
        recurring_parent=recurring_parent,
        declared_beats=extra,
    )
    with pytest.raises(ValidationError, match="reachable|prerequisite|clock"):
        scenario(windows=8, extra=extra)


@pytest.mark.parametrize("child_kind", ["recurring", "event"])
def test_prerequisite_after_expiry_is_rejected(child_kind):
    child = clock_beat("child", 1, every=2, expiry=4, requires=["parent"])
    if child_kind == "event":
        child["trigger"] = {"kind": "owner_message", "source": "actor"}
    with pytest.raises(ValidationError, match="unreachable"):
        scenario(windows=8, extra=[clock_beat("parent", 5), child])


def test_possible_same_window_and_recurring_dependencies_keep_canonical_hash():
    extra = [
        clock_beat("child", 3, requires=["recurring-parent"]),
        clock_beat("recurring-parent", 1, every=2, requires=["parent"]),
        clock_beat("parent", 2),
    ]
    spec = scenario(windows=8, extra=extra)
    assert (
        ScenarioSpec.model_validate_json(spec.canonical_json()).content_hash()
        == spec.content_hash()
    )


async def test_recurring_prerequisite_fires_before_same_window_child(pool, record):
    extra = [
        clock_beat("child", 3, requires=["recurring-parent"]),
        clock_beat("recurring-parent", 1, every=2, requires=["parent"]),
        clock_beat("parent", 2),
    ]
    director, run, engine, _, _ = await setup(pool, scenario(windows=5, extra=extra))
    for _ in range(4):
        await engine.advance(run)
    trace = await director.trace(run)
    clock_cues = [
        (e["payload"]["beat_id"], e["window"])
        for e in trace
        if e["kind"] == "actor_cue"
        and e["payload"]["beat_id"] in {"parent", "recurring-parent", "child"}
    ]
    assert clock_cues == [("parent", 2), ("recurring-parent", 3), ("child", 3)]
    record(clock_cues=clock_cues, same_window_prerequisite_order_preserved=True)
