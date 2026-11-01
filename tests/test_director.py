"""Real PostgreSQL checks for scenes, private chat, windows, and recovery."""

import asyncio
from uuid import UUID, uuid4, uuid5

import pytest

from agentrpg.activation import ActivationResult
from agentrpg.actor import ScriptedActor
from agentrpg.director import Director, emit, row, rows
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.world import WorldService


def bearer(record):
    return {"Authorization": f"Bearer {record['token']}"}


@pytest.mark.parametrize("count", [2, 15])
async def test_create_private_views_and_isolated_runs(client, count):
    spec = vehicle_scenario(count)
    response = await client.post("/api/v1/runs", json=spec.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    run = response.json()
    other = (await client.post("/api/v1/runs", json=spec.model_dump(mode="json"))).json()
    assert run["cluster_id"] != other["cluster_id"]
    assert len(run["participants"]) == count
    for participant in run["participants"]:
        headers = bearer(participant)
        view = await client.get(f"/api/v1/runs/{run['run_id']}/view", headers=headers)
        assert view.status_code == 200, view.text
        assert view.json()["owner_id"] == participant["owner_id"]
        assert "scenario" not in view.json()
        assert "clinic" not in view.text
        for path in ("", "/trace", "/report"):
            assert (
                await client.get(f"/api/v1/runs/{run['run_id']}{path}", headers=headers)
            ).status_code == 403
        assert (
            await client.get(f"/api/v1/runs/{other['run_id']}/view", headers=headers)
        ).status_code == 404


async def test_owner_chat_and_board_outbox(client, pool):
    run = (
        await client.post("/api/v1/runs", json=vehicle_scenario().model_dump(mode="json"))
    ).json()
    run_id = run["run_id"]
    assert (await client.post(f"/api/v1/runs/{run_id}/control/start")).status_code == 200
    one, two = run["participants"]
    payload = {"request_id": str(uuid4()), "content": "May I spend 20 credits on taxi-1?"}
    path = f"/api/v1/runs/{run_id}/owner/messages"
    original = await client.post(path, json=payload, headers=bearer(one))
    assert original.status_code == 200, original.text
    assert (await client.post(path, json=payload, headers=bearer(one))).json() == original.json()
    assert (
        await client.post(path, json={**payload, "content": "changed"}, headers=bearer(one))
    ).status_code == 409
    assert (await client.get(path, headers=bearer(two))).json() == {"items": []}
    assert (
        await client.post(path, json={**payload, "owner_id": two["owner_id"]}, headers=bearer(one))
    ).status_code == 422
    posted = await client.post(
        f"/api/v1/clusters/{run['cluster_id']}/threads",
        headers=bearer(one),
        json={
            "request_id": str(uuid4()),
            "title": "Transport",
            "body": "Please discuss the shared car.",
        },
    )
    assert posted.status_code == 201, posted.text
    events = await Director(pool).trace(UUID(run_id))
    assert len([e for e in events if e["kind"] == "owner_message"]) == 1
    event = next(e for e in events if e["kind"] == "board_posted")
    assert event["payload"]["message_id"] == posted.json()["id"]
    assert event["owner_id"] is None


async def test_scene_beats_are_once_and_conditioned(pool):
    director = Director(pool)
    spec = vehicle_scenario()
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    await asyncio.gather(*(director.fire_beats(run_id) for _ in range(5)))
    events = await director.trace(run_id)
    assert len([e for e in events if e["kind"] == "actor_cue"]) == 2
    assert all(e["owner_id"] for e in events if e["kind"] == "actor_cue")
    async with pool.connection() as conn:
        await emit(
            conn,
            run_id,
            "reservation_settled",
            0,
            {"status": "rejected", "asset_id": "shared-car"},
            "rejected-car",
            "owner-2",
        )
    await director.fire_beats(run_id)
    await director.fire_beats(run_id)
    events = await director.trace(run_id)
    assert (
        len(
            [
                e
                for e in events
                if e["kind"] == "actor_cue" and e["payload"]["beat_id"] == "pressure-2"
            ]
        )
        == 1
    )


class TransportAgent:
    """Test policy using the real HTTP and world paths, without model calls."""

    def __init__(self, client, pool, run, participant, barrier=None):
        self.client, self.pool, self.run, self.participant = client, pool, run, participant
        self.calls = []
        self.barrier = barrier

    async def run_activation(self, envelope):
        self.calls.append(envelope)
        if self.barrier and envelope.window_id == 0:
            await self.barrier.wait()
        run_id = self.run["run_id"]
        member = self.participant
        index = int(member["owner_id"].split("-")[1])
        headers = bearer(member)
        if envelope.window_id == 0:
            response = await self.client.post(
                f"/api/v1/runs/{run_id}/owner/messages",
                headers=headers,
                json={
                    "request_id": str(uuid5(envelope.activation_id, "ask")),
                    "content": f"May I spend 20 credits on taxi-{index}? Please approve.",
                },
            )
            assert response.status_code == 200, response.text
            response = await self.client.post(
                f"/api/v1/clusters/{self.run['cluster_id']}/threads",
                headers=headers,
                json={
                    "request_id": str(uuid5(envelope.activation_id, "board")),
                    "title": "Transport",
                    "body": f"I will use taxi-{index}; keep the shared car.",
                },
            )
            assert response.status_code == 201, response.text
            return ActivationResult(status="responded", text="Asked owner and notified peers.")
        if envelope.window_id == 1:
            response = await self.client.post(
                f"/api/v1/runs/{run_id}/actions",
                headers=headers,
                json={
                    "request_id": str(uuid5(envelope.activation_id, "reserve")),
                    "kind": "reserve",
                    "asset_id": f"taxi-{index}",
                },
            )
            assert response.status_code == 200, response.text
            return ActivationResult(status="waiting", pending_ids=(UUID(response.json()["id"]),))
        return ActivationResult(status="finished", text="Reservation completed.")


@pytest.mark.parametrize("count", [2, 15])
async def test_complete_owner_board_settlement_path(client, pool, count):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario(count))
    run_id = UUID(created["run_id"])
    barrier = asyncio.Barrier(count)
    agents = {
        p["owner_id"]: TransportAgent(client, pool, created, p, barrier)
        for p in created["participants"]
    }
    engine = RunEngine(pool, actors={o: ScriptedActor() for o in agents}, agents=agents)
    result = await engine.run(run_id)
    assert result["status"] == "completed", result
    snapshot = await WorldService(pool).snapshot(run_id)
    assert len(snapshot["reservations"]) == count + 1
    assert all(a["balance"] == 0 for a in snapshot["accounts"])
    trace = await director.trace(run_id)
    assert any(e["kind"] == "world_changed" for e in trace)
    assert len([e for e in trace if e["kind"] == "board_posted"]) == count
    for agent in agents.values():
        inputs = [m.content for e in agent.calls for m in e.owner_messages]
        assert any("authorize" in text for text in inputs)
        assert all("clinic" not in text for text in inputs)
        assert any(e.kind == "board_posted" for a in agent.calls for e in a.world_observations)
    assert WorldService.replay(await WorldService(pool).journal(run_id)) == snapshot


async def test_saved_actor_output_recovery_does_not_call_actor_again(client, pool, monkeypatch):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario())
    run_id = UUID(created["run_id"])
    actors = {p["owner_id"]: ScriptedActor() for p in created["participants"]}
    agents = {
        p["owner_id"]: TransportAgent(client, pool, created, p) for p in created["participants"]
    }
    engine = RunEngine(pool, actors=actors, agents=agents)
    real_apply = engine._apply
    failed = False

    async def fail_once(job, output, spec, token):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("Simulated crash after output save")
        return await real_apply(job, output, spec, token)

    monkeypatch.setattr(engine, "_apply", fail_once)
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert result["status"] == "paused"
    assert result["current_window"] == 0
    async with pool.connection() as conn:
        saved = await row(
            conn, "SELECT * FROM run_jobs WHERE run_id=%s AND status='saved'", (run_id,)
        )
    assert saved["output"] is not None

    async def unexpected(view):
        raise AssertionError("Accepted Actor output must be replayed.")

    monkeypatch.setattr(actors[saved["owner_id"]], "respond", unexpected)
    await director.control(run_id, "resume")
    result = await engine.advance(run_id)
    assert result["status"] == "running"
    assert result["current_window"] == 1
    async with pool.connection() as conn:
        messages = await rows(
            conn, "SELECT * FROM owner_messages WHERE run_id=%s AND source='owner'", (run_id,)
        )
    assert len(messages) == 2


async def test_provider_failure_does_not_advance_clock(pool):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario())
    run_id = UUID(created["run_id"])

    class FailingActor:
        async def respond(self, view):
            raise RuntimeError("Provider unavailable")

    actors = {p["owner_id"]: FailingActor() for p in created["participants"]}
    engine = RunEngine(pool, actors=actors, agents={o: object() for o in actors})
    await director.control(run_id, "start")
    for attempt in range(3):
        result = await engine.advance(run_id)
        assert result["current_window"] == 0
        assert result["status"] == ("incomplete" if attempt == 2 else "paused")
        if attempt < 2:
            await director.control(run_id, "resume")


async def test_recurring_clock_beats_are_bounded_and_not_backfilled(pool):
    from agentrpg.scenario_schema import BeatSpec, Trigger

    base = vehicle_scenario(1)
    recurring = BeatSpec(
        id="recurring",
        trigger=Trigger(kind="window_reached", window=0, every_windows=2),
        recipients=("owner-1",),
        cue="Send a status reminder.",
        max_uses=3,
    )
    spec = type(base).model_validate({**base.model_dump(), "beats": (*base.beats, recurring)})
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    for window in range(8):
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE environment_runs SET current_window=%s WHERE id=%s", (window, run_id)
            )
        await director.fire_beats(run_id)
        await director.fire_beats(run_id)
    events = await director.trace(run_id)
    assert [
        e["window"]
        for e in events
        if e["kind"] == "actor_cue" and e["payload"]["beat_id"] == "recurring"
    ] == [0, 2, 4]


async def test_many_board_events_are_coalesced_without_acknowledging_messages(client, pool):
    director = Director(pool)
    spec = vehicle_scenario(2)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    await director.fire_beats(run_id)
    author = created["participants"][0]
    # These are real board commits and durable outbox notifications.
    for index in range(120):
        result = await client.post(
            f"/api/v1/clusters/{created['cluster_id']}/threads",
            headers=bearer(author),
            json={
                "request_id": str(uuid4()),
                "title": f"Proposal {index}",
                "body": "Public transport proposal.",
            },
        )
        assert result.status_code == 201, result.text
    engine = RunEngine(pool, actors={}, agents={})
    await engine._prepare(run_id, spec, await director.participants(run_id), 0, "agent")
    async with pool.connection() as conn:
        job = await row(
            conn, "SELECT input FROM run_jobs WHERE run_id=%s AND owner_id='owner-2'", (run_id,)
        )
        deliveries = await row(
            conn,
            "SELECT count(*) AS n FROM event_deliveries d JOIN run_events e "
            "ON e.id=d.event_id WHERE d.run_id=%s AND d.owner_id='owner-2' "
            "AND e.kind='board_posted'",
            (run_id,),
        )
        acknowledgements = await row(
            conn,
            "SELECT count(*) AS n FROM message_receipts r "
            "JOIN messages m ON m.id=r.message_id WHERE m.cluster_id=%s",
            (created["cluster_id"],),
        )
    assert len(job["input"]["source_event_ids"]) == 2
    assert deliveries["n"] == 120
    assert acknowledgements["n"] == 0


async def test_activation_limit_has_explicit_measured_outcome(client, pool):
    from agentrpg.scenario_schema import RunLimits, ScenarioSpec

    base = vehicle_scenario(1)
    spec = ScenarioSpec.model_validate(
        {**base.model_dump(), "limits": RunLimits(max_agent_activations_per_owner=1).model_dump()}
    )
    director = Director(pool)
    created = await director.create_run(spec)
    agents = {
        p["owner_id"]: TransportAgent(client, pool, created, p) for p in created["participants"]
    }
    run = await RunEngine(pool, actors={o: ScriptedActor() for o in agents}, agents=agents).run(
        UUID(created["run_id"])
    )
    assert run["status"] == "completed"
    assert run["result"]["reason"] == "activation_limit"


@pytest.mark.parametrize(
    "case,expected_window,expected_cues",
    [("future", 4, [3]), ("recurring", 5, [2, 4]), ("expired", 2, [1]), ("blocked", 1, [])],
)
async def test_finished_assistant_waits_for_reachable_future_plot_beats(
    pool, case, expected_window, expected_cues
):
    from agentrpg.scenario_schema import BeatSpec, ScenarioSpec, Trigger

    class FinishedAgent:
        async def run_activation(self, envelope):
            return ActivationResult(status="finished")

    opening = BeatSpec(
        id="opening",
        trigger=Trigger(kind="run_started"),
        recipients=("owner-1",),
        cue="State your request.",
    )
    extra = []
    if case == "recurring":
        trigger, expiry, uses, cooldown, required = (
            Trigger(kind="window_reached", window=2, every_windows=2),
            None,
            2,
            0,
            ("opening",),
        )
    elif case == "expired":
        trigger, expiry, uses, cooldown, required = (
            Trigger(kind="window_reached", window=1, every_windows=1),
            2,
            5,
            2,
            ("opening",),
        )
    else:
        trigger, expiry, uses, cooldown, required = (
            Trigger(kind="window_reached", window=3),
            None,
            1,
            0,
            ("opening",),
        )
        if case == "blocked":
            extra.append(
                BeatSpec(
                    id="unmet",
                    trigger=Trigger(kind="reservation_settled", result_status="rejected"),
                    recipients=("owner-1",),
                    cue="Respond to a rejected request.",
                )
            )
            required = ("unmet",)
    future = BeatSpec(
        id="future",
        trigger=trigger,
        recipients=("owner-1",),
        cue="Give a status reminder.",
        requires=required,
        expires_window=expiry,
        max_uses=uses,
        cooldown_windows=cooldown,
    )
    base = vehicle_scenario(1)
    spec = ScenarioSpec.model_validate(
        {**base.model_dump(), "max_windows": 7, "beats": [opening, *extra, future]}
    )
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    engine = RunEngine(
        pool, actors={"owner-1": ScriptedActor()}, agents={"owner-1": FinishedAgent()}
    )
    result = await engine.run(run_id)
    assert result["status"] == "completed"
    assert result["current_window"] == expected_window
    trace = await director.trace(run_id)
    assert [
        event["window"]
        for event in trace
        if event["kind"] == "actor_cue" and event["payload"]["beat_id"] == "future"
    ] == expected_cues


async def test_actor_current_world_snapshot_identifies_its_owner(pool):
    import json

    director = Director(pool)
    spec = vehicle_scenario(1)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    await director.fire_beats(run_id)
    engine = RunEngine(pool, actors={}, agents={})
    await engine._prepare(run_id, spec, await director.participants(run_id), 0, "actor")
    async with pool.connection() as conn:
        job = await row(conn, "SELECT input FROM run_jobs WHERE run_id=%s", (run_id,))
    snapshot = next(
        observation
        for observation in job["input"]["observations"]
        if observation["kind"] == "current_world"
    )
    assert json.loads(snapshot["text"])["owner_id"] == job["input"]["owner_id"]


async def test_same_window_prerequisite_fires_before_child_in_reverse_declaration_order(pool):
    from agentrpg.scenario_schema import BeatSpec, ScenarioSpec, Trigger

    child = BeatSpec(
        id="child",
        trigger=Trigger(kind="window_reached", window=0),
        recipients=("owner-1",),
        cue="Follow the first scene.",
        requires=("parent",),
    )
    parent = BeatSpec(
        id="parent",
        trigger=Trigger(kind="run_started"),
        recipients=("owner-1",),
        cue="Introduce the first scene.",
    )
    base = vehicle_scenario(1)
    spec = ScenarioSpec.model_validate({**base.model_dump(), "beats": [child, parent]})
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    await director.fire_beats(run_id)
    trace = await director.trace(run_id)
    assert [e["payload"]["beat_id"] for e in trace if e["kind"] == "actor_cue"] == [
        "parent",
        "child",
    ]


async def test_operator_pause_overrides_failure_reason_until_explicit_resume(pool):
    from psycopg.types.json import Jsonb

    director = Director(pool)
    created = await director.create_run(vehicle_scenario(1))
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET status='paused',result=%s WHERE id=%s",
            (Jsonb({"reason": "worker_failure"}), run_id),
        )
    paused = await director.control(run_id, "pause")
    assert paused["status"] == "paused" and paused["result"] == {"reason": "operator_pause"}
    resumed = await director.control(run_id, "resume")
    assert resumed["status"] == "running" and resumed["result"] is None


async def test_operator_pause_wins_race_with_worker_failure_summary(pool, monkeypatch):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario(1))
    run_id = UUID(created["run_id"])
    failed, paused = False, False

    class FailingActor:
        async def respond(self, view):
            nonlocal failed
            failed = True
            raise RuntimeError("Simulated provider failure")

    engine = RunEngine(pool, actors={"owner-1": FailingActor()}, agents={"owner-1": object()})
    read_run = engine.director.get_run

    async def pause_after_running_snapshot(identifier):
        nonlocal paused
        result = await read_run(identifier)
        if failed and not paused and result["status"] == "running":
            paused = True
            await director.control(identifier, "pause")
        return result

    monkeypatch.setattr(engine.director, "get_run", pause_after_running_snapshot)
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert paused and result["status"] == "paused"
    assert result["result"] == {"reason": "operator_pause"}
    assert not any(
        event["kind"] == "infrastructure_failure" for event in await director.trace(run_id)
    )


async def test_operator_pause_wins_race_with_input_contract_failure(pool, monkeypatch):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario(1))
    run_id = UUID(created["run_id"])
    engine = RunEngine(pool, actors={"owner-1": object()}, agents={"owner-1": object()})

    async def pause_then_fail(*args, **kwargs):
        await director.control(run_id, "pause")
        raise ValueError("Simulated participant input error")

    monkeypatch.setattr(engine, "_prepare", pause_then_fail)
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert result["status"] == "paused" and result["result"] == {"reason": "operator_pause"}
    assert not any(event["kind"] == "simulator_error" for event in await director.trace(run_id))


async def test_queued_actor_cue_keeps_run_open_until_delivery(pool):
    from agentrpg.scenario_schema import ScenarioSpec

    base = vehicle_scenario(1)
    spec = ScenarioSpec.model_validate(
        {
            **base.model_dump(),
            "beats": [beat for beat in base.beats if beat.trigger.kind == "run_started"],
        }
    )
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])

    class RecordingActor(ScriptedActor):
        def __init__(self):
            super().__init__()
            self.views = []

        async def respond(self, view):
            self.views.append(view)
            return await super().respond(view)

    class FinishedAgent:
        async def run_activation(self, envelope):
            if envelope.window_id == 0:
                # A resumed Director can produce a cue after this window's Actor
                # input has been frozen. Deliver it in the next Actor turn.
                async with pool.connection() as conn:
                    await emit(
                        conn,
                        run_id,
                        "actor_cue",
                        0,
                        {"beat_id": "late-cue", "cue": "Provide the late scene reminder."},
                        "test:late-cue",
                        "owner-1",
                    )
            return ActivationResult(status="finished")

    actor = RecordingActor()
    engine = RunEngine(pool, actors={"owner-1": actor}, agents={"owner-1": FinishedAgent()})
    await director.control(run_id, "start")
    first = await engine.advance(run_id)
    assert first["status"] == "running" and first["current_window"] == 1
    final = await engine.advance(run_id)
    assert final["status"] == "completed" and final["current_window"] == 2
    assert actor.views[-1].cue == "Provide the late scene reminder."


@pytest.mark.parametrize("pause_after_settlement", [False, True])
async def test_consumed_owner_event_fires_plot_before_early_completion(
    pool, monkeypatch, pause_after_settlement
):
    from agentrpg.scenario_schema import BeatSpec, ScenarioSpec, Trigger

    base = vehicle_scenario(1)
    followup = BeatSpec(
        id="owner-followup",
        trigger=Trigger(kind="owner_message", source="actor", owner_id="owner-1"),
        recipients=("owner-1",),
        cue="Give a status reminder after the first request.",
    )
    spec = ScenarioSpec.model_validate(
        {
            **base.model_dump(),
            "beats": [
                *[beat for beat in base.beats if beat.trigger.kind == "run_started"],
                followup,
            ],
        }
    )
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])

    class RecordingActor(ScriptedActor):
        def __init__(self):
            super().__init__()
            self.views = []

        async def respond(self, view):
            self.views.append(view)
            return await super().respond(view)

    class FinishedAgent:
        def __init__(self):
            self.calls = []

        async def run_activation(self, envelope):
            self.calls.append(envelope)
            return ActivationResult(status="finished")

    actor, agent = RecordingActor(), FinishedAgent()
    engine = RunEngine(pool, actors={"owner-1": actor}, agents={"owner-1": agent})
    settle = engine.world.settle
    did_pause = False

    async def settle_then_pause(*args):
        nonlocal did_pause
        result = await settle(*args)
        if pause_after_settlement and not did_pause:
            did_pause = True
            await director.control(run_id, "pause")
        return result

    monkeypatch.setattr(engine.world, "settle", settle_then_pause)
    await director.control(run_id, "start")
    first = await engine.advance(run_id)
    assert agent.calls[0].owner_messages
    if pause_after_settlement:
        assert first["status"] == "paused" and first["current_window"] == 0
        assert first["result"] == {"reason": "operator_pause"}
        assert not any(
            event["kind"] == "actor_cue" and event["payload"]["beat_id"] == followup.id
            for event in await director.trace(run_id)
        )
        await director.control(run_id, "resume")
        first = await engine.advance(run_id)
    assert first["status"] == "running" and first["current_window"] == 1
    assert len(actor.views) == len(agent.calls) == 1
    final = await engine.advance(run_id)
    assert final["status"] == "completed" and final["current_window"] == 2
    assert actor.views[-1].cue == followup.cue
    cues = [
        event
        for event in await director.trace(run_id)
        if event["kind"] == "actor_cue" and event["payload"]["beat_id"] == followup.id
    ]
    assert len(cues) == 1
    assert cues[0]["window"] == 0
    async with pool.connection() as conn:
        delivery = await row(
            conn,
            'SELECT j."window" FROM event_deliveries d JOIN run_jobs j ON j.id=d.job_id '
            "WHERE d.event_id=%s AND d.recipient_kind='actor'",
            (cues[0]["id"],),
        )
    assert delivery["window"] == 1


@pytest.mark.parametrize("changed_window", [0, 3])
async def test_completion_after_plot_evaluation_fences_changed_window_and_phase(
    pool, monkeypatch, changed_window
):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario(1))
    run_id = UUID(created["run_id"])

    class FinishedAgent:
        async def run_activation(self, envelope):
            return ActivationResult(status="finished")

    engine = RunEngine(
        pool, actors={"owner-1": ScriptedActor()}, agents={"owner-1": FinishedAgent()}
    )
    fire_beats = engine.director.fire_beats
    calls = 0

    async def fire_then_change_run(identifier):
        nonlocal calls
        await fire_beats(identifier)
        calls += 1
        if calls == 2:
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE environment_runs SET current_window=%s,phase='actors' WHERE id=%s",
                    (changed_window, identifier),
                )

    monkeypatch.setattr(engine.director, "fire_beats", fire_then_change_run)
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert calls == 2
    assert result["status"] == "running" and result["phase"] == "actors"
    assert result["current_window"] == changed_window
