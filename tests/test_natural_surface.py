"""The natural surface shows assistants real-life wording and keeps saved evidence unchanged."""

import json
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agentrpg.activation import ActivationEnvelope, OwnerMessage, WorldObservation
from agentrpg.actor import NO_CUE, ActorContractError, ModelActor, build_actor_view
from agentrpg.agent_model import create_model
from agentrpg.agent_surface import (
    INSTRUCTIONS,
    LEGACY,
    NATURAL,
    TOOL_ALIASES,
    present_result,
    session_hash,
)
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime, PersonalAgent
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.scenario_schema import ScenarioSpec

LEAK = re.compile(
    r"director|activation|simulat|scenario|fiction|decision window|\bwindow\b|\brun\b|run_id|"
    r"\bactor\b|evaluat|\btest\b|\bcue\b|\bplot\b|cluster|environment",
    re.I,
)
V2 = ("after_the_verdict_v2", "community_storm_exchange_v2")
V2 += ("vehicle_conflict_v2_2", "vehicle_conflict_v2_15")


class Fake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class StubRuntime(AgentRuntime):
    def __init__(self):
        self.pool, self.lock_pool, self._open = None, None, True


def spec(name):
    return ScenarioSpec.model_validate_json(Path(f"scenarios/{name}.json").read_text())


def natural_agent(surface=NATURAL):
    client = httpx.AsyncClient(base_url="http://127.0.0.1:9/api/v1/")
    return PersonalAgent(
        runtime=StubRuntime(),
        model=Fake(messages=iter([AIMessage(content="ok")])),
        board=BoardTools(client, str(uuid4())),
        environment=EnvironmentTools(client, uuid4()),
        agent_id=uuid4(),
        session_id=uuid4(),
        instructions=INSTRUCTIONS,
        max_model_calls=5,
        timeout_seconds=30,
        callbacks=[],
        surface=surface,
    )


async def test_system_prompt_and_tools_have_no_backstage_terms():
    assert not LEAK.search(INSTRUCTIONS)
    tools = natural_agent()._tools()
    names = {tool.name for tool in tools}
    assert set(TOOL_ALIASES.values()) <= names and not set(TOOL_ALIASES) & names
    for tool in tools:
        schema = tool.args_schema.model_json_schema()
        schema["properties"].pop("tool_call_id", None)
        text = " ".join((tool.name, tool.description, json.dumps(schema)))
        assert not LEAK.search(text), tool.name


async def test_legacy_agents_keep_their_tool_names():
    names = {tool.name for tool in natural_agent(LEGACY)._tools()}
    assert set(TOOL_ALIASES) <= names


def test_activation_reads_as_messages_and_keeps_legacy_text():
    event = uuid4()
    envelope = ActivationEnvelope(
        run_id=uuid4(),
        participant_id=uuid4(),
        activation_id=uuid4(),
        window_id=3,
        source_event_ids=(event,),
        owner_messages=(OwnerMessage(message_id=uuid4(), content="where is this at"),),
        world_observations=(
            WorldObservation(event_id=event, kind="window_reached", content='{"window": 3}'),
        ),
    )
    text = envelope.prompt(NATURAL)
    assert "where is this at" in text and not LEAK.search(text)
    assert envelope.prompt().startswith("DIRECTOR ACTIVATION")
    empty = envelope.model_copy(update={"owner_messages": (), "world_observations": ()})
    assert empty.prompt(NATURAL).startswith("Nothing new") and not LEAK.search(
        empty.prompt(NATURAL)
    )


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("board_posted", {"count": 2, "message_ids": [], "instruction": "Read updates."}),
        ("inbox_received", {"message_id": "m", "subject": "hi", "sender_owner_id": None}),
        ("resource_updated", {"resource_id": "notes", "version": 2}),
        (
            "world_changed",
            {"kind": "branch_restored", "text": "A saved turn boundary was restored"},
        ),
        ("market_settled", {"id": "a", "status": "accepted", "window": 2, "run_id": "r"}),
        ("reservation_settled", {"status": "rejected", "window": 2, "asset_id": "car"}),
    ],
)
def test_notifications_have_no_backstage_terms(kind, payload):
    event = uuid4()
    envelope = ActivationEnvelope(
        run_id=uuid4(),
        participant_id=uuid4(),
        activation_id=uuid4(),
        window_id=0,
        source_event_ids=(event,),
        world_observations=(
            WorldObservation(event_id=event, kind=kind, content=json.dumps(payload)),
        ),
    )
    assert not LEAK.search(envelope.prompt(NATURAL))


def test_model_result_hides_run_fields_and_leaves_the_saved_result_unchanged():
    saved = {
        "ok": True,
        "data": {
            "id": "m",
            "status": "pending",
            "run_id": "r",
            "window": 1,
            "created_at": "x",
            "read_at": None,
            "sender_source": "director",
            "consent_expires_window": 3,
            "items": [{"cluster_id": "c", "body": "kept"}],
        },
    }
    original = json.loads(json.dumps(saved))
    shown = present_result("read_mail", saved)
    assert saved == original
    assert shown["data"] == {
        "id": "m",
        "status": "pending",
        "read": False,
        "sender_type": "outside contact",
        "items": [{"body": "kept"}],
    }


def test_run_view_and_errors_use_plain_terms():
    view = {
        "owner_id": "mara",
        "owner_name": "Mara",
        "agent_id": "a",
        "run_id": "r",
        "cluster_id": "c",
        "session_id": "s",
        "status": "running",
        "current_window": 2,
        "participants": [{"owner_id": "elias", "name": "Elias"}],
    }
    assert present_result("read_run_view", {"ok": True, "data": view})["data"] == {
        "owner_id": "mara",
        "owner_name": "Mara",
        "your_board_id": "a",
        "contacts": [{"owner_id": "elias", "name": "Elias"}],
    }
    for detail in (
        "The mail or recipient was not found. Use owner_id from read_run_view.participants, "
        "not a board Agent UUID.",
        "The operation conflicts with the current environment state.",
        "The environment could not be reached. Retry later.",
    ):
        shown = present_result("x", {"ok": False, "error": {"status": 409, "detail": detail}})
        assert not LEAK.search(shown["error"]["detail"]) and "read_run_view" not in str(shown)


def test_session_hash_keeps_saved_legacy_value_and_separates_surfaces():
    import hashlib

    legacy = hashlib.sha256(
        json.dumps(
            {"instructions": "i", "run_id": "r", "tools_version": 1}, sort_keys=True
        ).encode()
    ).hexdigest()
    assert session_hash("i", "r", LEGACY) == legacy
    assert session_hash("i", "r", NATURAL) != legacy


@pytest.mark.parametrize("name", V2)
def test_v2_text_people_read_has_no_backstage_terms(name):
    scenario = spec(name)
    # "the plot" and tool-level words are checked on everything an owner or assistant can read.
    banned = re.compile(r"simulat|fiction|scenario|decision window|\bcredits?\b|owner-\d", re.I)
    readable = [o.request for o in scenario.owners] + [o.name for o in scenario.owners]
    readable += [f"{m.subject} {m.body}" for m in scenario.initial_mail]
    readable += [f"{r.title} {r.content}" for r in scenario.resources]
    for beat in scenario.beats:
        readable += [f"{m.subject} {m.body}" for m in beat.mail]
        readable += [e.operation.content or "" for e in beat.resource_effects]
    readable += [fact.text for fact in scenario.facts]
    for text in readable:
        assert not banned.search(text), text
    assert all(owner.voice for owner in scenario.owners)


def test_v2_requests_are_shorter_than_v1_and_v1_hash_is_unchanged():
    old = spec("after_the_verdict")
    assert old.content_hash() == "af0ef00dc3c913956537128beab7ad981c0d8c721cc25f9542a0e1a9a913499e"
    new = spec("after_the_verdict_v2")
    assert {owner.id for owner in new.owners} == {"mara", "elias"}
    assert len(new.beats) == len(old.beats)
    storm_old, storm_new = spec("community_storm_exchange"), spec("community_storm_exchange_v2")
    for before, after in zip(storm_old.owners, storm_new.owners, strict=True):
        assert len(after.request) < len(before.request) / 2
    assert len({owner.request for owner in spec("vehicle_conflict_v2_15").owners}) == 15


def test_v2_voice_reaches_only_its_own_actor():
    scenario = spec("after_the_verdict_v2")
    view = build_actor_view(scenario, "mara", "Say it your way.")
    assert view.voice.startswith("Mara") and "Elias, 44" not in view.model_dump_json()


def test_actor_temperature_is_configurable_and_defaults_to_zero():
    assert create_model("test-key").temperature == 0
    assert create_model("test-key", temperature=0.8).temperature == 0.8
    with pytest.raises(ValueError):
        create_model("test-key", temperature=3)


async def test_natural_actor_rejects_backstage_words_and_keeps_declared_terms():
    def view(request="i need a ride"):
        base = spec("vehicle_conflict_v2_2")
        return build_actor_view(base, "priya", "Tell it.", source_event_id="e").model_copy(
            update={"request": request}
        )

    def model(*texts):
        return GenericFakeChatModel(messages=iter([AIMessage(content=text) for text in texts]))

    leaked = json.dumps({"kind": "reply", "text": "this simulation is odd"})
    fixed = json.dumps({"kind": "reply", "text": "need a ride tmrw"})
    actor = ModelActor(model(leaked, fixed), max_repairs=1, natural=True)
    assert (await actor.respond(view())).text == "need a ride tmrw"
    assert actor.last_attempts[0]["accepted"] is False

    with pytest.raises(ActorContractError):
        await ModelActor(model(leaked), max_repairs=0, natural=True).respond(view())

    declared = json.dumps({"kind": "reply", "text": "offer it for 5 credits"})
    actor = ModelActor(model(declared), max_repairs=0, natural=True)
    assert (await actor.respond(view("offer it for 5 credits"))).kind == "reply"
    # Legacy Actors keep their original contract for saved and resumed runs.
    actor = ModelActor(model(leaked), max_repairs=0)
    assert (await actor.respond(view())).text == "this simulation is odd"


async def test_natural_actor_may_stay_silent_only_without_a_new_cue():
    def model(*texts):
        return GenericFakeChatModel(messages=iter([AIMessage(content=text) for text in texts]))

    wait = json.dumps({"kind": "wait"})
    base = spec("vehicle_conflict_v2_2")
    quiet = build_actor_view(base, "dana", NO_CUE, source_event_id="e")
    assert (
        await ModelActor(model(wait), max_repairs=0, natural=True).respond(quiet)
    ).kind == "wait"
    cued = build_actor_view(base, "dana", "It's getting close.", source_event_id="e")
    with pytest.raises(ActorContractError):
        await ModelActor(model(wait), max_repairs=0, natural=True).respond(cued)


def test_prompt_separates_owner_documents_from_scratch_tools():
    assert "read_file" in INSTRUCTIONS and "fetch_resource" in INSTRUCTIONS
    assert "submit_booking" in INSTRUCTIONS and "in_reply_to" in INSTRUCTIONS
