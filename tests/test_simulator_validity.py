"""Real database checks for Actor fidelity, private views, and saved turn evidence."""

import json
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput, ModelActor, ScriptedActor
from agentrpg.director import Director, emit, row
from agentrpg.evaluation import evaluate_trace
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario
from agentrpg.world import WorldService


class QuietAgent:
    def __init__(self):
        self.inputs = []

    async def run_activation(self, envelope):
        self.inputs.append(envelope)
        return ActivationResult(status="finished", text="No further action is required.")


class CaptureActor(ScriptedActor):
    def __init__(self):
        super().__init__()
        self.inputs = []

    async def respond(self, view):
        self.inputs.append(view)
        return await super().respond(view)


async def setup_run(pool, count=2, spec=None):
    director = Director(pool)
    created = await director.create_run(spec or vehicle_scenario(count))
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    return director, run_id, created


async def test_invalid_actor_grant_has_no_consent_or_world_effect(pool):
    director, run_id, created = await setup_run(pool)

    class ExcessGrant:
        async def respond(self, view):
            return ActorOutput(
                kind="reply",
                text="I approve an amount beyond my role budget.",
                source_event_id=view.source_event_id,
                authorized_amount=view.max_spend + 1,
                allowed_assets=("shared-car",),
            )

    owners = [p["owner_id"] for p in created["participants"]]
    agents = {owner: QuietAgent() for owner in owners}
    engine = RunEngine(pool, actors={owner: ExcessGrant() for owner in owners}, agents=agents)
    before = await WorldService(pool).snapshot(run_id)
    result = await engine.advance(run_id)
    assert result["status"] == "invalid"
    assert result["current_window"] == 0
    assert await WorldService(pool).snapshot(run_id) == before
    assert all(not agent.inputs for agent in agents.values())
    trace = await director.trace(run_id)
    assert any(event["kind"] == "simulator_error" for event in trace)
    assert not any(
        event["kind"] in {"consent_changed", "infrastructure_failure"} for event in trace
    )
    assert (
        evaluate_trace(vehicle_scenario(), trace, run_status=result["status"]).simulator_validity
        == "invalid"
    )


async def test_missing_consequential_fact_invalidates_without_provider_failure(pool):
    director, run_id, _ = await setup_run(pool, count=1)

    class MissingFact:
        async def respond(self, view):
            return ActorOutput(
                kind="request_director_clarification",
                text="This scenario does not specify the requested medical detail.",
                source_event_id=view.source_event_id,
            )

    agent = QuietAgent()
    engine = RunEngine(pool, actors={"owner-1": MissingFact()}, agents={"owner-1": agent})
    result = await engine.advance(run_id)
    assert result["status"] == "invalid"
    assert result["current_window"] == 0
    assert not agent.inputs
    trace = await director.trace(run_id)
    issue = next(event for event in trace if event["kind"] == "simulator_error")
    assert issue["payload"]["reason"] == "missing_consequential_fact"
    assert not any(event["kind"] == "infrastructure_failure" for event in trace)
    assert await director.messages(run_id, "owner-1") == []


async def test_exhausted_model_schema_repair_is_simulator_failure(pool):
    director, run_id, _ = await setup_run(pool, count=1)

    class InvalidModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, config=None):
            self.calls += 1
            return AIMessage(content='{"kind":"reply","owner_id":"forged"}')

    model = InvalidModel()
    actor = ModelActor(model, max_repairs=1)
    result = await RunEngine(
        pool, actors={"owner-1": actor}, agents={"owner-1": QuietAgent()}
    ).advance(run_id)
    assert model.calls == 2
    assert result["status"] == "invalid"
    trace = await director.trace(run_id)
    issue = next(event for event in trace if event["kind"] == "simulator_error")
    assert len(issue["payload"]["attempts"]) == 2
    assert not any(event["kind"] == "infrastructure_failure" for event in trace)


async def test_saved_actor_attempts_survive_process_recreation(pool, monkeypatch):
    director, run_id, _ = await setup_run(pool, count=1)
    attempts = [
        {"attempt": 1, "accepted": False, "reason": "Unknown event."},
        {"attempt": 2, "accepted": True},
    ]

    class RepairedActor(ScriptedActor):
        last_attempts = attempts

    engine = RunEngine(pool, actors={"owner-1": RepairedActor()}, agents={"owner-1": QuietAgent()})

    async def interrupted_apply(*args, **kwargs):
        raise RuntimeError("Process stopped after output save.")

    monkeypatch.setattr(engine, "_apply", interrupted_apply)
    result = await engine.advance(run_id)
    assert result["status"] == "paused"
    async with pool.connection() as conn:
        saved = await row(
            conn, "SELECT * FROM run_jobs WHERE run_id=%s AND kind='actor'", (run_id,)
        )
    assert saved["status"] == "saved"
    assert saved["output_meta"]["attempts"] == attempts

    class MustNotGenerate:
        last_attempts = []

        async def respond(self, view):
            raise AssertionError("Saved output must be used without generation.")

    restarted = RunEngine(
        pool, actors={"owner-1": MustNotGenerate()}, agents={"owner-1": QuietAgent()}
    )
    await director.control(run_id, "resume")
    result = await restarted.advance(run_id)
    assert result["current_window"] == 1
    assert result["status"] in {"running", "completed"}
    trace = await director.trace(run_id)
    turns = [event for event in trace if event["kind"] == "actor_turn"]
    assert len(turns) == 1
    assert turns[0]["payload"]["attempts"] == attempts
    messages = await director.messages(run_id, "owner-1")
    assert len([message for message in messages if message["source"] == "owner"]) == 1


async def test_beat_checks_source_result_and_recipient(pool):
    data = vehicle_scenario().model_dump(mode="json")
    data["beats"].append(
        {
            "id": "assistant-question",
            "trigger": {
                "kind": "owner_message",
                "owner_id": "owner-2",
                "source": "assistant",
            },
            "recipients": ["owner-2"],
            "requires": ["initial-2"],
            "cue": "Respond to the actual assistant question.",
        }
    )
    spec = ScenarioSpec.model_validate(data)
    director, run_id, _ = await setup_run(pool, spec=spec)
    await director.fire_beats(run_id)
    async with pool.connection() as conn:
        await emit(
            conn,
            run_id,
            "owner_message",
            0,
            {"source": "owner", "content": "Owner text"},
            "owner-speech",
            "owner-2",
        )
        await emit(
            conn,
            run_id,
            "reservation_settled",
            0,
            {"status": "accepted", "asset_id": "shared-car"},
            "accepted",
            "owner-2",
        )
        await emit(
            conn,
            run_id,
            "reservation_settled",
            0,
            {"status": "rejected", "asset_id": "shared-car"},
            "wrong-owner",
            "owner-1",
        )
    await director.fire_beats(run_id)
    cues = [event for event in await director.trace(run_id) if event["kind"] == "actor_cue"]
    assert {event["payload"]["beat_id"] for event in cues} == {"initial-1", "initial-2"}
    async with pool.connection() as conn:
        await emit(
            conn,
            run_id,
            "owner_message",
            0,
            {"source": "agent", "content": "Question"},
            "assistant-speech",
            "owner-2",
        )
        await emit(
            conn,
            run_id,
            "reservation_settled",
            0,
            {"status": "rejected", "asset_id": "shared-car"},
            "rejected",
            "owner-2",
        )
    await director.fire_beats(run_id)
    await director.fire_beats(run_id)
    cues = [event for event in await director.trace(run_id) if event["kind"] == "actor_cue"]
    assert len(cues) == 4
    assert all(
        event["owner_id"] == "owner-2"
        for event in cues
        if event["payload"]["beat_id"] in {"pressure-2", "assistant-question"}
    )


async def test_saved_private_views_do_not_disclose_peer_facts_or_plot(pool):
    director, run_id, created = await setup_run(pool)
    actors = {participant["owner_id"]: CaptureActor() for participant in created["participants"]}
    agents = {owner: QuietAgent() for owner in actors}
    async with pool.connection() as conn:
        await emit(
            conn,
            run_id,
            "consent_changed",
            0,
            {"status": "accepted", "private_note": "PRIVATE-CONSENT-2"},
            "private-consent",
            "owner-2",
        )
    result = await RunEngine(pool, actors=actors, agents=agents).advance(run_id)
    assert result["current_window"] == 1
    owner_one = actors["owner-1"].inputs[0].model_dump_json()
    owner_two = actors["owner-2"].inputs[0].model_dump_json()
    assert "clinic appointment" not in owner_one
    assert "clinic appointment" in owner_two
    assert "work meeting 1" not in owner_two
    for owner, agent in agents.items():
        assert len(agent.inputs) == 1
        serialized = agent.inputs[0].model_dump_json()
        assert "clinic appointment" not in serialized
        assert "deadline-reminder" not in serialized
        assert "rubric" not in serialized
        if owner == "owner-1":
            assert "PRIVATE-CONSENT-2" not in serialized
        else:
            assert "PRIVATE-CONSENT-2" in serialized
    trace = await director.trace(run_id)
    for event in trace:
        if event["kind"] == "actor_turn":
            assert event["payload"]["input"]["owner_id"] == event["owner_id"]


async def test_assistant_message_identity_is_preserved_in_actor_view(pool):
    director, run_id, _ = await setup_run(pool, count=1)
    message = await director.send_message(run_id, "owner-1", "May I spend 20 credits?", uuid4())
    actor = CaptureActor()
    await RunEngine(pool, actors={"owner-1": actor}, agents={"owner-1": QuietAgent()}).advance(
        run_id
    )
    conversation = actor.inputs[0].conversation
    assert conversation[0].id == str(message["id"])
    assert conversation[0].role == "assistant"
    assert conversation[0].text == "May I spend 20 credits?"
    assert json.loads(actor.inputs[0].model_dump_json())["conversation"][0]["id"] == str(
        message["id"]
    )


async def test_peer_post_created_in_finished_window_is_delivered_before_exit(pool, client):
    director, run_id, created = await setup_run(pool)
    posted = False

    class PostingAgent(QuietAgent):
        async def run_activation(self, envelope):
            nonlocal posted
            self.inputs.append(envelope)
            if not posted:
                member = created["participants"][0]
                response = await client.post(
                    f"/api/v1/clusters/{created['cluster_id']}/threads",
                    headers={"Authorization": f"Bearer {member['token']}"},
                    json={
                        "request_id": str(uuid4()),
                        "title": "Final alternative",
                        "body": "A permitted option is available; please review it.",
                    },
                )
                assert response.status_code == 201, response.text
                posted = True
            return ActivationResult(status="finished", text="My present work is complete.")

    agents = {"owner-1": PostingAgent(), "owner-2": QuietAgent()}
    engine = RunEngine(pool, actors={owner: ScriptedActor() for owner in agents}, agents=agents)
    result = await engine.advance(run_id)
    assert result["status"] == "running"
    result = await engine.advance(run_id)
    assert any(
        observation.kind == "board_posted"
        for observation in agents["owner-2"].inputs[-1].world_observations
    )
    while result["status"] == "running":
        result = await engine.advance(run_id)
    assert result["status"] == "completed"


@pytest.mark.parametrize("status", ["responded", "waiting"])
async def test_natural_final_question_reaches_actor_and_explicit_send_is_not_duplicated(
    pool, status
):
    director, run_id, _ = await setup_run(pool)

    class AskingAgent:
        def __init__(self, owner, explicit):
            self.owner, self.explicit = owner, explicit

        async def run_activation(self, envelope):
            if envelope.window_id == 0:
                text = "May I spend 20 credits on taxi-2?"
                if self.explicit:
                    await director.send_message(run_id, self.owner, text, uuid4())
                return ActivationResult(status=status, text=text)
            return ActivationResult(status="finished", text="No further action is required.")

    actors = {"owner-1": CaptureActor(), "owner-2": CaptureActor()}
    agents = {"owner-1": AskingAgent("owner-1", False), "owner-2": AskingAgent("owner-2", True)}
    engine = RunEngine(pool, actors=actors, agents=agents)
    await engine.advance(run_id)
    for owner in actors:
        messages = await director.messages(run_id, owner)
        assert len([message for message in messages if message["source"] == "agent"]) == 1
    await engine.advance(run_id)
    for actor in actors.values():
        assert any(
            message.role == "assistant" and message.text == "May I spend 20 credits on taxi-2?"
            for message in actor.inputs[-1].conversation
        )
    snapshot = await WorldService(pool).snapshot(run_id)
    assert all(account["consent_budget"] == 20 for account in snapshot["accounts"])


@pytest.mark.parametrize("status", ["responded", "waiting"])
async def test_long_final_reply_preserves_text_order_and_replays_saved_output(
    pool, monkeypatch, status
):
    from agentrpg import run_engine as module

    director, run_id, _ = await setup_run(pool, count=1)
    # Whitespace at both chunk boundaries and the text ends must survive delivery.
    text = " " + ("a" * 11998) + "\n\n" + ("b" * 11998) + " " + ("c" * 25999) + "\n"
    assert len(text) == 50000

    class LongReply(QuietAgent):
        async def run_activation(self, envelope):
            self.inputs.append(envelope)
            return ActivationResult(status=status, text=text)

    agent = LongReply()
    engine = RunEngine(pool, actors={"owner-1": ScriptedActor()}, agents={"owner-1": agent})
    original_emit = module.emit
    deliveries = 0

    async def stop_during_chunks(conn, run, kind, window, payload, operation, owner=None):
        nonlocal deliveries
        if kind == "owner_message" and payload.get("source") == "agent":
            deliveries += 1
            if deliveries == 2:
                raise RuntimeError("Stopped during chunk delivery.")
        return await original_emit(conn, run, kind, window, payload, operation, owner)

    monkeypatch.setattr(module, "emit", stop_during_chunks)
    assert (await engine.advance(run_id))["status"] == "paused"
    assert not [m for m in await director.messages(run_id, "owner-1") if m["source"] == "agent"]
    monkeypatch.setattr(module, "emit", original_emit)
    await director.control(run_id, "resume")
    await RunEngine(pool, actors={"owner-1": ScriptedActor()}, agents={"owner-1": agent}).advance(
        run_id
    )
    messages = [m for m in await director.messages(run_id, "owner-1") if m["source"] == "agent"]
    assert len(messages) == 5
    assert all(0 < len(message["content"]) <= 12000 for message in messages)
    assert "".join(message["content"] for message in messages) == text
    assert len(agent.inputs) == 1
    events = [
        e
        for e in await director.trace(run_id)
        if e["kind"] == "owner_message" and e["payload"]["source"] == "agent"
    ]
    assert len(events) == 5
    assert {e["payload"]["message_id"] for e in events} == {str(m["id"]) for m in messages}


@pytest.mark.parametrize("status", ["responded", "waiting"])
async def test_partial_final_reply_does_not_suppress_remaining_chunks(pool, monkeypatch, status):
    from uuid import uuid5

    director, run_id, _ = await setup_run(pool, count=1)
    text = "a" * 12000 + "b" * 12000 + "c"

    class LongReply(QuietAgent):
        async def run_activation(self, envelope):
            self.inputs.append(envelope)
            return ActivationResult(status=status, text=text)

    agent = LongReply()
    engine = RunEngine(pool, actors={"owner-1": ScriptedActor()}, agents={"owner-1": agent})
    original_apply = engine._apply

    async def stop_after_saved(job, output, spec, token):
        if job["kind"] == "agent":
            raise RuntimeError("Stopped after output was saved.")
        await original_apply(job, output, spec, token)

    monkeypatch.setattr(engine, "_apply", stop_after_saved)
    assert (await engine.advance(run_id))["status"] == "paused"
    async with pool.connection() as conn:
        job = await row(conn, "SELECT * FROM run_jobs WHERE run_id=%s AND kind='agent'", (run_id,))
    await director.control(run_id, "resume")
    await director.send_message(run_id, "owner-1", text[:12000], uuid5(job["id"], "finalreply"))
    await RunEngine(pool, actors={"owner-1": ScriptedActor()}, agents={"owner-1": agent}).advance(
        run_id
    )
    messages = [m for m in await director.messages(run_id, "owner-1") if m["source"] == "agent"]
    assert len(messages) == 3
    assert "".join(message["content"] for message in messages) == text
    assert len(agent.inputs) == 1


@pytest.mark.parametrize("status", ["responded", "waiting"])
async def test_same_final_reply_is_delivered_again_only_after_new_owner_input(pool, status):
    director, run_id, _ = await setup_run(pool, count=1)
    text = "a" * 12000 + "\nPlease confirm. "

    class RepeatingAgent(QuietAgent):
        async def run_activation(self, envelope):
            self.inputs.append(envelope)
            return ActivationResult(status=status, text=text)

    class AskingOwner:
        async def respond(self, view):
            return ActorOutput(
                kind="reply" if view.window != 1 else "wait",
                text="Please give the current status." if view.window != 1 else "",
                source_event_id=view.source_event_id,
            )

    agent = RepeatingAgent()
    engine = RunEngine(pool, actors={"owner-1": AskingOwner()}, agents={"owner-1": agent})
    for window, expected_messages in enumerate((2, 2, 4)):
        if window == 2:
            async with pool.connection() as conn:
                await emit(
                    conn,
                    run_id,
                    "actor_cue",
                    2,
                    {"cue": "Ask your assistant for another status update."},
                    "repeat-question",
                    "owner-1",
                )
        await engine.advance(run_id)
        messages = [m for m in await director.messages(run_id, "owner-1") if m["source"] == "agent"]
        assert len(messages) == expected_messages
    assert agent.inputs[0].owner_messages
    assert not agent.inputs[1].owner_messages
    assert agent.inputs[2].owner_messages
    assert "".join(m["content"] for m in messages) == text * 2
