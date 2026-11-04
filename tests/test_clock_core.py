"""Clock contracts, arithmetic, and the gate. Database tests use real PostgreSQL."""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from pydantic import ValidationError

from agentrpg.activation import (
    ActivationEnvelope,
    ActivationResult,
    ClockedActivationResult,
    OwnerMessage,
)
from agentrpg.actor import (
    CLOCK_DAY,
    ActorCandidate,
    ActorOutput,
    ClockedActorCandidate,
    ModelActor,
    build_actor_view,
)
from agentrpg.agent_surface import (
    CLOCKED_END_TURN_DESCRIPTION,
    ClockedEndTurn,
    EndTurn,
    clock_instructions,
    present_result,
    render_activation,
)
from agentrpg.clock import (
    ClockConfig,
    cancel,
    cancel_event,
    classify_actor_event,
    classify_agent_event,
    fold_stamps,
    format_time,
    next_daily_wake,
    next_event,
    notice_delay_s,
    ordered,
    parse_at,
    price_actor,
    price_think,
    price_tool,
    retime,
    schedule,
    tie,
    until_after_yield,
)
from agentrpg.clock_gate import ClockGate
from agentrpg.director import Director
from agentrpg.scenario_schema import ScenarioSpec

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
# Saved before this feature existed. Saved runs and their evidence depend on these values.
FROZEN_HASHES = {
    "after_the_verdict.json": "af0ef00dc3c913956537128beab7ad981c0d8c721cc25f9542a0e1a9a913499e",
    "after_the_verdict_v2.json": "f82b8c474c7795107367ebdb872ff7a9998fa123ca2f528b3439fd9a5dd440ad",
    "community_storm_exchange.json": (
        "4628cca8567281ed9ffe51494676d139eaa2b93436723025ad981aa56659192a"
    ),
    "community_storm_exchange_v2.json": (
        "1f9085ea651e949a11f794a865ac725802d626e2628d8890931789907c1d9a50"
    ),
    "extension_control_15.json": "8a410e10e1783c1becc65e714bc8d3aa28cd1cbeda1e25a19b70d4edf7c67a95",
    "extension_control_2.json": "51900b86fce16e78a32f0cecd663067ce416cf4ac2fcc609467f8cc94c2ba46c",
    "friends_agent_renewal_funding.json": (
        "6f9fe38c4660c73c3d1e4d1ed83ca80f76f1a2fac45f2661e12ab1c50d1b2d5c"
    ),
    "friends_lunch_actors.json": "0918b9e9fc8d6979adab167501474e955e33b43bbfe5f142445a9e14268cca6c",
    "friends_lunch_ambiguous.json": (
        "e097da30900c59c54c5b568af5910bd3dec27a0fc86d764b1ccfd11df3d181ba"
    ),
    "friends_lunch_duplicate.json": (
        "d18c38444d9490374e85de5be49747733de9153a57024dd020cc5c43828425df"
    ),
    "friends_lunch_insufficient.json": (
        "65bf566f3cce342474b44ba74e4c1e9a9133e23302aff5aaac7af83b6dc98820"
    ),
    "generated-transport-reviewed.json": (
        "7a35d856cca45aa599ba9b6f0d6b6b9d8b167dc8ae0e61685bfaa4e92e0441a0"
    ),
    "vehicle-conflict-15.json": "cbccfa45dad80b973609f4693a0408ce2d1b3c3646cd355c6f92546a8e5b31c5",
    "vehicle-conflict-2.json": "93b0a93337f79036a1fc4181775bdc42ef2a1eafc3092daaf70a90eb0ee1c410",
    "vehicle-control-15.json": "4adebff94a0621fd24db265d2227f720d6415654b06d9485cb39d3ad7dec9fa1",
    "vehicle_conflict_v2_15.json": (
        "526d972b2f17df384acd1e456d1643f8a8a3a59e74892ef7aa1d41b046244f79"
    ),
    "vehicle_conflict_v2_2.json": (
        "5617b9e6bd2571906a75fdcda639454e611147ce3a2bcb976ebe2775e4fddf6d"
    ),
}
FORBIDDEN = (
    "window",
    "tick",
    "beat",
    " cue",
    "director",
    "actor",
    "scenario",
    "simulation",
    "simulated",
    "turn-count",
)
START = "2026-11-09T06:00:00+00:00"  # A Monday.


def config(**values):
    section = {"start_at": START, "duration_minutes": 10080, "epoch_minutes": 120, **values}
    return ClockConfig.from_section(section, 7)


def scenario_data(**clock):
    return {
        "id": "clock-schema",
        "name": "Clock schema",
        "owners": [
            {"id": "dan", "name": "Dan", "request": "Help.", "goals": ["Be well."]},
            {"id": "mei", "name": "Mei", "request": "Help.", "goals": ["Be well."]},
        ],
        "beats": [
            {
                "id": "start",
                "trigger": {"kind": "run_started"},
                "recipients": ["dan", "mei"],
                "cue": "Begin.",
            }
        ],
        "world": {"balances": {"dan": 10, "mei": 10}},
        "evaluation": {"verification_profile": "conversation"},
        "max_windows": 84,
        "clock": {"start_at": START, "duration_minutes": 10080, "epoch_minutes": 120, **clock},
    }


def ai(*names, text="", ids=None):
    calls = [
        {
            "name": name,
            "args": {"n": index},
            "id": (ids or {}).get(index, f"c{index}"),
            "type": "tool_call",
        }
        for index, name in enumerate(names)
    ]
    return AIMessage(content=text, tool_calls=calls)


# Scenario contract --------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected"), sorted(FROZEN_HASHES.items()))
def test_saved_scenario_hashes_do_not_change(name, expected):
    spec = ScenarioSpec.model_validate_json((SCENARIOS / name).read_text())
    assert spec.clock is None
    assert spec.content_hash() == expected
    assert '"clock"' not in spec.canonical_json()


def test_clock_section_changes_the_hash_only_when_present():
    with_clock = ScenarioSpec.model_validate(scenario_data())
    data = scenario_data()
    data.pop("clock")
    without = ScenarioSpec.model_validate({**data, "max_windows": 8})
    assert '"clock"' in with_clock.canonical_json()
    assert '"clock"' not in without.canonical_json()
    assert with_clock.clock.limits.max_parallel_thinkers == 8
    # A frozen run can be reloaded from its saved JSON with the same hash.
    saved = with_clock.model_dump(mode="json")
    assert ScenarioSpec.model_validate(saved).content_hash() == with_clock.content_hash()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"max_windows": 83}, "max_windows"),
        ({"clock": {"epoch_minutes": 60}}, "the limit is 100"),
        ({"clock": {"daily_wake": "25:00"}}, "daily_wake"),
        ({"clock": {"start_at": "2026-11-09T06:00:00"}}, "offset"),
        ({"clock": {"timeline": [{"id": "a", "at": "Mon 05:00", "cue": "x"}]}}, "recipients"),
        (
            {
                "clock": {
                    "timeline": [{"id": "a", "at": "Mon 05:00", "recipients": ["dan"], "cue": "x"}]
                }
            },
            "outside the run",
        ),
        (
            {
                "clock": {
                    "timeline": [
                        {"id": "a", "at": "Mon 07:00", "recipients": ["ghost"], "cue": "x"}
                    ]
                }
            },
            "unknown owner",
        ),
        (
            {
                "clock": {
                    "timeline": [
                        {"id": "a", "at": "Mon 07:00", "recipients": ["dan"], "cue": "x"},
                        {"id": "a", "at": "Mon 08:00", "recipients": ["dan"], "cue": "y"},
                    ]
                }
            },
            "Duplicate timeline",
        ),
        (
            {
                "clock": {
                    "lifecycle": [
                        {
                            "kind": "assistant_subscription",
                            "owner_id": "dan",
                            "site_id": "s",
                            "bill_id": "b",
                            "due_at": "Mon 05:59",
                            "amount": 50,
                        }
                    ]
                }
            },
            "outside the run",
        ),
        ({"clock": {"online_allowance": {"ghost": 1}}}, "unknown owner"),
        (
            {
                "clock": {
                    "seed_board": [
                        {"id": "p", "author": "ghost", "title": "t", "body": "b", "days_ago": 2}
                    ]
                }
            },
            "unknown owner",
        ),
    ],
)
def test_clock_validation_refuses(change, message):
    data = scenario_data(**change.get("clock", {}))
    if "max_windows" in change:
        data["max_windows"] = change["max_windows"]
    if "epoch_minutes" in change.get("clock", {}):
        data["max_windows"] = 100
    with pytest.raises(ValidationError, match=message):
        ScenarioSpec.model_validate(data)


def test_week_scenario_in_the_repository_validates():
    clocked = []
    for path in SCENARIOS.glob("*.json"):
        spec = ScenarioSpec.model_validate_json(path.read_text())
        if spec.clock is not None:
            clocked.append(path.name)
            assert spec.max_windows == spec.clock.window_count()
    assert clocked and not set(clocked) & set(FROZEN_HASHES)


# Times -------------------------------------------------------------------------------


def test_time_arithmetic_is_plain():
    assert parse_at("Mon 06:30", START) == 30
    assert parse_at("Monday 06:00", START) == 0
    assert parse_at("sat 23:59", START) == 5 * 1440 + 18 * 60 - 1
    assert parse_at("Mon 05:00", START) == -60
    with pytest.raises(ValueError, match="weekday"):
        parse_at("noon", START)
    assert format_time(datetime(2026, 11, 10, 14, 5, tzinfo=UTC)) == "Tuesday 14:05"
    assert format_time("2026-11-10T14:05:30.000123+00:00") == "Tuesday 14:05"
    clock = config()
    assert clock.now_text(0) == "Monday 06:00"
    assert clock.today_text(0) == "Monday 9 November"
    assert next_daily_wake(0, clock) == 7200
    assert next_daily_wake(7200, clock) == 7200 + 86400
    assert clock.window_at(7199) == 0 and clock.window_at(7200) == 1


def test_time_keeps_the_offset_of_the_start():
    clock = config(start_at="2026-11-09T06:00:00+02:00")
    assert clock.now_text(3600) == "Monday 07:00"
    assert next_daily_wake(0, clock) == 7200
    assert format_time("2026-11-09T04:30:00+00:00", clock.tz) == "Monday 06:30"


def test_until_is_clamped_and_defaults_to_the_next_morning():
    clock = config()
    assert until_after_yield(0, None, clock) == 7200
    assert until_after_yield(100, 1, clock) == 100 + 300
    assert until_after_yield(100, 30, clock) == 100 + 1800
    assert until_after_yield(100, 999_999, clock) == 100 + 2880 * 60


def test_notice_delay_is_seeded():
    assert notice_delay_s(7, "dan:1") == notice_delay_s(7, "dan:1")
    samples = {notice_delay_s(7, f"dan:{index}") for index in range(50)}
    assert len(samples) > 10
    assert all(60 <= sample <= 600 for sample in samples)


# Prices ------------------------------------------------------------------------------


def test_price_of_a_thought_ignores_provider_usage():
    clock = config(think={"tokens_per_minute": 1500, "floor_seconds": 20, "cap_seconds": 180})
    assert price_think(AIMessage(content="hi"), clock) == 20
    assert price_think(AIMessage(content="x" * 4000), clock) == 40
    assert price_think(AIMessage(content="x" * 400_000), clock) == 180
    with_usage = AIMessage(
        content="x" * 4000,
        usage_metadata={"input_tokens": 9, "output_tokens": 9, "total_tokens": 18},
    )
    assert price_think(with_usage, clock) == 40
    # Tool calls count as visible output. A provider's call ID does not.
    assert price_think(ai("send_mail", ids={0: "a"}), clock) == price_think(
        ai("send_mail", ids={0: "zzzzzzz"}), clock
    )


def test_price_of_a_tool_is_frozen_by_name():
    clock = config(tool_seconds={"send_mail": 90, "inspect_world": 5}, default_tool_seconds=30)
    assert price_tool("send_mail", clock) == 90
    assert price_tool("check_bookings", clock) == 5  # The visible name of inspect_world.
    assert price_tool("anything_else", clock) == 30


def test_price_of_an_owner_turn_is_clamped():
    clock = config()
    assert price_actor(0, clock) == 45
    assert price_actor(10_000, clock) == 900
    assert price_actor(300, clock) == 45 + 60


# Stamps ------------------------------------------------------------------------------


def test_fold_stamps_each_call_at_the_start_of_its_duration():
    clock = config(tool_seconds={"slow": 100, "fast": 10})
    messages = [
        HumanMessage(content="old", id="old"),
        ai("slow", ids={0: "before"}),
        HumanMessage(content="now", id="activation"),
        ai("slow", "write_todos", "fast", "end_turn"),
        ToolMessage(content="{}", tool_call_id="c0"),
        ai("fast", ids={0: "c9"}),
    ]
    fold = fold_stamps(1000, messages, "activation", clock)
    first = price_think(messages[3], clock)
    assert [(c.name, c.stamp_s, c.gated) for c in fold.calls[:4]] == [
        ("slow", 1000 + first, True),
        ("write_todos", 1000 + first + 100, False),
        ("fast", 1000 + first + 100, True),
        ("end_turn", 1000 + first + 110, False),
    ]
    assert fold.bars[0] == 1000 + first + 110
    assert fold.end_s == fold.bars[1] == fold.bars[0] + price_think(messages[5], clock) + 10
    assert [call.call_id for call in fold.last_message_calls] == ["c9"]
    # An activation that has not started stays at its start.
    assert fold_stamps(50, messages, "missing", clock).end_s == 50
    # Only offered tools are gated. A model's invented tool name costs nothing.
    assert not fold_stamps(0, messages, "activation", clock, gated={"fast"}).calls[0].gated


def test_stamps_recompute_identically_from_saved_messages():
    """Property: for random histories, a restart computes the same stamps."""
    clock = config(tool_seconds={"a": 7, "b": 41})
    for seed in range(200):
        rng = random.Random(seed)
        messages = [HumanMessage(content="go", id="task")]
        for step in range(rng.randint(0, 6)):
            names = [rng.choice(["a", "b", "ls", "end_turn"]) for _ in range(rng.randint(0, 4))]
            message = AIMessage(
                content="x" * rng.randint(0, 20_000),
                tool_calls=[
                    {
                        "name": name,
                        "args": {"k": index},
                        "id": f"{step}-{index}",
                        "type": "tool_call",
                    }
                    for index, name in enumerate(names)
                ],
            )
            messages.append(message)
            messages.extend(
                ToolMessage(content="{}", tool_call_id=call["id"]) for call in message.tool_calls
            )
        reloaded = messages_from_dict(json.loads(json.dumps(messages_to_dict(messages))))
        again = fold_stamps(0, reloaded, "task", clock)
        assert again == fold_stamps(0, messages, "task", clock)
        gated = [call.stamp_s for call in again.calls if call.gated]
        assert gated == sorted(gated)
        assert list(again.bars) == sorted(again.bars)


# Order -------------------------------------------------------------------------------


def _rows(count, seed):
    rng = random.Random(seed)
    rows = []
    for index in range(count):
        key = f"event-{index}"
        rows.append(
            {
                "due_s": rng.randint(0, 5),
                "class": rng.randint(0, 10),
                "tie": tie(7, key),
                "key": key,
            }
        )
    return rows


def test_order_is_total_and_independent_of_insert_order():
    rows = _rows(300, 1)
    expected = [row["key"] for row in ordered(rows)]
    for seed in range(20):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert [row["key"] for row in ordered(shuffled)] == expected


def test_a_tie_depends_on_the_seed_and_not_on_arrival():
    first = sorted(["a", "b", "c", "d", "e", "f"], key=lambda key: tie(1, key))
    other = sorted(["a", "b", "c", "d", "e", "f"], key=lambda key: tie(2, key))
    assert first != other
    assert first == sorted(reversed(["a", "b", "c", "d", "e", "f"]), key=lambda key: tie(1, key))


# Wake rules --------------------------------------------------------------------------


def _event(kind, owner_id=None, cause=None, **payload):
    return {"kind": kind, "owner_id": owner_id, "cause_process": cause, "payload": payload}


def test_agent_wake_classes():
    assert classify_agent_event(_event("owner_message", "dan", source="owner"), "dan") == "direct"
    assert classify_agent_event(_event("owner_message", "dan", source="agent"), "dan") is None
    assert classify_agent_event(_event("inbox_received", "dan"), "dan") == "direct"
    assert classify_agent_event(_event("market_settled", "dan"), "dan") == "direct"
    assert classify_agent_event(_event("market_changed"), "dan") == "ambient"
    assert classify_agent_event(_event("resource_updated", "dan"), "dan") == "ambient"
    # An Agent's own effects never wake it, and another owner's events are not its own.
    assert classify_agent_event(_event("resource_updated", "dan", "agent:dan"), "dan") is None
    assert classify_agent_event(_event("inbox_received", "mei"), "dan") is None
    post = _event("board_posted", author_id="A1", audience=None)
    assert classify_agent_event(post, "dan", "A1") is None
    assert classify_agent_event(post, "dan", "A2") == "ambient"
    direct = _event("board_posted", author_id="A1", audience=["A2"])
    assert classify_agent_event(direct, "dan", "A2") == "direct"
    assert classify_agent_event(direct, "mei", "A3") is None
    assert classify_agent_event(_event("window_reached"), "dan") is None


def test_owner_wake_classes():
    assert classify_actor_event(_event("actor_cue", "dan"), "dan") == "cue"
    assert classify_actor_event(_event("owner_message", "dan", source="agent"), "dan") == "message"
    assert classify_actor_event(_event("owner_message", "dan", source="owner"), "dan") is None
    assert classify_actor_event(_event("inbox_received", "dan"), "dan") == "message"
    assert classify_actor_event(_event("actor_effect_rejected", "dan"), "dan") == "message"
    assert classify_actor_event(_event("inbox_received", "dan", "actor:dan"), "dan") is None
    # R13: nothing about the board reaches a person, and a record change wakes nobody.
    assert classify_actor_event(_event("board_posted"), "dan") is None
    assert classify_actor_event(_event("resource_updated", "dan"), "dan") is None
    assert classify_actor_event(_event("market_settled"), "dan") is None


# Legacy bytes ------------------------------------------------------------------------


def test_new_fields_leave_window_run_bytes_unchanged():
    run, participant, activation, message = uuid4(), uuid4(), uuid4(), uuid4()
    envelope = ActivationEnvelope(
        run_id=run,
        participant_id=participant,
        activation_id=activation,
        window_id=2,
        owner_messages=(OwnerMessage(message_id=message, content="Hello"),),
    )
    dumped = envelope.model_dump(mode="json")
    assert set(dumped) == {
        "run_id",
        "participant_id",
        "activation_id",
        "window_id",
        "source_event_ids",
        "owner_messages",
        "world_observations",
    }
    assert ActivationEnvelope.model_validate(dumped).prompt() == envelope.prompt()
    assert "now_text" not in envelope.prompt()
    assert envelope.prompt("natural") == "Your owner says:\n\nHello"
    assert render_activation((), ()) == (
        "Nothing new since you last checked. Pick up anything still open, or end your turn."
    )
    output = ActorOutput(kind="wait", source_event_id="e")
    assert not {"next_action", "director_requests", "online_spend_approval"} & set(
        output.model_dump()
    )
    for hidden in ("next_action", "director_requests", "online_spend_approval"):
        assert hidden not in ActorCandidate.model_fields
        assert hidden not in str(ActorCandidate.model_json_schema())
    assert "wake_in_minutes" not in EndTurn.model_fields
    assert "wake_in_minutes" not in ActivationResult.model_fields
    assert set(present_result("x", {"ok": True, "data": {"created_at": "z", "a": 1}})["data"]) == {
        "a"
    }
    spec = ScenarioSpec.model_validate(scenario_data())
    assert "clock" not in build_actor_view(spec, "dan", "Hi").model_dump(mode="json")


def test_old_saved_records_still_validate():
    ActorOutput.model_validate({"kind": "reply", "text": "Hi", "source_event_id": "e"})
    ClockedActivationResult.model_validate({"status": "finished", "text": ""})
    assert ClockedActivationResult(status="responded", wake_in_minutes=9).wake_in_minutes == 9


def test_clocked_prompt_parts_use_plain_words():
    envelope = ActivationEnvelope(
        run_id=uuid4(),
        participant_id=uuid4(),
        activation_id=uuid4(),
        window_id=3,
        now_text="Tuesday 14:05",
        wake_reason="You asked to look at this again around now.",
    )
    prompt = envelope.prompt("natural")
    assert prompt.startswith("It's Tuesday 14:05. You asked")
    assert "Nothing new" in prompt
    assert "window" not in prompt
    assert (
        ActivationEnvelope.model_validate(envelope.model_dump(mode="json")).prompt("natural")
        == prompt
    )
    texts = [
        clock_instructions(120),
        CLOCKED_END_TURN_DESCRIPTION,
        json.dumps(ClockedEndTurn.model_json_schema()),
        CLOCK_DAY,
        json.dumps(ClockedActorCandidate.model_json_schema()["properties"]["next_action"]),
        json.dumps(ClockedActorCandidate.model_json_schema()["properties"]["try_to_make_happen"]),
        json.dumps(
            ClockedActorCandidate.model_json_schema()["properties"]["online_spend_approval"]
        ),
    ]
    for text in texts:
        lowered = text.lower()
        assert not [word for word in FORBIDDEN if word in lowered], text
    assert "every 2 hours" in clock_instructions(120)
    assert "every 45 minutes" in clock_instructions(45)


def test_friendly_time_on_clocked_results():
    result = present_result(
        "list_mail",
        {
            "ok": True,
            "data": {
                "items": [{"id": "m", "created_at": "2026-11-10T14:05:00.5+00:00", "window": 3}]
            },
        },
        time_text=format_time,
    )
    assert result["data"]["items"] == [{"id": "m", "time": "Tuesday 14:05"}]


def test_clocked_actor_candidate_maps_back_to_the_saved_field():
    assert "try_to_make_happen" in ClockedActorCandidate.model_fields
    assert "director_requests" not in ClockedActorCandidate.model_fields
    with pytest.raises(ValidationError):
        ActorOutput(
            kind="wait",
            source_event_id="e",
            online_spend_approval=5,
        )
    saved = ActorOutput(
        kind="wait",
        source_event_id="e",
        next_action={
            "description": "Nap",
            "minutes": 30,
            "reachable": False,
            "category": " Sleep ",
        },
        director_requests=("Call the landlord",),
    )
    assert saved.next_action.category == "sleep"
    assert ActorOutput.model_validate(saved.model_dump(mode="json")) == saved
    with pytest.raises(ValidationError):
        ActorOutput(
            kind="wait",
            source_event_id="e",
            next_action={"description": "Nap", "minutes": 4321, "reachable": True, "category": "x"},
        )
    assert ModelActor  # The natural wait rule is covered by the engine tests.


# The gate ----------------------------------------------------------------------------


def _gate(**values):
    clock = config(
        think={"tokens_per_minute": 1500, "floor_seconds": 20, "cap_seconds": 180},
        default_tool_seconds=30,
        **values,
    )
    gate = ClockGate(clock)
    gate.register_tools("agent:a", ["read", "write", "post"])
    gate.register_tools("agent:b", ["read", "write", "post"])
    return gate


HISTORY = [HumanMessage(content="go", id="task")]


async def test_thinking_process_blocks_at_its_bound_and_a_tie_waits():
    gate = _gate()
    gate.begin_activation("agent:a", "task", 1, 100)
    assert gate.bound("agent:a") == 120
    assert gate.allows(119)
    assert not gate.allows(120)
    gate.begin_actor("actor:a", 100)
    assert gate.bound("actor:a") == 130
    gate.end("actor:a")
    gate.end("agent:a")
    assert gate.allows(10**9)


async def test_after_model_registers_calls_and_only_free_calls_do_not_park():
    gate = _gate()
    gate.begin_activation("agent:a", "task", 1, 100)
    message = ai("read", "write_todos", "write")
    gate.after_model("agent:a", HISTORY, message)
    think = price_think(message, gate.config)
    head = gate.head()
    assert head.name == "read" and head.stamp_s == 100 + think and not head.arrived
    assert gate.bound("agent:a") is None
    # A later item may pass only a process that can no longer precede it by its calls.
    assert gate.allows(10**6)
    await gate.before_tool("agent:a", message.tool_calls[1], [*HISTORY, message])  # A free tool.
    parked = asyncio.create_task(
        gate.before_tool("agent:a", message.tool_calls[0], [*HISTORY, message])
    )
    await asyncio.sleep(0)
    assert head.arrived and not parked.done()
    finished = head.finished
    gate.release(head)
    await parked
    assert head.running
    gate.after_tool("agent:a", message.tool_calls[0])
    assert finished.is_set() and head.completed
    assert gate.head().name == "write"
    assert gate.head().stamp_s == 100 + think + 30
    second = gate.head()
    parked = asyncio.create_task(
        gate.before_tool("agent:a", message.tool_calls[2], [*HISTORY, message])
    )
    await asyncio.sleep(0)
    gate.release(second)
    await parked
    gate.after_tool("agent:a", message.tool_calls[2])
    # Nothing is left to run, so the process blocks at its bar until it reports.
    assert gate.head() is None
    assert gate.bound("agent:a") == 100 + think + 60


async def test_registered_call_that_never_arrives_is_cleared():
    gate = _gate()
    gate.begin_activation("agent:a", "task", 1, 100)
    message = ai("read", "write")
    gate.after_model("agent:a", HISTORY, message)
    # The next model call means that the batch has joined.
    gate.before_model("agent:a", [*HISTORY, message])
    assert gate.head() is None
    assert gate.bound("agent:a") == 100 + price_think(message, gate.config) + 60 + 20
    # The end of the activation clears them too.
    gate.after_model("agent:a", [*HISTORY, message], ai("read", ids={0: "later"}))
    assert gate.head() is not None
    gate.end("agent:a")
    assert gate.head() is None
    assert gate.allows(10**9)
    # The conductor's bound clears one when nothing else does.
    gate.begin_activation("agent:a", "task", 1, 100)
    gate.after_model("agent:a", HISTORY, ai("read"))
    entry = gate.head()
    gate.drop(entry)
    assert gate.head() is None and gate.bound("agent:a") is not None


async def test_failed_call_keeps_its_slot_for_the_retry():
    gate = _gate()
    gate.begin_activation("agent:a", "task", 1, 100)
    message = ai("read")
    gate.after_model("agent:a", HISTORY, message)
    entry = gate.head()
    parked = asyncio.create_task(
        gate.before_tool("agent:a", message.tool_calls[0], [*HISTORY, message])
    )
    await asyncio.sleep(0)
    finished = entry.finished
    gate.release(entry)
    await parked
    gate.tool_failed("agent:a", message.tool_calls[0])
    assert finished.is_set()
    assert gate.head() is entry and not entry.arrived
    gate.attempt_failed("agent:a")
    assert gate.head() is entry and gate.bound("agent:a") is None
    parked = asyncio.create_task(
        gate.before_tool("agent:a", message.tool_calls[0], [*HISTORY, message])
    )
    await asyncio.sleep(0)
    assert entry.arrived and not parked.done()
    gate.release(entry)
    await parked
    gate.after_tool("agent:a", message.tool_calls[0])
    assert gate.head() is None


async def test_cancel_while_parked_keeps_the_slot_and_leaves_a_clean_state():
    gate = _gate()
    gate.begin_activation("agent:a", "task", 1, 100)
    message = ai("read")
    gate.after_model("agent:a", HISTORY, message)
    parked = asyncio.create_task(
        gate.before_tool("agent:a", message.tool_calls[0], [*HISTORY, message])
    )
    await asyncio.sleep(0)
    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked
    assert gate.head() is not None and not gate.head().arrived


async def test_first_arrival_registers_a_resumed_batch_and_skips_saved_calls():
    async def saved(process, activation):
        assert (process, activation) == ("agent:a", "task")
        return {"done"}

    gate = _gate()
    gate.saved_calls = saved
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "read", "args": {}, "id": "done", "type": "tool_call"},
            {"name": "write", "args": {}, "id": "open", "type": "tool_call"},
            {"name": "post", "args": {}, "id": "answered", "type": "tool_call"},
        ],
    )
    history = [*HISTORY, message, ToolMessage(content="{}", tool_call_id="answered")]
    fold = fold_stamps(100, history, "task", gate.config, gated={"read", "write", "post"})
    gate.begin_activation("agent:a", "task", 1, 100, resume_bar=100 + fold.calls[1].stamp_s - 100)
    # The recovery barrier: it blocks from its saved bar and not from bar plus think time.
    assert gate.bound("agent:a") == fold.calls[1].stamp_s
    assert not gate.allows(fold.calls[1].stamp_s)
    # A call with a saved result returns at once and does not run again.
    await gate.before_tool("agent:a", message.tool_calls[0], history)
    entry = gate.head()
    assert entry.call_id == "open" and entry.stamp_s == fold.calls[1].stamp_s
    assert gate.bound("agent:a") is None
    parked = asyncio.create_task(gate.before_tool("agent:a", message.tool_calls[1], history))
    await asyncio.sleep(0)
    assert entry.arrived and not parked.done()
    parked.cancel()
    await asyncio.gather(parked, return_exceptions=True)


async def test_think_slots_bound_model_calls():
    gate = _gate(limits={"max_parallel_thinkers": 2})

    async def think():
        async with gate.think_slot():
            await asyncio.sleep(0.005)

    await asyncio.gather(*(think() for _ in range(9)))
    assert gate.peak_calls_in_flight == 2 and gate.calls_in_flight == 0


async def test_gate_passes_calls_of_a_session_outside_the_conductor():
    gate = _gate()
    await gate.before_tool("agent:a", {"name": "read", "id": "x"}, HISTORY)
    gate.after_tool("agent:a", {"name": "read", "id": "x"})
    gate.after_model("agent:a", HISTORY, ai("read"))
    gate.before_model("agent:a", HISTORY)
    assert gate.head() is None


def test_simulated_conductor_never_goes_backwards_and_ignores_latency():
    """Property: with random thoughts and random real-time order, releases keep story order."""

    def simulate(world_seed, latency_seed):
        async def scenario_run():
            gate = _gate()
            released = []
            world = random.Random(world_seed)
            order = random.Random(latency_seed)
            plans = {
                name: [
                    [world.choice(["read", "write", "post"]) for _ in range(world.randint(0, 3))]
                    for _ in range(world.randint(1, 4))
                ]
                for name in ("agent:a", "agent:b")
            }
            sizes = {name: [world.randint(0, 9000) for _ in plan] for name, plan in plans.items()}
            start = {"agent:a": world.randint(0, 50), "agent:b": world.randint(0, 50)}

            async def process(name):
                gate.begin_activation(name, "task", 1, start[name])
                history = [HumanMessage(content="go", id="task")]
                for step, batch in enumerate(plans[name]):
                    gate.before_model(name, history)
                    for _ in range(order.randint(0, 3)):
                        await asyncio.sleep(0)
                    message = AIMessage(
                        content="x" * sizes[name][step],
                        tool_calls=[
                            {"name": tool, "args": {}, "id": f"{step}.{i}", "type": "tool_call"}
                            for i, tool in enumerate(batch)
                        ],
                    )
                    gate.after_model(name, history, message)
                    history = [*history, message]

                    async def one(call, seen):
                        await gate.before_tool(name, call, seen)
                        gate.after_tool(name, call)

                    await asyncio.gather(*(one(call, history) for call in message.tool_calls))
                    history = [
                        *history,
                        *(
                            ToolMessage(content="{}", tool_call_id=c["id"])
                            for c in message.tool_calls
                        ),
                    ]
                gate.end(name)

            tasks = [asyncio.create_task(process(name)) for name in plans]
            while any(not task.done() for task in tasks):
                head = gate.head()
                if head is None or not head.arrived or not gate.allows(head.stamp_s):
                    await asyncio.sleep(0)
                    continue
                released.append((head.stamp_s, head.process, head.call_id))
                done = head.finished
                gate.release(head)
                await done.wait()
            return released

        return asyncio.run(scenario_run())

    for world_seed in range(30):
        first = simulate(world_seed, 0)
        assert [item[0] for item in first] == sorted(item[0] for item in first)
        for latency_seed in (1, 2, 3):
            assert simulate(world_seed, latency_seed) == first


# Queue and triggers on real PostgreSQL -------------------------------------------------


def clocked_spec(**clock):
    data = scenario_data(**clock)
    data["max_windows"] = 84
    return ScenarioSpec.model_validate(data)


async def _state(pool, run_id):
    async with pool.connection() as conn:
        return await (
            await conn.execute("SELECT * FROM clock_state WHERE run_id=%s", (run_id,))
        ).fetchone()


async def test_queue_pops_in_rule_order_whatever_the_insert_order(pool):
    director = Director(pool)
    run_id = UUID((await director.create_run(clocked_spec()))["run_id"])
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM clock_events WHERE run_id=%s", (run_id,))
        rows = _rows(120, 3)
        shuffled = list(rows)
        random.Random(9).shuffle(shuffled)
        for item in shuffled:
            await schedule(
                conn, run_id, item["due_s"], "beat", {}, item["key"], None, "test", item["class"]
            )
        seed = await (
            await conn.execute("SELECT seed FROM environment_runs WHERE id=%s", (run_id,))
        ).fetchone()
        expected = ordered([{**item, "tie": tie(seed["seed"], item["key"])} for item in rows])
        popped = []
        while (event := await next_event(conn, run_id)) is not None:
            popped.append(event["key"])
            await conn.execute("UPDATE clock_events SET status='done' WHERE id=%s", (event["id"],))
        await conn.commit()
    assert popped == [item["key"] for item in expected]


async def test_queue_helpers_are_idempotent_and_never_move_behind_now(pool):
    director = Director(pool)
    run_id = UUID((await director.create_run(clocked_spec()))["run_id"])
    async with pool.connection() as conn:
        await conn.execute("UPDATE clock_state SET sim_s=500 WHERE run_id=%s", (run_id,))
        await schedule(conn, run_id, 100, "agent_wake", {"a": 1}, "k1", "dan")
        await schedule(conn, run_id, 900, "agent_wake", {"a": 2}, "k1", "dan")
        # The two call forms that other modules use.
        await schedule(
            conn, run_id, 700, "director_plan", "k2", payload={"plan": {}}, source="director"
        )
        await schedule(
            conn, run_id, due_s=800, kind="plan", key="k3", owner_id="dan", payload={"x": 1}
        )
        cursor = await conn.execute(
            "SELECT key,due_s,payload,class,source FROM clock_events WHERE run_id=%s "
            "AND key IN ('k1','k2','k3') ORDER BY key",
            (run_id,),
        )
        rows = await cursor.fetchall()
        assert rows[0]["due_s"] == 500 and rows[0]["payload"] == {"a": 1}
        assert rows[0]["class"] == 8
        assert rows[1]["due_s"] == 700 and rows[1]["class"] == 0 and rows[1]["source"] == "director"
        assert rows[2]["class"] == 0
        assert await retime(conn, run_id, "k2", 200)
        cursor = await conn.execute(
            "SELECT due_s FROM clock_events WHERE run_id=%s AND key='k2'", (run_id,)
        )
        assert (await cursor.fetchone())["due_s"] == 500
        assert await cancel(conn, run_id, "k2")
        assert not await cancel(conn, run_id, "k2")
        with pytest.raises(ValueError, match="another source"):
            await cancel_event(conn, run_id, "k3", only_source="director")
        with pytest.raises(ValueError, match="queued"):
            await cancel_event(conn, run_id, "k2")
        with pytest.raises(ValueError, match="Unknown"):
            await schedule(conn, run_id, 1, "nonsense", {}, "k9")
        await conn.commit()


async def test_create_run_seeds_the_known_future(pool):
    spec = clocked_spec(
        timeline=[{"id": "tue", "at": "Tue 09:00", "recipients": ["dan"], "cue": "Rain."}],
        lifecycle=[
            {
                "kind": "assistant_subscription",
                "owner_id": "dan",
                "site_id": "care",
                "bill_id": "b",
                "due_at": "Sat 23:59",
                "amount": 5,
            }
        ],
        director={"enabled": True, "review_every_minutes": 1440},
    )
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    async with pool.connection() as conn:
        events = await (
            await conn.execute(
                "SELECT kind,key,due_s,class FROM clock_events WHERE run_id=%s", (run_id,)
            )
        ).fetchall()
        processes = await (
            await conn.execute(
                "SELECT owner_id,kind,state FROM clock_processes WHERE run_id=%s ORDER BY 1,2",
                (run_id,),
            )
        ).fetchall()
        run = await (
            await conn.execute("SELECT phase FROM environment_runs WHERE id=%s", (run_id,))
        ).fetchone()
    by_key = {event["key"]: event for event in events}
    assert run["phase"] == "agents"
    assert by_key["boundary:0"]["due_s"] == 7200 and by_key["boundary:0"]["class"] == 4
    assert by_key["beat:tue"]["due_s"] == (1440 + 180) * 60
    assert by_key["run_end"]["due_s"] == 604800 and by_key["run_end"]["class"] == 10
    assert by_key["lifecycle:dan:care:b"]["class"] == 5
    assert sum(event["kind"] == "director_review" for event in events) == 6
    assert [(p["owner_id"], p["kind"], p["state"]) for p in processes] == [
        ("dan", "actor", "idle"),
        ("dan", "agent", "idle"),
        ("mei", "actor", "idle"),
        ("mei", "agent", "idle"),
    ]


async def test_stamp_trigger_uses_story_time_and_keeps_insert_order(pool):
    director = Director(pool)
    run_id = UUID((await director.create_run(clocked_spec()))["run_id"])
    await director.control(run_id, "start")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE clock_state SET sim_s=3600,turn_process='agent:dan' WHERE run_id=%s", (run_id,)
        )
        await conn.commit()
    first = await director.send_message(run_id, "dan", "One", uuid4())
    second = await director.send_message(run_id, "dan", "Two", uuid4())
    moment = datetime.fromisoformat(START) + timedelta(hours=1)
    assert moment <= first["created_at"] < second["created_at"] < moment + timedelta(seconds=1)
    async with pool.connection() as conn:
        events = await (
            await conn.execute(
                "SELECT cause_process,created_at FROM run_events WHERE run_id=%s "
                "AND kind='owner_message' ORDER BY created_at",
                (run_id,),
            )
        ).fetchall()
    assert [event["cause_process"] for event in events] == ["agent:dan", "agent:dan"]
    assert first["created_at"] < events[0]["created_at"] < second["created_at"]


async def test_stamp_trigger_is_a_no_op_for_window_runs_and_plain_boards(pool, provision, client):
    data = scenario_data()
    data.pop("clock")
    data["max_windows"] = 8
    director = Director(pool)
    created = await director.create_run(ScenarioSpec.model_validate(data))
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    before = datetime.now(UTC) - timedelta(minutes=5)
    message = await director.send_message(run_id, "dan", "Hello", uuid4())
    assert message["created_at"] > before
    async with pool.connection() as conn:
        event = await (
            await conn.execute(
                "SELECT cause_process FROM run_events WHERE run_id=%s AND kind='owner_message'",
                (run_id,),
            )
        ).fetchone()
    assert event["cause_process"] is None
    cluster, agents = await provision(count=1)
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {agents[0]['token']}"},
    ) as board:
        posted = await board.post(
            f"/api/v1/clusters/{cluster['id']}/threads",
            json={"title": "Plain", "body": "Board", "request_id": str(uuid4())},
        )
    assert posted.status_code == 201
    assert datetime.fromisoformat(posted.json()["created_at"]) > before


async def test_seed_board_keeps_past_times_and_counts_as_read(app, pool):
    spec = clocked_spec(
        seed_board=[
            {
                "id": "old",
                "author": "dan",
                "title": "Fence panel",
                "body": "Anyone got a spare?",
                "days_ago": 12,
                "replies": [{"author": "mei", "body": "Maybe."}],
            },
            {
                "id": "private",
                "author": "mei",
                "title": "About Dan",
                "body": "Between us.",
                "days_ago": 3,
                "audience": ["dan"],
            },
        ]
    )
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    dan = next(p for p in created["participants"] if p["owner_id"] == "dan")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": f"Bearer {dan['token']}"},
    ) as board:
        updates = await board.get(f"clusters/{created['cluster_id']}/updates")
        threads = await board.get(f"clusters/{created['cluster_id']}/threads")
    assert updates.status_code == 200 and updates.json()["items"] == []
    created_at = {
        item["title"]: datetime.fromisoformat(item["created_at"])
        for item in threads.json()["items"]
    }
    start = datetime.fromisoformat(START)
    assert created_at["Fence panel"] < start - timedelta(days=12)
    assert start - timedelta(days=4) < created_at["About Dan"] < start
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT count(*) AS n FROM run_events WHERE run_id=%s AND kind='board_posted'",
                (run_id,),
            )
        ).fetchone()
        reply = await (
            await conn.execute(
                "SELECT m.created_at FROM messages m WHERE m.cluster_id=%s "
                "AND m.parent_id IS NOT NULL",
                (created["cluster_id"],),
            )
        ).fetchone()
    assert rows["n"] == 0
    assert created_at["Fence panel"] < reply["created_at"] < start
