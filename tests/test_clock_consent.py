"""Clocked owners' booking yes, a yes in words only, and deeds of other people.

The five-lives runs of 8 to 10 November found that no clocked owner could approve a booking at
all, that owners often said yes without the field that frees up money, and that one owner's model
wrote another person's arrival. Real PostgreSQL for the engine test, scripted models only.
"""

import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from agentrpg.actor import (
    CLOCK_BOOKING,
    CLOCK_DAY,
    ActorContractError,
    ActorOutput,
    ModelActor,
    _candidate_schema,
    _speaks_for_other,
    _unrecorded_yes,
    build_actor_view,
    validate_actor_output,
)
from agentrpg.director import Director
from agentrpg.director_brain import COMMON_PROMPT
from agentrpg.scenario_schema import ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "try_clock_control", ROOT / "scripts" / "try_clock_control.py"
)
control = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("try_clock_control", control)
SPEC.loader.exec_module(control)

NEXT = {"description": "Shift", "minutes": 60, "reachable": False, "category": "work"}
CLOCK = {"now": "Tuesday 09:00", "today": "Tuesday 10 November", "current_action": None}


def two_assets(owners=("dan", "mei")):
    data = control.scenario(owners, duration_minutes=240).model_dump(mode="json")
    data["world"]["assets"].append({"id": "van", "name": "Shared van", "price": 7})
    return ScenarioSpec.model_validate(data)


def view(*conversation, clock=CLOCK, observations=(), spec=None):
    return build_actor_view(
        spec or two_assets(),
        "dan",
        "Your assistant wrote to you.",
        conversation=[
            {"role": role, "text": text, "id": f"m{index}"}
            for index, (role, text) in enumerate(conversation)
        ],
        observations=observations,
        source_event_id="e1",
        clock=clock,
    )


def said(text, **fields):
    return ActorOutput(kind="reply", text=text, source_event_id="e1", next_action=NEXT, **fields)


def actor_model(*answers):
    return GenericFakeChatModel(messages=iter([AIMessage(json.dumps(a)) for a in answers]))


# The booking yes. ------------------------------------------------------------------------------


def test_only_a_clocked_day_with_things_to_book_shows_the_booking_field():
    schema = _candidate_schema(view()).model_json_schema()
    assert "booking_approval" in schema["properties"]
    booking = json.dumps(schema["$defs"]["BookingApproval"])
    assert '"car"' in booking and '"van"' in booking
    none = view().model_copy(update={"assets": ()})
    assert "booking_approval" not in _candidate_schema(none).model_fields
    window = view().model_copy(update={"clock": None})
    assert "booking_approval" not in _candidate_schema(window).model_fields


def test_a_booking_yes_is_checked_and_a_saved_turn_keeps_its_bytes():
    grant = said("Yes, book the van.", booking_approval={"booking": "van", "up_to": 7})
    validate_actor_output(view(("assistant", "Shall I book the van for $7?")), grant)
    with pytest.raises(ActorContractError, match="not in assets"):
        validate_actor_output(
            view(), said("Book the boat.", booking_approval={"booking": "boat", "up_to": 7})
        )
    with pytest.raises(ValidationError, match="most they will pay"):
        said("Book it.", booking_approval={"booking": "van", "up_to": 0})
    with pytest.raises(ValidationError, match="approve a booking"):
        ActorOutput(
            kind="wait", source_event_id="e1", booking_approval={"booking": "van", "up_to": 7}
        )
    cancel = said("Cancel the car.", booking_approval={"booking": "car", "action": "cancel"})
    validate_actor_output(view(), cancel)
    # An unset field stays out of the saved turn, so older saved turns load byte for byte.
    assert "booking_approval" not in said("Thanks.").model_dump(mode="json")
    assert ActorOutput.model_validate(grant.model_dump(mode="json")) == grant


def test_owners_are_told_when_a_booking_yes_applies():
    for words in ("one moment only", "A yes in words alone books", "never fill it twice"):
        assert words in CLOCK_BOOKING
    field = _candidate_schema(view()).model_fields["booking_approval"].description
    assert "status replies" in field and "already allowed" in field


async def test_the_booking_rules_reach_only_owners_with_things_to_book():
    answer = {"kind": "wait", "next_action": NEXT}
    model = control.ScriptModel(script=[AIMessage(content=json.dumps(answer))])
    await ModelActor(model, natural=True, max_repairs=0).respond(view())
    assert "booking_approval is for one moment only" in model.seen[0].content
    model = control.ScriptModel(script=[AIMessage(content=json.dumps(answer))])
    plain = view().model_copy(update={"assets": ()})
    await ModelActor(model, natural=True, max_repairs=0).respond(plain)
    assert "booking_approval" not in model.seen[0].content


async def test_a_booking_yes_adds_to_what_the_owner_allowed_and_a_booking_settles(
    app, pool, test_database_url
):
    spec = two_assets(("dan",))
    actors = {
        "dan": control.ScriptedOwner(
            [
                {
                    "kind": "reply",
                    "text": "Yes, book the car for this afternoon.",
                    "next_action": NEXT,
                    "booking_approval": {"booking": "car", "up_to": 5},
                }
            ]
        )
    }
    async with control.clocked_run(
        app, test_database_url, spec, {"dan": control.ScriptModel()}, actors
    ) as (engine, created, _runtime, _agents):
        run_id = UUID(created["run_id"])
        director = Director(pool)
        await director.control(run_id, "start")
        # The van was allowed earlier. The car yes must keep it.
        await engine.world.set_consent(run_id, "dan", 7, ["van"], uuid4())
        await engine.advance(created["run_id"], max_sim_minutes=20)
        async with pool.connection() as conn:
            account = await (
                await conn.execute(
                    "SELECT consent_budget,consent_assets,consent_kinds FROM world_accounts "
                    "WHERE run_id=%s AND owner_id='dan'",
                    (run_id,),
                )
            ).fetchone()
            window = (
                await (
                    await conn.execute(
                        "SELECT current_window FROM environment_runs WHERE id=%s", (run_id,)
                    )
                ).fetchone()
            )["current_window"]
            changes = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM run_events WHERE run_id=%s "
                    "AND kind='actor_effect_rejected'",
                    (run_id,),
                )
            ).fetchone()
        assert account["consent_budget"] == 12
        assert sorted(account["consent_assets"]) == ["car", "van"]
        assert account["consent_kinds"] == ["reserve"]
        assert changes["n"] == 0
        await engine.world.submit(run_id, "dan", window, uuid4(), "reserve", "car")
        await engine.advance(created["run_id"], max_sim_minutes=90)
        async with pool.connection() as conn:
            held = await (
                await conn.execute(
                    "SELECT asset_id FROM world_reservations WHERE run_id=%s AND owner_id='dan'",
                    (run_id,),
                )
            ).fetchall()
    assert [item["asset_id"] for item in held] == ["car"]


# A yes in words only. --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asked,text,found",
    [
        ("Shall I book the hall for $40?", "yes go ahead", "$40"),
        ("The $64 order was declined. Ok to pay it?", "Ok, do it.", "$64"),
        ("Shall I book the hall for $40?", "no, leave it", None),
        ("I booked the hall for $40.", "yes great", None),
        ("Do you want the car on Saturday?", "yes please", None),
    ],
)
def test_a_yes_in_words_only_is_found(asked, text, found):
    assert _unrecorded_yes(view(("assistant", asked)), said(text)) == found


def test_a_yes_with_its_field_or_after_the_owner_spoke_is_not_asked_about():
    asked = view(("assistant", "Shall I spend $40 on the hall?"))
    assert _unrecorded_yes(asked, said("yes", online_spend_approval=40)) is None
    booked = said("yes", booking_approval={"booking": "car", "up_to": 5})
    assert _unrecorded_yes(asked, booked) is None
    later = view(("assistant", "Shall I spend $40?"), ("owner", "hmm"))
    assert _unrecorded_yes(later, said("yes")) is None
    assert _unrecorded_yes(asked.model_copy(update={"clock": None}), said("yes")) is None


async def test_a_yes_in_words_is_asked_about_once_and_the_next_answer_stands():
    asked = view(("assistant", "Shall I book the van for $7?"))
    words = {"kind": "reply", "text": "yes go ahead", "next_action": NEXT}
    typed = {**words, "booking_approval": {"booking": "van", "up_to": 7}}
    actor = ModelActor(actor_model(words, typed), max_repairs=2, natural=True)
    output = await actor.respond(asked)
    assert output.booking_approval.booking == "van"
    assert "fill booking_approval instead" in actor.last_attempts[0]["reason"]
    # Asked once: the same words again are their answer.
    actor = ModelActor(actor_model(words, words), max_repairs=2, natural=True)
    output = await actor.respond(asked)
    assert output.text == "yes go ahead" and output.booking_approval is None
    assert len(actor.last_attempts) == 2


# Other people's deeds. -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,found",
    [
        ("ok so mei just walked in. i rang her and she came down", "mei"),
        ("Mei said yes to the tea!", "mei"),
        ("I hope mei turns up at four.", None),
        ("If mei arrived early she'd see everything", None),
        ("Can you check whether mei said yes?", None),
        ("dan just walked in", None),
        ("Mei left a note on the door.", None),
    ],
)
def test_a_deed_of_someone_else_stated_as_done_is_found(text, found):
    assert _speaks_for_other(view(), said(text)) == found


def test_a_deed_that_reached_this_person_is_theirs_to_repeat():
    told = view(("assistant", "Good news: Mei said yes to the tea."))
    assert _speaks_for_other(told, said("Mei said yes, lovely")) is None
    seen = view(observations=[{"id": "o1", "kind": "news", "text": "Mei just walked in."}])
    assert _speaks_for_other(seen, said("mei just walked in!")) is None


async def test_a_deed_of_someone_else_is_asked_about_once():
    first = {"kind": "reply", "text": "mei just walked in, it worked", "next_action": NEXT}
    second = {
        "kind": "reply",
        "text": "Ringing Mei to ask her down now.",
        "next_action": NEXT,
        "try_to_make_happen": ["Ring Mei and ask her to come down"],
    }
    actor = ModelActor(actor_model(first, second), max_repairs=1, natural=True)
    output = await actor.respond(view())
    assert output.director_requests == ("Ring Mei and ask her to come down",)
    assert "Mei decides for themselves" in actor.last_attempts[0]["reason"]
    actor = ModelActor(actor_model(first), max_repairs=0, natural=True)
    assert (await actor.respond(view())).text == first["text"]


def test_owners_and_the_director_are_told_others_decide_for_themselves():
    assert "decide for themselves" in CLOCK_DAY
    assert "try_to_make_happen" in CLOCK_DAY
    assert "decide for themselves" in COMMON_PROMPT
    assert "doing_now" in COMMON_PROMPT
