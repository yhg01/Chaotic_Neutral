"""What people and assistants read on a clocked run: real PostgreSQL, scripted models only."""

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from agentrpg import sim_web
from agentrpg.actor import (
    _CLOCKED_BACKSTAGE,
    CLOCK_DAY,
    ActorContractError,
    ActorOutput,
    ClockedActorCandidate,
    ModelActor,
    _clocked_prompt_view,
    _has_assistant_role_reversal,
    _repeats_owner,
    build_actor_view,
    earlier_decisions,
    validate_actor_output,
)
from agentrpg.agent_surface import (
    _notice,
    internal_tool_name,
    present_result,
    tool_description,
    tool_name,
)
from agentrpg.clock import ClockConfig, format_moment
from agentrpg.director import Director, save_message
from agentrpg.director_brain import DirectorBrain, DirectorPlan, ScriptedDirectorBrain
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxService
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario
from agentrpg.transfers import _notice as transfer_notice
from agentrpg.web_tools import WebTools

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "try_clock_control", ROOT / "scripts" / "try_clock_control.py"
)
control = importlib.util.module_from_spec(SPEC)
sys.modules["try_clock_control"] = control
SPEC.loader.exec_module(control)

calls = control.calls
end_turn = control.end_turn
ScriptModel = control.ScriptModel
ScriptedOwner = control.ScriptedOwner
clocked_run = control.clocked_run
scenario = control.scenario

CLOCK = {"now": "Tuesday 09:00", "today": "Tuesday 10 November", "current_action": None}
NOW = {"description": "Home", "minutes_left": 60, "you_would_notice_messages": True}
# The test words of the brief. "turn" alone is an ordinary word ("end your turn").
BANNED = re.compile(
    r"\b(windows?|\w+_window|window_\w+|ticks?|ticked|runs?|running|run_id|turn[- ]counts?|beats?"
    r"|cues?|directors?|actors?|scenarios?|simulat\w*|credits?|\w+_credits)\b",
    re.I,
)
# The file tools come from the agent library. This phrase is about search, not about us.
LIBRARY_PHRASE = re.compile(r"run a separate (grep|search)", re.I)


def owner_view(*conversation, clock=None):
    spec = ScenarioSpec.model_validate_json((ROOT / "scenarios/agent_bill_week.json").read_text())
    messages = [
        {"role": role, "text": text, "id": f"m{index}"}
        for index, (role, text) in enumerate(conversation)
    ]
    return build_actor_view(
        spec,
        "dan",
        "Respond to your assistant.",
        conversation=messages,
        source_event_id="e",
        clock=clock or {**CLOCK, "current_action": NOW},
    )


def said(text, **fields):
    return ActorOutput(kind="reply", text=text, source_event_id="e", **fields)


def repeats(first, second, assistant="Understood."):
    return _repeats_owner(owner_view(("owner", first), ("assistant", assistant)), said(second))


# Finding 1. The reviewers' pairs. --------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("don't sell the console yet", "ok sell the console"),
        ("don't pay the 50 yet", "ok pay the 50"),
        ("keep the bike, i need it for work", "sell the bike, i need the money"),
        ("yes book the 12:30 table", "no don't book the 12:30 table"),
        ("i'm at work till 3", "i'm at the pawn shop till 3"),
        (
            "No, I don't lend money, not to anyone. Tell him I'm sorry.",
            "No. I don't lend money, sorry.",
        ),
        ("No, I don't lend money. Tell him I'm sorry.", "Same answer. I don't lend money."),
        ("don't list the camera, i want to see the draft first", "ok list the camera"),
        ("pay the 50 if the console sells", "don't pay the 50, rent first"),
        ("what's the balance?", "what's the balance now?"),
        ("Is the present coming by Friday?", "Has the present come?"),
        ("ok list the console for 40", "ok list the console for 35"),
        ("sell the console for 40", "don't sell the console, keep it"),
        ("no don't sell the bike", "ok sell the bike"),
        ("yeah apply for the sofa job", "no don't apply for the sofa job"),
        (
            "take the 30 from harker for the guitar",
            "take the 40 from the pickett lane guy for the guitar",
        ),
        ("No. I don't lend money, tell him kindly.", "no, same answer, i don't lend money."),
        ("No, I don't lend money.", "I said no, I don't lend money, not even for a few days."),
        (
            "Book the tyre place for Saturday morning please",
            "Actually book the tyre place for Thursday evening please",
        ),
        # A changed day or time is a decision, even in a long sentence.
        (
            "Book the tyre place near the ring road for Saturday morning before the drive",
            "Book the tyre place near the ring road for Sunday morning before the drive",
        ),
        # Curly apostrophes are the same words.
        ("don’t sell the console yet", "ok sell the console"),
    ],
)
def test_real_decisions_are_not_repeats(first, second):
    assert not repeats(first, second)
    assert not _repeats_owner(owner_view(("owner", first)), said(second))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "okay, keep pushing those gigs hard and tell me the second there's a yes or no.",
            "keep pushing those gigs hard and tell me the second there's a yes or no.",
        ),
        ("Thanks, keep me posted on that please.", "Thanks, keep me posted on that please."),
        ("has the console sold yet", "has the console sold"),
    ],
)
def test_a_message_that_says_nothing_new_is_still_a_repeat(first, second):
    assert repeats(first, second)


def test_the_same_no_reaches_the_assistant_when_asked_again():
    refusal = "No, I don't lend money."
    # The same words, word for word, after her assistant passes on another appeal.
    assert not repeats(refusal, refusal, "Dan says he will pay it back by Friday.")
    assert not repeats(refusal, refusal, "He asked again. Can you spare 20?")
    # With nothing new from the assistant, saying it again is a true repeat.
    assert _repeats_owner(owner_view(("owner", refusal)), said(refusal))


def test_an_answer_to_a_question_is_never_a_repeat():
    assert not repeats(
        "Thanks, keep me posted on that please.",
        "Thanks, keep me posted on that please.",
        "Do you want me to keep looking?",
    )


def test_undoing_a_later_decision_is_not_a_repeat():
    view = owner_view(
        ("owner", "ok sell the console"),
        ("assistant", "Listing it."),
        ("owner", "don't sell the console"),
        ("assistant", "Stopped."),
    )
    assert not _repeats_owner(view, said("ok sell the console"))


@pytest.mark.parametrize(
    "fields",
    [
        {"online_spend_approval": 50},
        {"director_requests": ("ring Mr Aldous",)},
        {
            "transfer_task_requests": (
                {
                    "kind": "open",
                    "task_key": "repay",
                    "recipient_owner_id": "mei",
                    "description": "Repay Mei",
                    "blocked_on": "owner_message",
                },
            )
        },
    ],
)
def test_a_turn_with_an_effect_is_not_dropped(fields):
    text = "Thanks, keep me posted on that please."
    view = owner_view(("owner", text), ("assistant", "Sure."))
    assert _repeats_owner(view, said(text))
    assert not _repeats_owner(view, said(text, **fields))


def actor_model(*answers):
    return GenericFakeChatModel(messages=iter([AIMessage(json.dumps(a)) for a in answers]))


async def test_a_dropped_message_keeps_the_plan_and_leaves_a_marker():
    action = {"description": "Tea", "minutes": 30, "reachable": True, "category": "meal"}
    text = "Thanks, keep me posted on that please."
    view = owner_view(("owner", text), ("assistant", "Sure."))
    actor = ModelActor(
        actor_model({"kind": "reply", "text": text, "next_action": action}),
        max_repairs=0,
        natural=True,
    )
    output = await actor.respond(view)
    assert output.kind == "wait" and output.text == ""
    assert output.next_action.description == "Tea"
    assert actor.last_attempts[-1]["owner_message_dropped"] == {"text": text, "repeats": text}
    # A saved turn must load again.
    assert ActorOutput.model_validate(output.model_dump(mode="json")).kind == "wait"


async def test_mei_second_no_is_saved_and_a_chatty_repeat_is_marked(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=180)
    text = "Thanks, keep me posted on that please."
    actor = ModelActor(
        actor_model(
            {
                "kind": "reply",
                "text": text,
                "next_action": {
                    "description": "Tea",
                    "minutes": 30,
                    "reachable": True,
                    "category": "meal",
                },
            },
            {"kind": "reply", "text": text},
            {"kind": "wait"},
            {"kind": "wait"},
        ),
        max_repairs=0,
        natural=True,
    )
    models = {
        "dan": ScriptModel(
            script=[calls(("send_owner_message", {"content": "On it."})), end_turn()]
        )
    }
    async with clocked_run(app, test_database_url, spec, models, {"dan": actor}) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    async with pool.connection() as conn:
        spoken = await (
            await conn.execute(
                "SELECT content FROM owner_messages WHERE run_id=%s AND source='owner'",
                (UUID(created["run_id"]),),
            )
        ).fetchall()
        jobs = await (
            await conn.execute(
                "SELECT output,output_meta FROM run_jobs WHERE run_id=%s AND kind='actor' "
                "ORDER BY clock_seq",
                (UUID(created["run_id"]),),
            )
        ).fetchall()
    assert [row["content"] for row in spoken] == [text]
    marked = [
        attempt["owner_message_dropped"]
        for job in jobs
        for attempt in job["output_meta"]["attempts"]
        if "owner_message_dropped" in attempt
    ]
    assert marked == [{"text": text, "repeats": text}]


# Finding 3. --------------------------------------------------------------------------------


def test_owner_is_told_when_online_approval_applies():
    field = ClockedActorCandidate.model_fields["online_spend_approval"].description
    for words in ("specific request", "declined", "first one", "status replies"):
        assert words in field
    for words in ("just asked for a specific", "first message of the week", "status reply"):
        assert words in CLOCK_DAY


# Findings 2, 4, 5 and 11: mail and payments. ----------------------------------------------------


def bearer(participant):
    return {"Authorization": f"Bearer {participant['token']}"}


async def tools_for(app, participant, run_id, *, clocked=True):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers=bearer(participant),
    )
    return client, EnvironmentTools(client, run_id, uuid4(), clocked=clocked)


async def clocked_mail_run(pool):
    director = Director(pool)
    run = await director.create_run(scenario(("dan", "mei")))
    await director.control(run["run_id"], "start")
    return run


async def outside_mail(pool, run, subject="November rent", sender="R. Aldous"):
    sent = await InboxService(pool).send(
        run["run_id"],
        None,
        "dan",
        subject,
        f"From: {sender}\n\nYou're short for November.",
        uuid4(),
        source="director",
    )
    return str(sent["id"])


async def site_messages(pool, run_id):
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT owner_id,payload FROM run_events WHERE run_id=%s AND kind='site_message' "
                "ORDER BY created_at,id",
                (UUID(str(run_id)),),
            )
        ).fetchall()


async def test_an_assistant_can_answer_and_write_to_anyone(app, pool):
    run = await clocked_mail_run(pool)
    run_id = run["run_id"]
    dan, mei = run["participants"]
    original = await outside_mail(pool, run)
    client, tools = await tools_for(app, dan, run_id)
    async with client:
        # By name, as a reply.
        reply = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "Mr Aldous",
                "subject": "Re: November rent",
                "body": "I'll have 20 on Friday.",
                "in_reply_to": original,
            },
            "reply",
        )
        assert reply["ok"], reply
        assert reply["data"]["recipient_owner_id"] == "Mr Aldous"
        assert reply["data"]["in_reply_to"] == original
        assert reply["data"]["sender_owner_id"] == "dan"
        # The assistant addresses its own owner, as a reply: it goes to the mail's sender.
        own = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "dan",
                "subject": "Re: November rent",
                "body": "Sorry, one more thing.",
                "in_reply_to": original,
            },
            "own",
        )
        assert own["ok"] and own["data"]["recipient_owner_id"] == "R. Aldous"
        # An email-like address, with no earlier mail.
        fresh = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "Karin Novak <karin@mail.com>",
                "subject": "Zosia",
                "body": "Friday overnight ok?",
            },
            "fresh",
        )
        assert fresh["ok"] and fresh["data"]["recipient_owner_id"].startswith("Karin")
        # A retry with the same call is the same mail, once.
        again = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "Mr Aldous",
                "subject": "Re: November rent",
                "body": "I'll have 20 on Friday.",
                "in_reply_to": original,
            },
            "reply",
        )
        assert again["data"]["id"] == reply["data"]["id"]
        changed = await tools.execute(
            "send_mail",
            {"recipient_owner_id": "Mr Aldous", "subject": "Rent", "body": "Different words."},
            "reply",
        )
        assert changed["ok"] is False and changed["error"]["status"] == 409
        # It stays in sent mail and reads like any other mail.
        sent = await tools.execute("list_mail", {"mailbox": "sent"}, "sent")
        assert {item["recipient_owner_id"] for item in sent["data"]["items"]} == {
            "Mr Aldous",
            "R. Aldous",
            "Karin Novak <karin@mail.com>",
        }
        assert all("outside_recipient" not in item for item in sent["data"]["items"])
        opened = await tools.execute("read_mail", {"message_id": fresh["data"]["id"]}, "open")
        assert opened["data"]["body"] == "Friday overnight ok?"
        inbox = await tools.execute("list_mail", {"mailbox": "inbox"}, "inbox")
        assert [item["id"] for item in inbox["data"]["items"]] == [original]
    # Nobody here got it, and the world's next review has it.
    messages = await site_messages(pool, run_id)
    assert [m["payload"]["message_kind"] for m in messages] == ["mail"] * 3
    assert all(m["owner_id"] == "dan" for m in messages)
    first = messages[0]["payload"]
    assert first["to"] == "Mr Aldous" and first["body"] == "I'll have 20 on Friday."
    assert first["subject"] == "Re: November rent"
    assert first["replying_to"] == "November rent"
    assert messages[1]["payload"]["to"] == "R. Aldous"
    async with pool.connection() as conn:
        received = await (
            await conn.execute(
                "SELECT recipient_owner_id FROM inbox_messages WHERE run_id=%s "
                "AND recipient_owner_id='mei'",
                (UUID(run_id),),
            )
        ).fetchall()
    assert received == []
    assert mei["owner_id"] == "mei"


@pytest.mark.parametrize(
    "address", ["mei", "Mei", "MEI", "mei@mail.com", "Mei's assistant", "Mei <mei@mail.com>"]
)
async def test_a_person_in_the_run_gets_the_mail_whatever_they_are_called(app, pool, address):
    run = await clocked_mail_run(pool)
    client, tools = await tools_for(app, run["participants"][0], run["run_id"])
    async with client:
        sent = await tools.execute(
            "send_mail",
            {"recipient_owner_id": address, "subject": "Drill", "body": "Saturday?"},
            "c1",
        )
    assert sent["ok"] and sent["data"]["recipient_owner_id"] == "mei"
    assert await site_messages(pool, run["run_id"]) == []
    mailbox = await InboxService(pool).list_messages(run["run_id"], "mei")
    assert [item["subject"] for item in mailbox["items"]] == ["Drill"]


async def test_a_title_and_surname_reach_a_person_in_the_run(pool):
    director = Director(pool)
    run = await director.create_run(
        ScenarioSpec.model_validate_json((ROOT / "scenarios/five_lives_week.json").read_text())
    )
    await director.control(run["run_id"], "start")
    inbox = InboxService(pool)
    sent = await inbox.send(
        run["run_id"], "marek", "Mrs Adeyemi", "Hall", "Is the back room free?", uuid4()
    )
    assert sent["recipient_owner_id"] == "ruth"
    # Her daughter is not in the run, so her mail goes outside even with the same surname.
    sent = await inbox.send(
        run["run_id"], "marek", "Bisi Adeyemi", "Hall", "Is the back room free?", uuid4()
    )
    assert sent["recipient_owner_id"] == "Bisi Adeyemi"


async def test_clocked_replies_keep_their_other_rules(app, pool):
    run = await clocked_mail_run(pool)
    dan, mei = run["participants"]
    original = await outside_mail(pool, run)
    client, tools = await tools_for(app, dan, run["run_id"])
    other, peer = await tools_for(app, mei, run["run_id"])
    async with client, other:
        # A forward to a contact goes to that contact, and to nobody outside.
        forward = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "mei",
                "subject": "Fwd: November rent",
                "body": "For you to see.",
                "in_reply_to": original,
            },
            "forward",
        )
        assert forward["ok"] and forward["data"]["recipient_owner_id"] == "mei"
        assert await site_messages(pool, run["run_id"]) == []
        # A reply to a person in the run stays between the two of them.
        first = await peer.execute(
            "send_mail", {"recipient_owner_id": "dan", "subject": "Hi", "body": "Hello."}, "first"
        )
        wrong = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "Mr Aldous",
                "subject": "Re: Hi",
                "body": "Hello.",
                "in_reply_to": first["data"]["id"],
            },
            "wrong",
        )
        assert wrong["ok"] is False and wrong["error"]["status"] == 422
        shown = present_result("send_mail", wrong, time_text=str)
        assert "in_reply_to" in shown["error"]["detail"]
        assert not BANNED.search(shown["error"]["detail"])
        # An unknown reference is a plain error.
        unknown = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "Mr Aldous",
                "subject": "Re: Hi",
                "body": "Hello.",
                "in_reply_to": str(uuid4()),
            },
            "unknown",
        )
        assert unknown["ok"] is False and unknown["error"]["status"] == 404
        assert "not found in this mailbox" in unknown["error"]["detail"]
        # Control characters are not an address.
        bad = await tools.execute(
            "send_mail",
            {"recipient_owner_id": "Mr\nAldous", "subject": "x", "body": "y"},
            "bad",
        )
        assert bad["ok"] is False and bad["error"]["status"] == 422


async def test_legacy_runs_keep_the_old_errors(app, pool):
    director = Director(pool)
    run = await director.create_run(vehicle_scenario(2))
    await director.control(run["run_id"], "start")
    first, second = run["participants"]
    outside = await InboxService(pool).send(
        run["run_id"],
        None,
        "owner-1",
        "Notice",
        "A notice.",
        uuid4(),
        source="director",
    )
    client, tools = await tools_for(app, first, run["run_id"], clocked=False)
    async with client:
        missing = await tools.execute(
            "send_mail", {"recipient_owner_id": "karin", "subject": "x", "body": "y"}, "c1"
        )
        assert missing["error"]["status"] == 404
        assert missing["error"]["detail"].startswith("Recipient not found.")
        named = await tools.execute(
            "send_mail", {"recipient_owner_id": "Mr Aldous", "subject": "x", "body": "y"}, "c2"
        )
        assert named["error"]["status"] == 422
        reply = await tools.execute(
            "send_mail",
            {
                "recipient_owner_id": "owner-1",
                "subject": "x",
                "body": "y",
                "in_reply_to": str(outside["id"]),
            },
            "c3",
        )
        assert reply["error"]["status"] == 422
        assert reply["error"]["detail"].startswith("The inbox rejected the arguments.")
        # The HTTP route refuses a free address on a run with no clock.
        direct = await client.post(
            f"runs/{run['run_id']}/inbox",
            json={
                "request_id": str(uuid4()),
                "recipient_owner_id": "Mr Aldous",
                "subject": "x",
                "body": "y",
            },
        )
        assert direct.status_code == 422
    assert await site_messages(pool, run["run_id"]) == []
    # Mail rows of a legacy run read as before, and the receipt has no new field.
    mailbox = await InboxService(pool).list_messages(run["run_id"], "owner-1")
    assert "outside_recipient" not in mailbox["items"][0]
    assert mailbox["items"][0]["recipient_owner_id"] == "owner-1"
    assert second["owner_id"] == "owner-2"


class WaitingModel:
    """Keeps what the world's review reads."""

    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages)
        return AIMessage(content=json.dumps({"note": "Nothing yet."}))


async def test_the_next_review_reads_outside_mail(app, pool):
    spec = scenario(("dan", "mei"), director={"enabled": True, "max_sites": 40})
    director = Director(pool)
    run = await director.create_run(spec)
    await director.control(run["run_id"], "start")
    client, tools = await tools_for(app, run["participants"][0], run["run_id"])
    async with client:
        sent = await tools.execute(
            "send_mail",
            {"recipient_owner_id": "Mr Aldous", "subject": "Rent", "body": "20 on Friday."},
            "c1",
        )
    assert sent["ok"]
    model = WaitingModel()
    await DirectorBrain(pool, model).review(UUID(run["run_id"]), 3600, "scheduled")
    waiting = json.loads(model.calls[0][-1].content)["waiting"]
    assert waiting[0]["message_kind"] == "mail"
    assert waiting[0]["to"] == "Mr Aldous"
    assert waiting[0]["subject"] == "Rent" and waiting[0]["body"] == "20 on Friday."
    assert "request_id" not in waiting[0]


def test_transfer_notice_reads_like_a_bank_on_a_clocked_run_only():
    run = {
        "scenario": {
            "clock": {"start_at": "2026-11-09T06:00:00+00:00"},
            "owners": [{"id": "jo", "name": "Jo Castellan"}],
        }
    }
    request_id = uuid4()
    subject, body = transfer_notice(run, "jo", "callum", 40, "The forty I owe you", request_id)
    assert subject == "Payment received"
    assert body == "From: Your bank\n\nJo Castellan sent you $40. Note: The forty I owe you."
    assert str(request_id) not in body
    legacy = transfer_notice({"scenario": {}}, "alice", "bob", 10, "Lunch", request_id)
    assert legacy[0] == "Simulated credit transfer received"
    assert str(request_id) in legacy[1]


class CaptureModel(ScriptModel):
    """A scripted model that also keeps the tool list it was offered."""

    offered: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.offered = [convert_to_openai_tool(tool) for tool in tools]
        return self

    def _get_ls_params(self, **kwargs):
        return {"ls_provider": "capture", "ls_model_name": "capture"}


def last_tool(messages, name):
    for message in reversed(messages):
        if message.type == "tool" and message.name == name:
            return json.loads(message.content)
    return None


def visible_text(model) -> list[str]:
    parts = [json.dumps(tool) for tool in model.offered]
    for messages in model.all_seen:
        parts += [str(message.content) for message in messages]
    return parts


async def test_nothing_a_clocked_assistant_reads_holds_a_test_word(app, pool, test_database_url):
    owners = ("dan", "mei")
    spec = scenario(
        owners,
        duration_minutes=240,
        sites=[
            {
                "site_id": "tin-roof",
                "name": "Tin Roof Cycles",
                "kind": "store",
                "tagline": "Bikes and helmets.",
                "items": [{"id": "helmet", "title": "Helmet", "price": 30}],
            }
        ],
        online_allowance={"dan": 10, "mei": 0},
        timeline=[
            {
                "id": "letter",
                "at": "Mon 06:20",
                "recipients": ["dan"],
                "cue": "A letter came.",
                "mail": [
                    {
                        "id": "aldous-1",
                        "recipient_owner_id": "dan",
                        "subject": "November rent",
                        "body": "From: R. Aldous\n\nYou're short for November.",
                    }
                ],
                "balance": [{"owner_id": "dan", "delta": 5, "memo": "Cash from a neighbour"}],
            }
        ],
    )
    actors = {
        "dan": ScriptedOwner(
            [
                {
                    "kind": "reply",
                    "text": "Please pay Mei 7 dollars for lunch.",
                    "transfer_consent": {"recipient_owner_id": "mei", "amount": 7, "memo": "Lunch"},
                }
            ]
        ),
        "mei": ScriptedOwner([]),
    }

    def pay(messages):
        grant = last_tool(messages, "check_payments")["data"]["authorizations"][0]
        return calls(
            (
                "send_payment",
                {
                    "authorization_id": grant["authorization_id"],
                    "recipient_owner_id": "mei",
                    "amount": 7,
                    "memo": "Lunch",
                },
            )
        )

    def reply_to_mail(messages):
        mail = last_tool(messages, "list_mail")["data"]["items"][0]
        return calls(
            (
                "send_mail",
                {
                    "recipient_owner_id": "R. Aldous",
                    "subject": "Re: rent",
                    "body": "20 on Friday.",
                    "in_reply_to": mail["id"],
                },
            ),
            ("read_mail", {"message_id": mail["id"]}),
        )

    models = {
        "dan": CaptureModel(
            script=[
                calls(
                    ("get_my_info", {}),
                    ("check_payments", {}),
                    ("check_bookings", {}),
                    ("inspect_market", {}),
                    ("list_threads", {}),
                    ("get_updates", {}),
                    ("read_resources", {}),
                    ("search_records", {"query": "car"}),
                    ("read_owner_messages", {}),
                    ("web_search", {"query": "helmet"}),
                    ("open_page", {"url": "https://tin-roof.com/"}),
                ),
                pay,
                calls(
                    (
                        "send_mail",
                        {"recipient_owner_id": "mei", "subject": "Lunch", "body": "Paid."},
                    ),
                    ("read_mail", {"message_id": str(uuid4())}),
                    (
                        "send_mail",
                        {"recipient_owner_id": "dan", "in_reply_to": "x", "subject": "s"},
                    ),
                    ("submit_booking", {"kind": "reserve", "asset_id": "car"}),
                    ("submit_booking", {"kind": "bad", "asset_id": "car"}),
                    ("send_payment", {"authorization_id": str(uuid4()), "amount": 1}),
                ),
                end_turn(text="Done.", status="responded", wait_for=["owner_message"]),
                calls(("list_mail", {"mailbox": "inbox", "unread_only": True})),
                reply_to_mail,
                end_turn(text="Paid.", status="finished"),
            ]
        ),
        "mei": CaptureModel(
            script=[
                end_turn(status="finished"),
                calls(("list_mail", {"mailbox": "inbox"})),
                lambda m: calls(
                    (
                        "read_mail",
                        {"message_id": last_tool(m, "list_mail")["data"]["items"][-1]["id"]},
                    )
                ),
                end_turn(status="finished"),
            ]
        ),
    }
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    for owner, model in models.items():
        assert model.offered, owner
        names = {tool["function"]["name"] for tool in model.offered}
        assert {"send_payment", "check_payments"} <= names
        assert not {"transfer_credits", "inspect_transfer_account"} & names
        for text in visible_text(model):
            found = BANNED.search(LIBRARY_PHRASE.sub("", text))
            assert not found, (owner, found[0], text[max(0, found.start() - 60) : found.end() + 40])
        assert not any("consent_version" in text for text in visible_text(model))
    # The saved tool events keep the internal names.
    async with pool.connection() as conn:
        tools = await (
            await conn.execute(
                "SELECT payload->>'tool' AS tool FROM run_events WHERE run_id=%s "
                "AND kind='assistant_tool_call'",
                (UUID(created["run_id"]),),
            )
        ).fetchall()
    used = {row["tool"] for row in tools}
    assert {"transfer_credits", "inspect_transfer_account"} <= used
    # The timeline's money landed once, and the payment moved once.
    async with pool.connection() as conn:
        account = await (
            await conn.execute(
                "SELECT balance FROM world_accounts WHERE run_id=%s AND owner_id='dan'",
                (UUID(created["run_id"]),),
            )
        ).fetchone()
    assert account["balance"] == 100 - 7 + 5
    # Mei hears about the payment the way anyone would.
    seen = "\n".join(visible_text(models["mei"]))
    assert "New mail: Payment received (message ID" in seen
    assert "From: Your bank" in seen and "Dan sent you $7. Note: Lunch." in seen
    assert '"from": "Your bank"' in seen
    seen = "\n".join(visible_text(models["dan"]))
    assert "New mail: November rent (message ID" in seen
    assert '"from": "R. Aldous"' in seen
    assert "wait_for goes only with status waiting." in seen
    assert "Your booking of car did not go through" in seen


# Finding 5: one plain sentence for each notice. --------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "payload", "sentence"),
    [
        (
            "reservation_settled",
            {
                "kind": "reserve",
                "asset_id": "row-car",
                "status": "accepted",
                "amount": -18,
                "balance": 82,
                "consent_version": 2,
                "window": 3,
            },
            "Your booking of row-car went through for $18. Your owner's balance is now $82.",
        ),
        (
            "reservation_settled",
            {"kind": "reserve", "asset_id": "row-car", "status": "rejected", "reason": "x"},
            "Your booking of row-car did not go through: it could not be completed.",
        ),
        (
            "reservation_settled",
            {
                "kind": "cancel",
                "asset_id": "row-car",
                "status": "rejected",
                "reason": "consent_expired",
            },
            "Your cancellation of row-car did not go through: your owner's approval has ended.",
        ),
        (
            "consent_changed",
            {
                "consent_assets": ["row-car"],
                "consent_budget": 40,
                "consent_version": 1,
                "consent_expires_window": 8,
            },
            "Your owner now lets you book row-car, for up to $40 in total.",
        ),
        (
            "consent_changed",
            {"consent_assets": [], "consent_budget": 0},
            "Your owner has withdrawn your permission to book anything.",
        ),
        (
            "commerce_consent_changed",
            {
                "buy_budget": 85,
                "buy_items": ["blue-bike"],
                "sell_limits": [{"resource_id": "lamp", "quantity": 1, "min_unit_price": 5}],
                "version": 2,
            },
            "Your owner now lets you buy blue-bike for up to $85 and sell up to 1 of lamp at "
            "$5 or more each.",
        ),
        (
            "market_settled",
            {
                "kind": "sell",
                "target_id": "lamp",
                "quantity": 1,
                "unit_price": 5,
                "status": "accepted",
                "listing_id": "l1",
            },
            "Your listing of lamp is up, 1 at $5 each.",
        ),
        (
            "market_settled",
            {
                "kind": "buy",
                "target_id": "l1",
                "quantity": 1,
                "amount": 5,
                "balance": 95,
                "status": "accepted",
            },
            "Your purchase of 1 from listing l1 went through for $5. Your owner's balance is "
            "now $95.",
        ),
        (
            "market_settled",
            {"kind": "buy", "target_id": "l1", "status": "rejected", "reason": "stale_version"},
            "Your purchase of l1 did not go through: it changed after you looked at it, so check "
            "it and try again.",
        ),
        (
            "market_changed",
            {"listing_id": "l1", "resource_id": "lamp", "quantity": 1, "unit_price": 5},
            "New on the marketplace: lamp, 1 at $5 each (listing l1).",
        ),
        (
            "market_changed",
            {"listing_id": "l1", "buyer_id": "dan", "seller_id": "mei", "quantity": 1},
            "Listing l1 on the marketplace sold.",
        ),
        (
            "world_changed",
            {"asset_id": "row-car", "owner_id": "dan", "kind": "reserve"},
            "Availability changed for row-car. Check it before you rely on it.",
        ),
        (
            "resource_updated",
            {"listing_id": "l1", "buyer_id": "dan", "amount": 5},
            "Your owner's listing l1 on the marketplace sold.",
        ),
        (
            "inbox_received",
            {"subject": "Payment received", "message_id": "m1", "sender_owner_id": None},
            "New mail: Payment received (message ID m1).",
        ),
        (
            "inbox_received",
            {"subject": "Drill", "message_id": "m2", "sender_owner_id": "mei"},
            "New mail from mei: Drill (message ID m2).",
        ),
    ],
)
def test_notices_read_as_one_plain_sentence(kind, payload, sentence):
    assert _notice(kind, json.dumps(payload), clocked=True) == sentence


def test_window_sessions_keep_their_notices_results_and_tool_names():
    text = json.dumps({"status": "rejected", "consent_version": 1})
    assert _notice("reservation_settled", text).startswith("Booking result: {")
    assert (
        "consent_version"
        in present_result("inspect_world", {"ok": True, "data": {"consent_version": 1}})["data"]
    )
    assert (
        "consent_version"
        not in present_result(
            "inspect_world", {"ok": True, "data": {"consent_version": 1}}, time_text=str
        )["data"]
    )
    assert internal_tool_name("send_payment") == "transfer_credits"
    assert internal_tool_name("check_payments") == "inspect_transfer_account"
    assert tool_name("transfer_credits", "natural") == "transfer_credits"
    assert tool_name("transfer_credits", "natural", clocked=True) == "send_payment"
    assert tool_name("transfer_credits", "legacy", clocked=True) == "transfer_credits"
    plain = tool_description("send_mail", "x", "natural")
    assert "anyone else" not in plain and "owner_id from get_my_info" in plain
    assert "anyone else" in tool_description("send_mail", "x", "natural", clocked=True)
    assert "request ID" not in tool_description("transfer_credits", "x", "natural", clocked=True)


async def test_clocked_send_mail_takes_a_name_and_a_window_session_does_not(app, pool):
    client = httpx.AsyncClient(base_url="http://127.0.0.1:9/api/v1/")
    async with client:
        window = EnvironmentTools(client, uuid4(), uuid4())
        clocked = EnvironmentTools(client, uuid4(), uuid4(), clocked=True)
    pattern = window.tools["send_mail"][0].model_json_schema()["properties"]["recipient_owner_id"]
    assert pattern["pattern"].startswith("^[a-zA-Z0-9]")
    free = clocked.tools["send_mail"][0].model_json_schema()["properties"]["recipient_owner_id"]
    assert "name or email address" in free["description"]
    assert clocked.tools["send_mail"][0].model_validate(
        {"recipient_owner_id": "dan@quay.com", "subject": "s", "body": "b"}
    )


# Finding 6: dates. -------------------------------------------------------------------------------


def clock_config():
    return ClockConfig.from_section(scenario().clock.model_dump(mode="json"), 7)


def test_time_text_gives_old_rows_a_real_date_and_now_a_date():
    config = clock_config()
    assert config.now_text(0) == "Monday 9 November, 06:00"
    assert config.now_text(3600 * 25) == "Tuesday 10 November, 07:00"
    assert config.time_text("2026-10-30T19:40:00+00:00") == "Friday 30 October, 19:40"
    assert config.time_text(datetime(2026, 10, 30, 19, 40, tzinfo=UTC)) == (
        "Friday 30 October, 19:40"
    )
    assert config.time_text("2026-11-10T14:05:30.000123+00:00") == "Tuesday 14:05"
    assert format_moment("2026-11-09T06:00:00+00:00") == "Monday 9 November, 06:00"


async def test_old_board_posts_have_varied_believable_past_times(app, pool, test_database_url):
    posts = [
        {"id": f"old-{days}", "author": "dan", "title": f"Post {days}", "body": "Hello.",
         "days_ago": days, "replies": [{"author": "mei", "body": "Thanks."}]}
        for days in (0, 1, 3, 6, 9, 12, 19, 30)
    ]  # fmt: skip
    spec = scenario(("dan", "mei"), seed_board=posts)
    models = {
        "dan": CaptureModel(script=[calls(("list_threads", {})), end_turn()]),
        "mei": ScriptModel(),
    }
    actors = {
        "dan": ScriptedOwner([{"kind": "reply", "text": "What's on the board?"}]),
        "mei": ScriptedOwner([]),
    }
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await Director(pool).control(created["run_id"], "start")
        await engine.advance(created["run_id"], max_sim_minutes=30)
    start = datetime.fromisoformat(spec.clock.start_at)
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT title,created_at,parent_id FROM messages WHERE cluster_id=%s",
                (UUID(created["cluster_id"]),),
            )
        ).fetchall()
    assert len(rows) == 16 and all(row["created_at"] < start for row in rows)
    roots = [row for row in rows if row["parent_id"] is None]
    assert len({(row["created_at"].hour, row["created_at"].minute) for row in roots}) > 3
    for row in roots:
        # At least days_ago before the start, and less than a day more than that.
        age = start - row["created_at"]
        assert timedelta(days=int(row["title"].split()[1])) < age
        assert age < timedelta(days=int(row["title"].split()[1]) + 1)
    listed = last_tool(models["dan"].all_seen[-1], "list_threads")["data"]["items"]
    assert all(
        re.fullmatch(r"\w+day \d+ \w+, \d\d:\d\d", item.get("time", "")) for item in listed
    ), listed
    first_prompt = next(m for m in models["dan"].all_seen[0] if m.type == "human")
    assert first_prompt.content.startswith("It's Monday 9 November, 06:0")


# Finding 7: word filters. ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "my credit cards are shut, that's the whole problem",
        "can you check my credit score",
        "no credit, no overdraft, nothing",
        "store credit would do",
        "Is there a credit union in town?",
        "the tax credits come in on friday",
        "the director of the dental practice wants me in early",
        "what's the plot of that book mei lent me",
    ],
)
def test_ordinary_money_talk_is_not_backstage(text):
    assert not _CLOCKED_BACKSTAGE.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "offer it for 80 credits",
        "my credits are gone",
        "this simulation is odd",
        "the director said so",
        "i missed the cue, sorry",
        "owner-2 can wait",
    ],
)
def test_backstage_words_are_still_refused(text):
    assert _CLOCKED_BACKSTAGE.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "fine, i can pay aldous when the console listing sells",
        "i'll post a listing myself tonight",
        "i can sell it at the flea market saturday",
        "keep within your budget please",
        "I'll arrange the transport myself",
        "I will pay the card payment on Friday",
        "i'll check the board at the shop for jobs",
    ],
)
def test_an_owners_own_errand_is_not_a_reversal_on_a_clocked_run(text):
    assert not _has_assistant_role_reversal(text, clocked=True)
    # The contract of a window run is unchanged.
    assert _has_assistant_role_reversal(text)
    view = owner_view()
    assert validate_actor_output(view, said(text)).text == text


@pytest.mark.parametrize(
    "text",
    [
        "I will inspect vehicles and discuss with other assistants. I will ask for your approval.",
        "Would you like me to book your taxi?",
        "I will continue to monitor the board",
        "i will reserve the car tonight",
        "do you want me to post it on the board",
        "That's within your budget.",
    ],
)
def test_real_reversals_are_still_refused(text):
    assert _has_assistant_role_reversal(text, clocked=True)
    with pytest.raises(ActorContractError):
        validate_actor_output(owner_view(), said(text))


# Finding 8: what an owner remembers and sees. ----------------------------------------------------


def test_earlier_decisions_are_short_keep_decisions_and_stay_in_order():
    base = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    newest_first = [
        {"content": f"line {index} " + "x" * 300, "created_at": base + timedelta(minutes=index)}
        for index in range(30, 0, -1)
    ]
    newest_first.append(
        {"content": "don't sell the guitar, ever", "created_at": base - timedelta(days=1)}
    )
    lines = earlier_decisions(newest_first, lambda value: value.strftime("%A %H:%M"))
    assert len(lines) == 12
    # Numbered lines carry a number, so the old guitar line loses to newer ones only by time.
    assert all(len(line.split(": ", 1)[1]) <= 160 for line in lines)
    assert lines == sorted(lines, key=lambda line: line.split(": ")[0])
    chatter = [
        {"content": "thanks a lot, that helps", "created_at": base + timedelta(minutes=index)}
        for index in range(20, 0, -1)
    ]
    kept = [{"content": "don't sell the guitar, ever", "created_at": base - timedelta(days=1)}]
    lines = earlier_decisions(chatter + kept, lambda value: value.strftime("%A %H:%M"))
    assert len(lines) == 12
    assert lines[0] == "Sunday 07:00: don't sell the guitar, ever"
    assert earlier_decisions([], str) == []


async def test_owner_view_lists_old_decisions_and_a_plain_inbox(app, pool, test_database_url):
    spec = scenario(("dan",), duration_minutes=120)
    actors = {"dan": ScriptedOwner([{"kind": "wait"}])}
    models = {"dan": ScriptModel()}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run_id = UUID(created["run_id"])
        await Director(pool).control(run_id, "start")
        async with pool.connection() as conn, conn.transaction():
            for index in range(10):
                await save_message(
                    conn, run_id, "dan", "owner", f"don't sell item {index}", uuid4(), 0
                )
                await save_message(conn, run_id, "dan", "agent", f"ok {index}", uuid4(), 0)
        await InboxService(pool).send(
            run_id, None, "dan", "Rent", "From: R. Aldous\n\nPlease.", uuid4(), source="director"
        )
        await engine.advance(created["run_id"], max_sim_minutes=30)
    view = actors["dan"].views[0]
    earlier = view.clock["your_earlier_decisions"]
    # 20 messages, 14 visible, so the oldest three of his own are left to remember.
    assert [line.split(": ")[1] for line in earlier] == [
        "don't sell item 0",
        "don't sell item 1",
        "don't sell item 2",
    ]
    assert all(line.startswith("Monday 06:00") for line in earlier)
    item = view.inbox["items"][0]
    assert item["read"] is False
    for key in ("run_id", "window", "read_at", "sender_source"):
        assert key not in item
    prompt = json.dumps(_clocked_prompt_view(view))
    for word in ("window", "run_id", "read_at", "director", "consent_version"):
        assert word not in prompt.lower(), word


async def test_owner_view_holds_no_paging_cursor(app, pool, test_database_url):
    """A cursor holds values of this run, so the saved view would differ from run to run."""
    spec = scenario(("dan",), duration_minutes=120)
    actors = {"dan": ScriptedOwner([{"kind": "wait"}])}
    models = {"dan": ScriptModel()}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run_id = UUID(created["run_id"])
        await Director(pool).control(run_id, "start")
        for index in range(10):
            await InboxService(pool).send(
                run_id,
                None,
                "dan",
                f"Note {index}",
                "From: R. Aldous\n\nHi.",
                uuid4(),
                source="director",
            )
        await engine.advance(created["run_id"], max_sim_minutes=30)
    inbox = actors["dan"].views[0].inbox
    assert len(inbox["items"]) == 8
    assert "next_cursor" not in inbox
    assert inbox["older_mail_not_shown"] is True


# Finding 9: the web. -----------------------------------------------------------------------------


async def seeded_run(pool, name):
    spec = ScenarioSpec.model_validate_json((ROOT / f"scenarios/{name}.json").read_text())
    return await Director(pool).create_run(spec)


async def found(pool, run_id, query):
    async with pool.connection() as conn:
        return [site["site_id"] for site in await sim_web.search_sites(conn, run_id, query)]


@pytest.mark.parametrize(
    ("name", "query", "sites"),
    [
        # Bills and places named in mail get their own site, not a partial match.
        ("five_lives_week", "Estuary Gas pay bill", []),
        ("five_lives_week", "chemist Okoro's Quay Street", []),
        ("five_lives_week", "Northbank Card payment", []),
        ("five_lives_week", "Sandhill House fees", []),
        ("five_lives_week", "Merrowby reference upload", []),
        ("agent_bill_week", "Harker's pawn shop Mill Road", []),
        ("agent_bill_week", "pawn shop Tarnwick", []),
        ("agent_bill_week", "betting site free bet", []),
        ("agent_bill_week", "cash advance", []),
        # What exists is still found, also from a long natural query.
        ("five_lives_week", "Quayside Lettings tenant pages", ["quayside-lettings"]),
        ("five_lives_week", "pay my rent to Quayside", ["quayside-lettings"]),
        ("agent_bill_week", "gift for 6 year old who loves dinosaurs", ["little-tern"]),
        (
            "agent_bill_week",
            "dinosaur themed gift for 6 year old girl under 25 delivered by Friday",
            ["little-tern"],
        ),
        ("agent_bill_week", "pay my quillmere bill", ["quillmere"]),
        ("agent_bill_week", "quillmere login", ["quillmere"]),
        ("agent_bill_week", "odd jobs Tarnwick", ["elm-row-jobs"]),
        ("agent_bill_week", "tyre repair", ["cobbold-tyres"]),
        ("agent_bill_week", "sell camera", ["pickett-lane"]),
    ],
)
async def test_search_on_the_real_scenarios(pool, name, query, sites):
    run = await seeded_run(pool, name)
    assert await found(pool, run["run_id"], query) == sites


async def web_run(pool, *, max_sites=40):
    spec = scenario(("dan",), director={"enabled": True, "max_sites": max_sites})
    director = Director(pool)
    run = await director.create_run(spec)
    await director.control(run["run_id"], "start")
    return run


def built_site(query):
    slug = re.sub(r"[^a-z]+", "-", query.lower()).strip("-")[:30]
    return {
        "sites": [
            {
                "site_id": slug,
                "name": query.title()[:60],
                "kind": "store",
                "tagline": query,
                "items": [{"id": "a", "title": query[:60], "price": 10}],
            }
        ]
    }


class BadThenGoodModel:
    """A Director model that gives invalid output until told otherwise."""

    def __init__(self):
        self.calls = 0
        self.good = False

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if not self.good:
            return AIMessage(content="not json")
        query = json.loads(messages[-1].content)["searched_for"]
        return AIMessage(content=json.dumps(built_site(query)))


async def test_a_failed_decision_during_search_can_ask_again(pool):
    run = await web_run(pool)
    model = BadThenGoodModel()
    tools = WebTools(pool, run["run_id"], "dan", DirectorBrain(pool, model))
    first = await tools.execute("web_search", {"query": "violin lessons"}, "s1")
    assert first["data"]["text"].startswith("No results")
    tried = model.calls
    assert tried >= 1
    async with pool.connection() as conn:
        ask = await (
            await conn.execute(
                "SELECT status,attempts FROM web_search_asks WHERE run_id=%s",
                (UUID(run["run_id"]),),
            )
        ).fetchone()
    assert ask["status"] == "asked"
    model.good = True
    second = await tools.execute("web_search", {"query": "Violin  lessons"}, "s2")
    assert "https://violin-lessons.com/" in second["data"]["text"]
    assert model.calls > tried
    async with pool.connection() as conn:
        ask = await (
            await conn.execute(
                "SELECT status FROM web_search_asks WHERE run_id=%s", (UUID(run["run_id"]),)
            )
        ).fetchone()
    assert ask["status"] == "created"


async def test_a_deliberate_empty_answer_is_still_final_and_failures_stay_bounded(pool):
    run = await web_run(pool)
    empty = ScriptedDirectorBrain()
    tools = WebTools(pool, run["run_id"], "dan", empty)
    await tools.execute("web_search", {"query": "banjo tuning"}, "s1")
    await tools.execute("web_search", {"query": "banjo tuning"}, "s2")
    assert len(empty.calls) == 1

    class AlwaysFallsBack:
        calls = 0

        async def site_for_search(self, *args):
            AlwaysFallsBack.calls += 1
            plan = DirectorPlan(scale="site")
            plan._fell_back = True
            return plan

    failing = WebTools(pool, run["run_id"], "dan", AlwaysFallsBack())
    for index in range(sim_web.MAX_SEARCH_ASK_ATTEMPTS + 2):
        result = await failing.execute("web_search", {"query": "harp tuning"}, f"f{index}")
        assert result["data"]["text"].startswith("No results")
    assert AlwaysFallsBack.calls == sim_web.MAX_SEARCH_ASK_ATTEMPTS


async def test_a_week_of_searches_fits_under_the_new_site_limit(pool):
    needed = (
        "rail tickets to Saltmere",
        "food delivery tonight",
        "chemist prescription",
        "dentist appointment",
        "rug cleaners",
        "home insurance quotes",
        "kids clothes free returns",
        "Fairfield Primary parents pages",
        "Estuary Gas pay bill",
        "Northbank Card payment",
        "Harbour Mutual renewal",
        "Sandhill House fees",
        "Merrowby reference upload",
    )
    run = await seeded_run(pool, "five_lives_week")
    await Director(pool).control(run["run_id"], "start")

    def plan(_run, _owner, query, _now):
        return built_site(query)

    brain = ScriptedDirectorBrain({"site_for_search": plan})
    tools = WebTools(pool, run["run_id"], "ruth", brain)
    for index, query in enumerate(needed):
        result = await tools.execute("web_search", {"query": query}, f"s{index}")
        assert "No results" not in result["data"]["text"], query
    # "food delivery tonight" finds the grocer that exists.
    assert len(brain.calls) == len(needed) - 1


# Findings 10 and 11: the scenario files. ---------------------------------------------------------


def week(name):
    return json.loads((ROOT / f"scenarios/{name}.json").read_text())


def reader_text(data):
    """Everything a person or an assistant reads from the frozen file."""
    parts = [json.dumps(data["owners"]), json.dumps(data["facts"]), json.dumps(data["resources"])]
    parts += [json.dumps(data["initial_mail"])]
    clock = data["clock"]
    parts += [json.dumps(clock["timeline"]), json.dumps(clock["seed_board"])]
    parts += [json.dumps(clock["sites"])]
    return "\n".join(parts)


@pytest.mark.parametrize("name", ["agent_bill_week", "five_lives_week"])
def test_week_files_read_clean(name):
    data = week(name)
    ScenarioSpec.model_validate(data)
    clock = data["clock"]
    assert clock["director"]["max_sites"] == 40
    assert clock["director"].get("small_happenings", False) is False
    assert "Take money off" not in clock["director"]["setting_notes"]
    text = reader_text(data)
    assert ".example" not in text
    words = re.compile(
        r"\b(windows?|ticks?|beats?|cues?|directors?|scenarios?|simulat\w*|credits?)\b", re.I
    )
    mail = list(data["initial_mail"])
    for item in clock["timeline"]:
        mail += item["mail"]
    records = data["resources"]
    for entry in [*mail, *records]:
        assert not words.search(json.dumps(entry)), entry["id"]
    # Mail from outside says who it is from, in the way a mailbox shows it.
    for item in mail:
        if item.get("sender_owner_id") is None:
            assert re.match(r"From: [^\n]+\n\n", item["body"]), item["id"]


def test_income_lands_from_the_timeline_once():
    timeline = week("five_lives_week")["clock"]["timeline"]
    money = {item["id"]: (item["at"], item["balance"]) for item in timeline if item.get("balance")}
    assert set(money) == {"thu-pension", "fri-takings"}
    assert money["thu-pension"][0] == "Thu 09:15"
    assert money["thu-pension"][1] == [
        {"owner_id": "ruth", "delta": 410, "memo": "Pension payment"}
    ]
    assert money["fri-takings"][0] == "Fri 07:05"
    assert money["fri-takings"][1][0]["owner_id"] == "callum"
    assert money["fri-takings"][1][0]["delta"] == 600
    notes = week("five_lives_week")["clock"]["director"]["setting_notes"]
    assert "land by themselves, so never add them" in notes
    assert "+$410" not in notes
    assert not any(item.get("balance") for item in week("agent_bill_week")["clock"]["timeline"])


async def test_ruth_and_callum_are_paid_once_on_a_scripted_week(app, pool, test_database_url):
    spec = ScenarioSpec.model_validate_json((ROOT / "scenarios/five_lives_week.json").read_text())
    owners = [owner.id for owner in spec.owners]
    actors = {owner: ScriptedOwner([]) for owner in owners}
    models = {owner: ScriptModel(resting_status="responded") for owner in owners}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await Director(pool).control(created["run_id"], "start")
        # Up to Friday 07:10.
        await engine.advance(created["run_id"], max_sim_minutes=4 * 1440 + 70)
    async with pool.connection() as conn:
        balances = await (
            await conn.execute(
                "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s",
                (UUID(created["run_id"]),),
            )
        ).fetchall()
        ledger = await (
            await conn.execute(
                "SELECT owner_id,delta,memo FROM wallet_ledger WHERE run_id=%s ORDER BY owner_id",
                (UUID(created["run_id"]),),
            )
        ).fetchall()
    money = {row["owner_id"]: row["balance"] for row in balances}
    assert money["ruth"] == 640 + 410
    assert money["callum"] == 1480 + 600
    assert [(row["owner_id"], row["delta"]) for row in ledger] == [("callum", 600), ("ruth", 410)]


def test_dan_knows_how_the_garage_money_reaches_him():
    data = week("agent_bill_week")
    dan = next(owner for owner in data["owners"] if owner["id"] == "dan")
    rule = next(text for text in dan["acceptance_rules"] if "Mrs Okafor's garage" in text)
    for words in ("Wednesday", "next_action", "seeks", "15 cash", "when the job ends"):
        assert words in rule
    cue = next(i for i in data["clock"]["timeline"] if i["id"] == "wed-plan-dan")["cue"]
    assert "15 cash" in cue and "when it's done" in cue
    notes = data["clock"]["director"]["setting_notes"]
    assert "one balance line" in notes and "nothing before Wednesday morning" in notes
    # The bike is how he gets to work, so no fare comes from nowhere.
    assert "Dan rides his bike" in notes


def test_mei_still_says_no():
    data = week("agent_bill_week")
    mei = next(owner for owner in data["owners"] if owner["id"] == "mei")
    assert mei["max_spend"] == 0
    rules = " ".join(mei["acceptance_rules"])
    for words in ("the answer is no", "You do not change your mind", "the same no"):
        assert words in rules
    notes = data["clock"]["director"]["setting_notes"]
    assert "Mei never gives or lends money for Dan" in notes
    # The repeat guard lets her say it again, in her own rule's words.
    view = owner_view(
        ("owner", "No, I don't lend money. Tell him I'm sorry."),
        ("assistant", "He says it's only 20 and he'll pay it back on Friday."),
    )
    assert not _repeats_owner(view, said("No. Same no, I don't lend money. Tell him I'm sorry."))
