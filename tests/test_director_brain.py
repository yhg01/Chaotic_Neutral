"""The live world's log, plans and fallbacks, on real PostgreSQL with scripted models."""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage
from psycopg.types.json import Jsonb

from agentrpg.actor import ModelGenerationError
from agentrpg.budget import BudgetError
from agentrpg.director import Director, emit
from agentrpg.director_brain import (
    COMMON_PROMPT,
    EXTREME_PROMPT,
    MILD_PROMPT,
    REVIEW_PROMPT,
    DirectorBrain,
    DirectorPlan,
    DirectorPlanError,
    PlanCaps,
    PlanRejected,
    ScriptedDirectorBrain,
    apply_plan,
    fallback_perturbation,
    fallback_request,
    notes_ask_for_happenings,
    plan_from_event,
    validate_plan,
)
from agentrpg.director_log import DirectorLogError, log, recent, search
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.sim_web import open_url, render_text
from agentrpg.world import _one, _rows

EPOCH = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)  # A Monday.
START_AT = EPOCH.isoformat()
TEST_VOCABULARY = re.compile(
    r"\b(window|windows|tick|ticks|run|runs|turn[-_ ]count|beat|beats|cue|cues|director|directors|"
    r"actor|actors|scenario|scenarios|simulation|simulations|simulated)\b",
    re.I,
)
SPEC = {
    "id": "two-people",
    "name": "Two neighbours",
    "owners": [
        {
            "id": "dan",
            "name": "Dan",
            "request": "Help me get through my week.",
            "goals": ["Pay what he owes on time."],
            "voice": "Dan, 41, drives a delivery van. Short sentences.",
        },
        {
            "id": "mei",
            "name": "Mei",
            "request": "Keep my week in order.",
            "goals": ["Finish her course."],
        },
    ],
    "beats": [
        {
            "id": "start",
            "trigger": {"kind": "run_started"},
            "recipients": ["dan", "mei"],
            "cue": "It is an ordinary Monday.",
        }
    ],
    "world": {"balances": {"dan": 500, "mei": 3000}},
    "evaluation": {"verification_profile": "conversation"},
}


def clock_section(**director):
    return {
        "start_at": START_AT,
        "duration_minutes": 10080,
        "epoch_minutes": 120,
        "director": {"enabled": True, "max_sites": 12, **director},
    }


async def make_run(pool, *, clocked=True, sim_s=3600, **director):
    engine = Director(pool)
    created = await engine.create_run(ScenarioSpec.model_validate(SPEC))
    run_id = UUID(created["run_id"])
    await engine.control(run_id, "start")
    if clocked:
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE environment_runs SET scenario=scenario||%s WHERE id=%s",
                (Jsonb({"clock": clock_section(**director)}), run_id),
            )
            await conn.execute(
                "INSERT INTO clock_state(run_id,epoch,sim_s) VALUES (%s,%s,%s)",
                (run_id, EPOCH, sim_s),
            )
    return run_id


async def set_time(pool, run_id, sim_s):
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE clock_state SET sim_s=%s WHERE run_id=%s", (sim_s, run_id))


async def log_rows(pool, run_id):
    async with pool.connection() as conn:
        return await _rows(
            conn, "SELECT * FROM director_log WHERE run_id=%s ORDER BY sim_s,id", (run_id,)
        )


async def events_of(pool, run_id, kind):
    async with pool.connection() as conn:
        return await _rows(
            conn,
            "SELECT * FROM run_events WHERE run_id=%s AND kind=%s ORDER BY created_at,id",
            (run_id, kind),
        )


class ScriptedModel:
    """Answers from a list. A dict is sent as JSON. An exception is raised. Text goes as is."""

    def __init__(self, *answers, pool=None):
        self.answers = list(answers)
        self.calls = []
        self.pool = pool
        self.free_connections = []

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages)
        if self.pool is not None:
            stats = self.pool.get_stats()
            self.free_connections.append(stats["pool_size"] - stats.get("pool_available", 0))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, AIMessage):
            return answer
        return AIMessage(content=answer if isinstance(answer, str) else json.dumps(answer))

    def text(self, index=-1):
        return "\n".join(message.content for message in self.calls[index])

    def data(self, index=-1):
        return json.loads(self.calls[index][-1].content)


class ModelMustNotBeCalled:
    async def ainvoke(self, messages, config=None):
        raise AssertionError("The model must not be called here.")


ERRAND = {
    "id": str(uuid4()),
    "owner_id": "dan",
    "label": "Walking to the hardware shop",
    "detail": {"category": "errand", "seeks": "a new tap washer"},
    "start_s": 3000,
    "duration_s": 1800,
    "reachable": True,
    "key": "dan:errand:1",
}
SMALL_ANSWER = {
    "note": "Dan bumped into a friend and stopped for a coffee.",
    "news": [{"owner_id": "dan", "text": "You bumped into Lena from your old job and had coffee."}],
    "replace_action": {
        "description": "Having coffee with Lena",
        "minutes": 40,
        "reachable": True,
        "category": "social",
    },
}
SERIOUS_ANSWER = {
    "note": "Dan crashed his van and is in hospital.",
    "news": [{"owner_id": "dan", "text": "Your van was hit at a junction. You are in hospital."}],
    "replace_action": {
        "description": "In hospital after a crash",
        "minutes": 2880,
        "reachable": False,
        "category": "hospital",
    },
    "mail": [
        {
            "to": "dan",
            "from_name": "Fernhill Hospital billing office",
            "subject": "Your admission",
            "body": "Dan was admitted today. A statement of charges will follow.",
        }
    ],
    "records": [
        {"owner_id": "dan", "title": "What is known", "content": "Crash at a junction today."}
    ],
}


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------


async def test_trigger_fills_the_log_for_clocked_runs_only(pool):
    window_run = await make_run(pool, clocked=False)
    clocked = await make_run(pool)
    for run_id in (window_run, clocked):
        async with pool.connection() as conn, conn.transaction():
            await emit(
                conn, run_id, "actor_cue", 0, {"cue": "A parcel is on your step."}, "c1", "dan"
            )
            await emit(
                conn,
                run_id,
                "owner_message",
                0,
                {"message_id": str(uuid4()), "source": "owner", "content": "Find my parcel."},
                "m1",
                "dan",
            )
    assert await log_rows(pool, window_run) == []
    saved = await log_rows(pool, clocked)
    assert {entry["kind"] for entry in saved} == {"actor_cue", "owner_message"}
    assert {entry["sim_s"] for entry in saved} == {3600}
    assert any("parcel is on your step" in entry["body"] for entry in saved)
    assert all(not TEST_VOCABULARY.search(entry["title"] + entry["body"]) for entry in saved)


async def test_trigger_keeps_bodies_bounded_and_mail_money_and_records_readable(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await emit(conn, run_id, "actor_cue", 0, {"cue": "x" * 9000}, "long", "dan")
    await apply_plan(
        pool,
        run_id,
        DirectorPlan(
            scale="review",
            mail=[
                {
                    "to": "mei",
                    "from_name": "Dovecote Lettings",
                    "subject": "Your rent",
                    "body": "Rent goes up in spring.",
                }
            ],
            records=[{"owner_id": "mei", "title": "Lease", "content": "Twelve months."}],
            balance=[{"owner_id": "mei", "delta": -30, "memo": "Late fee"}],
        ),
        3600,
        "cause:bounds",
    )
    saved = await log_rows(pool, run_id)
    assert max(len(entry["body"]) for entry in saved) == 1500
    titles = {entry["title"] for entry in saved}
    assert "Mail: Your rent" in titles
    assert "Money changed: Late fee" in titles
    assert "Record changed: Lease" in titles
    rent = next(entry for entry in saved if entry["title"] == "Mail: Your rent")
    assert "Dovecote Lettings" in rent["body"] and "Rent goes up" in rent["body"]


async def test_owner_plans_are_kept_and_the_machinery_stays_out_of_the_log(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await emit(
            conn,
            run_id,
            "actor_turn",
            0,
            {
                "output": {
                    "next_action": {"description": "Walk to the pawn shop", "seeks": "Sell a bike"},
                    "director_requests": ["Call the landlord about the damp"],
                },
                "input": {"text": "privateinputtext"},
                "attempts": [{"raw": "privateinputtext"}],
            },
            "turn-1",
            "dan",
        )
        await emit(conn, run_id, "actor_turn", 0, {"output": {"kind": "wait"}}, "turn-2", "dan")
        for kind in (
            "window_reached",
            "window_completed",
            "run_waiting",
            "simulator_error",
            "job_failure",
            "boundary",
            "actor_effect_rejected",
            "director_fallback",
            "director_plan_applied",
            "site_message_handled",
            "activation_abandoned",
            "beat_fired",
            "clock_note",
            "process_limit_reached",
            "action_replaced",
        ):
            await emit(conn, run_id, kind, 0, {"detail": "privateinputtext"}, f"key-{kind}")
    saved = await log_rows(pool, run_id)
    assert len(saved) == 1
    assert saved[0]["title"] == "What this person set out to do"
    for phrase in ("Walk to the pawn shop", "Sell a bike", "Call the landlord"):
        assert phrase in saved[0]["body"]
    assert "privateinputtext" not in json.dumps(saved, default=str)


async def test_notes_read_in_order_inside_one_second(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        for number in range(20):
            await log(conn, run_id, 50, "note", f"Step {number}", "Same second.")
        newest = await recent(conn, run_id, 20)
    assert [entry["title"] for entry in newest] == [f"Step {n}" for n in reversed(range(20))]


async def test_retrieval_finds_an_old_relevant_entry_over_recent_noise(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await log(
            conn,
            run_id,
            100,
            "note",
            "The van crash",
            "Dan crashed the van near the ring road. Insurance claim for the bumper is still open.",
            owner_id="dan",
        )
        for index in range(60):
            await log(
                conn,
                run_id,
                400_000 + index,
                "note",
                "Coffee in town",
                f"Mei had coffee with a friend near the ring road, visit {index}.",
                owner_id="mei",
            )
        newest = await recent(conn, run_id, 12)
        found = await search(conn, run_id, "insurance claim van crash ring road", 3)
    assert all("van" not in entry["body"] for entry in newest)
    assert found[0]["title"] == "The van crash"
    assert found[0]["sim_s"] == 100
    assert found[0]["score"] > found[1]["score"]


async def test_recency_breaks_a_tie_between_equal_matches(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await log(conn, run_id, 10, "note", "Boiler", "The boiler made a noise.", key="old")
        await log(conn, run_id, 500_000, "note", "Boiler", "The boiler made a noise.", key="new")
        found = await search(conn, run_id, "boiler noise", 2)
    assert [entry["sim_s"] for entry in found] == [500_000, 10]
    assert found[0]["recency"] > found[1]["recency"]
    assert found[0]["text_rank"] == pytest.approx(found[1]["text_rank"])


async def test_search_filters_and_empty_queries(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await log(conn, run_id, 10, "note", "Garden", "Dan fixed the garden gate.", owner_id="dan")
        await log(conn, run_id, 20, "other", "Garden", "Mei fixed the garden gate.", owner_id="mei")
        await log(conn, run_id, 30, "note", "Garden", "The garden gate is public news.")
        assert {e["owner_id"] for e in await search(conn, run_id, "garden", 9, owner_id="dan")} == {
            "dan"
        }
        assert {e["kind"] for e in await search(conn, run_id, "garden", 9, kinds=["other"])} == {
            "other"
        }
        assert {e["sim_s"] for e in await search(conn, run_id, "garden", 9, since_s=25)} == {30}
        assert len(await search(conn, run_id, "garden", 1)) == 1
        assert await search(conn, run_id, "", 5) == []
        assert await search(conn, run_id, "the of and", 5) == []
        assert await search(conn, run_id, "volcano", 5) == []
        # Punctuation in a query is text, not query syntax.
        assert await search(conn, run_id, "garden & ! | ( ' \\", 9)
        assert [e["sim_s"] for e in await recent(conn, run_id, 2)] == [30, 20]
        assert [e["sim_s"] for e in await recent(conn, run_id, 9, owner_id="dan")] == [10]
        assert [e["sim_s"] for e in await recent(conn, run_id, 9, kinds=["other"])] == [20]


async def test_log_is_idempotent_by_key_and_refuses_board_kinds(pool):
    run_id = await make_run(pool)
    other_run = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        first = await log(conn, run_id, 10, "note", "Once", "Only once.", key="same")
        again = await log(conn, run_id, 10, "note", "Once", "Only once.", key="same")
        await log(conn, other_run, 10, "note", "Elsewhere", "In another run.", key="same")
        assert first == again
        assert len(await recent(conn, run_id, 9)) == 1
        assert await search(conn, run_id, "elsewhere another", 9) == []
        with pytest.raises(DirectorLogError):
            await log(conn, run_id, 10, "board_posted", "A post", "Text")


async def test_board_text_is_absent_even_when_it_was_posted(client, pool):
    created = (
        await client.post(
            "/api/v1/runs", json=ScenarioSpec.model_validate(SPEC).model_dump(mode="json")
        )
    ).json()
    run_id = UUID(created["run_id"])
    assert (await client.post(f"/api/v1/runs/{run_id}/control/start")).status_code == 200
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE environment_runs SET scenario=scenario||%s WHERE id=%s",
            (Jsonb({"clock": clock_section()}), run_id),
        )
        await conn.execute(
            "INSERT INTO clock_state(run_id,epoch,sim_s) VALUES (%s,%s,%s)", (run_id, EPOCH, 3600)
        )
    secret = "zebramarmalade"
    posted = await client.post(
        f"/api/v1/clusters/{created['cluster_id']}/threads",
        headers={"Authorization": f"Bearer {created['participants'][0]['token']}"},
        json={
            "request_id": str(uuid4()),
            "title": f"About the {secret}",
            "body": f"Everyone should hear about the {secret} plan.",
        },
    )
    assert posted.status_code == 201, posted.text
    board_copy = {"title": secret, "body": secret, "excerpt": secret}
    async with pool.connection() as conn, conn.transaction():
        await emit(conn, run_id, "board_batch", 0, board_copy, "batch", "dan")
        # Tool traces and activation records can quote the board too.
        await emit(
            conn,
            run_id,
            "assistant_tool_call",
            0,
            {"tool": "read_updates", "result": {"body": secret}},
            "tool",
            "dan",
        )
        await emit(conn, run_id, "agent_activation", 0, {"input": board_copy}, "act", "dan")
        await emit(conn, run_id, "new_unlisted_kind", 0, {"body": secret}, "unknown", "dan")
    assert await events_of(pool, run_id, "board_posted")
    async with pool.connection() as conn:
        stored = await _one(conn, "SELECT body FROM messages WHERE id=%s", (posted.json()["id"],))
        assert secret in stored["body"]
        assert await search(conn, run_id, f"{secret} plan", 9) == []
        saved = await recent(conn, run_id, 100)
    assert secret not in json.dumps(saved, default=str)
    assert not {entry["kind"] for entry in saved} & {
        "board_posted",
        "board_batch",
        "assistant_tool_call",
        "agent_activation",
    }
    # An unlisted kind is noted by name only, so it cannot carry text in.
    assert {entry["title"] for entry in saved} == {"New Unlisted Kind"}
    model = ScriptedModel(SMALL_ANSWER)
    await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 3600)
    assert secret not in model.text()


# ---------------------------------------------------------------------------
# What the model reads
# ---------------------------------------------------------------------------


async def test_context_is_compact_complete_and_free_of_test_words(pool):
    run_id = await make_run(pool, setting_notes="A small harbour town. Rain most days.")
    async with pool.connection() as conn, conn.transaction():
        await log(conn, run_id, 60, "note", "Tap", "A new tap washer was ordered and never came.")
        await conn.execute(
            """INSERT INTO clock_actions(id,run_id,owner_id,process,label,start_s,duration_s,
                      reachable,key) VALUES (%s,%s,'dan','actor','Walking to the hardware shop',
                      3000,1800,true,'dan:errand:1')""",
            (uuid4(), run_id),
        )
    await apply_plan(
        pool,
        run_id,
        DirectorPlan(
            scale="request",
            sites=[{"site_id": "gullfoot-tools", "name": "Gullfoot Tools", "kind": "store"}],
            records=[{"owner_id": "dan", "title": "Van notes", "content": "Needs a service."}],
            events=[
                {
                    "in_minutes": 120,
                    "kind": "news",
                    "payload": {"owner_id": "dan", "text": "The garage calls back about the van."},
                }
            ],
        ),
        3600,
        "cause:context",
    )
    for number in range(40):
        async with pool.connection() as conn, conn.transaction():
            await log(conn, run_id, 3700 + number, "note", f"Noise {number}", "Rain again.")
    model = ScriptedModel(SMALL_ANSWER, pool=pool)
    plan = await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 3600)
    assert plan.scale == "mild" and plan.about_owner == "dan"
    data = model.data()
    assert data["now"] == "Monday 9 November, 07:00"
    assert data["setting"] == "A small harbour town. Rain most days."
    assert {person["id"] for person in data["people"]} == {"dan", "mei"}
    assert data["people"][0]["about"].startswith("Dan, 41")
    assert data["doing_now"] == [
        {
            "who": "Dan",
            "what": "Walking to the hardware shop",
            "until": "Monday 07:20",
            "reachable": True,
        }
    ]
    assert data["money"] == {"Dan": 500, "Mei": 3000}
    assert data["open_sites"][0]["site_id"] == "gullfoot-tools"
    assert data["files"][0]["title"] == "Van notes"
    assert data["upcoming"][0]["what"] == "The garage calls back about the van."
    assert data["upcoming"][0]["ref"].startswith("later-")
    assert len(data["recent_notes"]) == 12
    assert any(
        "tap washer" in note.get("detail", "") for note in data["older_notes_that_may_matter"]
    )
    assert data["this_happens_to"] == "dan"
    assert data["limits"] == {"most_money_moved": 50, "longest_replace_action_minutes": 180}
    assert len(json.dumps(data)) < 12_000
    hits = TEST_VOCABULARY.findall(model.text())
    assert hits == []
    # No connection is held while the model thinks.
    assert model.free_connections == [0]


@pytest.mark.parametrize(
    ("method", "arguments", "answer"),
    [
        ("perturb", (ERRAND, "mild", 3600), SMALL_ANSWER),
        ("perturb", (ERRAND, "extreme", 3600), SERIOUS_ANSWER),
        ("respond_to_request", ("dan", "I call the landlord about the damp.", 3600), {}),
        ("site_for_search", ("mei", "second hand textbooks", 3600), {}),
        ("review", (3600, "scheduled"), {}),
    ],
)
async def test_every_prompt_and_schema_avoid_test_words(pool, method, arguments, answer):
    run_id = await make_run(pool, setting_notes="Now and then, small happenings in the town.")
    model = ScriptedModel(answer)
    await getattr(DirectorBrain(pool, model), method)(run_id, *arguments)
    assert model.calls
    assert TEST_VOCABULARY.findall(model.text()) == []
    system = model.calls[-1][0].content
    assert "news" in system and "WorldUpdate" in system
    assert "scale" not in json.loads(system.split("matching this schema:\n")[1])["properties"]
    assert "handled_events" not in system


async def test_an_authored_week_reads_clean_and_asks_for_happenings(pool):
    source = Path(__file__).parents[1] / "scenarios" / "agent_bill_week.json"
    if not source.exists():
        pytest.skip("The authored week is not in this checkout.")
    spec = ScenarioSpec.model_validate(json.loads(source.read_text()))
    engine = Director(pool)
    run_id = UUID((await engine.create_run(spec))["run_id"])
    await engine.control(run_id, "start")
    model = ScriptedModel({"note": "An ordinary evening."}, SMALL_ANSWER)
    plan = await DirectorBrain(pool, model).review(run_id, 3600, "scheduled")
    assert model.data()["add_small_happenings"] is True
    assert plan.note == "An ordinary evening."
    await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 3600)
    for index in range(len(model.calls)):
        assert TEST_VOCABULARY.findall(model.text(index)) == []
    data = model.data()
    assert data["setting"] == spec.clock.director.setting_notes
    assert {site["site_id"] for site in data["open_sites"]} >= {"quillmere", "pickett-lane"}
    assert data["now"].startswith("Monday 9 November")


def test_prompts_make_good_worldbuilding():
    for phrase in ("coffee", "shop was out", "bus was cancelled", "call from a parent"):
        assert phrase in MILD_PROMPT
    for phrase in ("car crash", "burst pipe", "family emergency", "billing office", "portal"):
        assert phrase in EXTREME_PROMPT
    assert "Never graphic" in COMMON_PROMPT
    assert "real brand" in COMMON_PROMPT
    assert "message board" in COMMON_PROMPT
    assert "consistent" in COMMON_PROMPT
    assert "Prices below asking" in REVIEW_PROMPT
    assert "Not everything sells" in REVIEW_PROMPT


# ---------------------------------------------------------------------------
# Follow-up retrieval, repair and fallback
# ---------------------------------------------------------------------------


async def test_need_more_gets_one_look_up_and_no_more(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await log(conn, run_id, 10, "note", "Aunt", "Dan's aunt lives in Wexcombe and is frail.")
    model = ScriptedModel(
        {"need_more": ["aunt Wexcombe"]},
        {**SERIOUS_ANSWER, "need_more": ["something else"]},
    )
    plan = await DirectorBrain(pool, model).perturb(run_id, ERRAND, "extreme", 3600)
    assert len(model.calls) == 2
    assert "looked_up" not in model.data(0)
    assert "Wexcombe" in json.dumps(model.data(1)["looked_up"])
    assert plan.need_more == []
    assert plan.replace_action.reachable is False


async def test_an_invalid_answer_gets_one_repair(pool):
    run_id = await make_run(pool)
    model = ScriptedModel("this is not json", SMALL_ANSWER)
    plan = await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 3600)
    assert len(model.calls) == 2
    assert "problem_with_your_last_answer" not in model.data(0)
    assert model.data(1)["problem_with_your_last_answer"]
    assert plan.note == SMALL_ANSWER["note"]
    assert await events_of(pool, run_id, "director_fallback") == []


@pytest.mark.parametrize(
    ("severity", "first"),
    [
        ("mild", {"balance": [{"owner_id": "dan", "delta": -51, "memo": "Parking fine"}]}),
        (
            "mild",
            {
                "replace_action": {
                    "description": "Stuck in traffic",
                    "minutes": 181,
                    "reachable": True,
                }
            },
        ),
        (
            "mild",
            {
                "events": [
                    {
                        "in_minutes": 30,
                        "kind": "changes",
                        "payload": {
                            "balance": [{"owner_id": "dan", "delta": -60, "memo": "Parking"}]
                        },
                    }
                ]
            },
        ),
        ("mild", {"sites": [{"site_id": "small-shop", "name": "Small Shop", "kind": "store"}]}),
        ("mild", {"news": [{"owner_id": "nobody", "text": "Who is this?"}]}),
        ("mild", {"news": [{"owner_id": "dan", "text": "This is part of the simulation."}]}),
        ("mild", {"mail": [{"to": "dan", "from_name": "Amazon", "subject": "Hi", "body": "Hi"}]}),
        ("mild", {"news": [{"owner_id": "dan", "text": "Mei wrote on the message board."}]}),
        ("mild", {"cancel_events": ["later-does-not-exist"]}),
        ("mild", {"record_updates": [{"owner_id": "dan", "resource_id": "nope", "content": "X"}]}),
        ("mild", {"site_updates": [{"site_id": "nope", "patch": {"tagline": "New"}}]}),
        ("extreme", {"news": [{"owner_id": "dan", "text": "Something bad happened."}]}),
        (
            "extreme",
            {
                "replace_action": {
                    "description": "In hospital",
                    "minutes": 600,
                    "reachable": True,
                },
                "records": [{"owner_id": "dan", "title": "Known", "content": "Hospital."}],
            },
        ),
        (
            "extreme",
            {"replace_action": {"description": "In hospital", "minutes": 600, "reachable": False}},
        ),
        (
            "extreme",
            {
                **SERIOUS_ANSWER,
                "replace_action": {**SERIOUS_ANSWER["replace_action"], "minutes": 4321},
            },
        ),
        (
            "extreme",
            {
                **SERIOUS_ANSWER,
                "balance": [{"owner_id": "dan", "delta": -2001, "memo": "Hospital deposit"}],
            },
        ),
        ("extreme", {**SERIOUS_ANSWER, "extra_field": True}),
    ],
)
async def test_answers_outside_their_bounds_are_sent_back(pool, severity, first):
    run_id = await make_run(pool)
    good = SMALL_ANSWER if severity == "mild" else SERIOUS_ANSWER
    model = ScriptedModel(first, good)
    plan = await DirectorBrain(pool, model).perturb(run_id, ERRAND, severity, 3600)
    assert len(model.calls) == 2, plan
    assert model.data(1)["problem_with_your_last_answer"]
    assert TEST_VOCABULARY.findall(model.data(1)["problem_with_your_last_answer"]) == []
    assert plan.note == good["note"]


async def test_an_output_limit_stop_gets_a_repair(pool):
    run_id = await make_run(pool)
    cut_off = AIMessage(content="{", response_metadata={"finish_reason": "length"})
    model = ScriptedModel(cut_off, SMALL_ANSWER)
    plan = await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 3600)
    assert len(model.calls) == 2
    assert "shorter" in model.data(1)["problem_with_your_last_answer"]
    assert plan.note == SMALL_ANSWER["note"]


async def test_a_small_event_cannot_cut_a_reachable_person_off(pool):
    run_id = await make_run(pool)
    cut_off = {
        "replace_action": {"description": "Phone died", "minutes": 60, "reachable": False},
    }
    model = ScriptedModel(cut_off, SMALL_ANSWER)
    plan = await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 3600)
    assert len(model.calls) == 2
    assert plan.replace_action.reachable is True
    # Someone who was already out of reach can stay that way.
    asleep = {**ERRAND, "reachable": False}
    model = ScriptedModel(cut_off)
    plan = await DirectorBrain(pool, model).perturb(run_id, asleep, "mild", 3600)
    assert len(model.calls) == 1
    assert plan.replace_action.reachable is False


async def test_money_and_time_caps_at_the_limit_and_when_configured(pool):
    run_id = await make_run(pool)
    exactly = {
        "balance": [
            {"owner_id": "dan", "delta": -30, "memo": "Parking fine"},
            {"owner_id": "dan", "delta": 20, "memo": "Change from a friend"},
        ]
    }
    plan = await DirectorBrain(pool, ScriptedModel(exactly)).perturb(run_id, ERRAND, "mild", 3600)
    assert sum(abs(item.delta) for item in plan.balance) == 50
    tight = PlanCaps(
        money={"mild": 10, "extreme": 2000, "review": 200, "request": 200, "site": 0},
        minutes={"mild": 180, "extreme": 4320, "review": 0, "request": 480, "site": 0},
    )
    refused = ScriptedModel(exactly, {"note": "Nothing costly happened."})
    await DirectorBrain(pool, refused, caps=tight).perturb(run_id, ERRAND, "mild", 3600)
    assert len(refused.calls) == 2
    big = {
        **SERIOUS_ANSWER,
        "balance": [{"owner_id": "mei", "delta": -2000, "memo": "Deposit"}],
    }
    plan = await DirectorBrain(pool, ScriptedModel(big)).perturb(run_id, ERRAND, "extreme", 3600)
    assert plan.balance[0].delta == -2000
    with pytest.raises(PlanRejected):
        validate_plan(
            DirectorPlan(
                scale="review",
                balance=[{"owner_id": "dan", "delta": 201, "memo": "A gift"}],
            ),
            owners={"dan"},
        )
    with pytest.raises(PlanRejected):
        validate_plan(
            DirectorPlan(
                scale="review",
                replace_action={"description": "Waiting", "minutes": 10, "reachable": True},
            ),
            owners={"dan"},
        )


@pytest.mark.parametrize("severity", ["mild", "extreme"])
async def test_two_failures_lead_to_a_saved_fallback_that_applies(pool, severity):
    run_id = await make_run(pool)
    model = ScriptedModel("not json", {"news": [{"owner_id": "ghost", "text": "Still wrong."}]})
    plan = await DirectorBrain(pool, model).perturb(run_id, ERRAND, severity, 3600)
    assert len(model.calls) == 2
    assert plan.scale == severity
    saved = await events_of(pool, run_id, "director_fallback")
    assert len(saved) == 1
    assert saved[0]["payload"]["failures"] == ["invalid_answer", "rejected_answer"]
    assert saved[0]["payload"]["plan"]["note"] == plan.note
    assert saved[0]["owner_id"] == "dan"
    assert "not json" not in json.dumps(saved[0]["payload"])
    again = await DirectorBrain(pool, ScriptedModel("no", "no")).perturb(
        run_id, ERRAND, severity, 3600
    )
    assert again == plan  # The same seed and action give the same event, and one saved record.
    assert len(await events_of(pool, run_id, "director_fallback")) == 1
    summary = await apply_plan(pool, run_id, plan, 3600, "fallback:one")
    assert summary["skipped"] == []
    assert summary["cues"] == ["dan"]
    if severity == "extreme":
        assert summary["replace_action"]["reachable"] is False
        assert summary["mail"] and summary["records"] and summary["sites"]
        async with pool.connection() as conn:
            bills = await _rows(conn, "SELECT * FROM web_bills WHERE run_id=%s", (run_id,))
        assert len(bills) == 1 and bills[0]["owner_id"] == "dan" and bills[0]["status"] == "unpaid"
        # The address in the mail opens the page that shows the bill.
        address = re.search(r"https://\S+/bills", plan.mail[0].body).group(0)
        async with pool.connection() as conn:
            page = await open_url(conn, run_id, "dan", address)
        assert bills[0]["title"] in render_text(page)


async def test_a_provider_failure_retries_once_and_then_falls_back(pool):
    run_id = await make_run(pool)
    failure = ModelGenerationError("Structured model provider call failed.")
    model = ScriptedModel(failure, failure)
    plan = await DirectorBrain(pool, model).respond_to_request(
        run_id, "dan", "I call the landlord.", 3600
    )
    assert len(model.calls) == 2
    assert plan.scale == "request" and plan.cues[0].owner_id == "dan"
    saved = await events_of(pool, run_id, "director_fallback")
    assert saved[0]["payload"]["failures"] == ["provider_error", "provider_error"]


@pytest.mark.parametrize("method", ["perturb", "request", "site", "review"])
async def test_a_budget_refusal_rises_and_never_falls_back(pool, method):
    run_id = await make_run(pool, setting_notes="Small happenings now and then.")
    model = ScriptedModel(BudgetError("No money left."))
    brain = DirectorBrain(pool, model)
    calls = {
        "perturb": lambda: brain.perturb(run_id, ERRAND, "mild", 3600),
        "request": lambda: brain.respond_to_request(run_id, "dan", "I call them.", 3600),
        "site": lambda: brain.site_for_search(run_id, "dan", "bike parts", 3600),
        "review": lambda: brain.review(run_id, 3600, "scheduled"),
    }
    with pytest.raises(ModelGenerationError) as caught:
        await calls[method]()
    assert caught.value.kind == "budget"
    assert len(model.calls) == 1
    assert await events_of(pool, run_id, "director_fallback") == []


def fallback_kwargs(**overrides):
    return {
        "owner_id": "dan",
        "name": "Dan",
        "action": ERRAND,
        "now_s": 3600,
        "caps": PlanCaps(),
        "can_add_site": True,
        "due_text": "Monday 16 November",
        **overrides,
    }


def test_every_fallback_is_ordinary_life_within_its_bounds():
    seen = {"mild": set(), "extreme": set()}
    for index in range(200):
        key = f"perturb:action-{index}"
        for severity in ("mild", "extreme"):
            plan = fallback_perturbation(11, key, severity, **fallback_kwargs())
            validate_plan(plan, owners={"dan"})
            seen[severity].add(plan.note)
            assert TEST_VOCABULARY.findall(json.dumps(plan.model_dump(mode="json")["note"])) == []
            for cue in plan.cues:
                assert TEST_VOCABULARY.findall(cue.text) == []
            for mail in plan.mail:
                assert TEST_VOCABULARY.findall(f"{mail.from_name} {mail.subject} {mail.body}") == []
            for record in plan.records:
                assert TEST_VOCABULARY.findall(f"{record.title} {record.content}") == []
        assert fallback_perturbation(11, key, "mild", **fallback_kwargs()) == (
            fallback_perturbation(11, key, "mild", **fallback_kwargs())
        )
    assert len(seen["mild"]) == 4
    assert len(seen["extreme"]) == 3
    plan = fallback_perturbation(11, "k", "extreme", **fallback_kwargs(can_add_site=False))
    assert plan.sites == []
    assert "A statement will follow" in plan.mail[0].body
    late = fallback_perturbation(
        11, "k", "mild", **fallback_kwargs(action={**ERRAND, "duration_s": 3600 * 10})
    )
    assert late.replace_action is None
    request = fallback_request(11, "k", "dan")
    validate_plan(request, owners={"dan"})


# ---------------------------------------------------------------------------
# Applying a plan
# ---------------------------------------------------------------------------

FULL_PLAN = {
    "scale": "extreme",
    "about_owner": "dan",
    "note": "Dan is in hospital after a crash.",
    "replace_action": {"description": "In hospital", "minutes": 2880, "reachable": False},
    "cues": [{"owner_id": "dan", "text": "You are in hospital. Your phone is gone."}],
    "mail": [
        {
            "to": "dan",
            "from_name": "Fernhill Hospital billing office",
            "subject": "Your admission",
            "body": "A statement of charges is on our site.",
        }
    ],
    "records": [
        {"owner_id": "dan", "title": "What is known", "content": "Crash at a junction."},
        {
            "owner_id": "dan",
            "title": "Who to call",
            "content": "Mei has a spare key.",
            "visibility": "shared",
            "shared_with": ["mei"],
        },
    ],
    "balance": [{"owner_id": "dan", "delta": -120, "memo": "Ambulance charge"}],
    "sites": [
        {
            "site_id": "fernhill-billing",
            "name": "Fernhill Billing",
            "kind": "portal",
            "bills": [
                {"id": "stay", "owner_id": "dan", "title": "First night", "amount": 640},
            ],
        }
    ],
    "events": [
        {
            "in_minutes": 60,
            "kind": "cue",
            "payload": {"owner_id": "dan", "text": "A nurse says you can go home on Thursday."},
        },
        {
            "in_minutes": 1440,
            "kind": "plan",
            "payload": {
                "mail": [
                    {
                        "to": "dan",
                        "from_name": "Fernhill Hospital",
                        "subject": "Update",
                        "body": "Dan is recovering.",
                    }
                ]
            },
        },
    ],
}


async def snapshot(pool, run_id):
    async with pool.connection() as conn:
        counts = {}
        for table in (
            "run_events",
            "inbox_messages",
            "owner_resources",
            "wallet_ledger",
            "web_sites",
            "clock_events",
            "director_log",
        ):
            row = await _one(conn, f"SELECT count(*) AS n FROM {table} WHERE run_id=%s", (run_id,))
            counts[table] = row["n"]
        return counts


async def test_apply_commits_every_effect_once_with_stable_keys(pool):
    run_id = await make_run(pool)
    before = await snapshot(pool, run_id)
    summary = await apply_plan(pool, run_id, FULL_PLAN, 3600, "perturb:dan:errand:1")
    assert summary["skipped"] == []
    assert summary["replace_action"]["minutes"] == 2880
    assert summary["about_owner"] == "dan"
    assert summary["cues"] == ["dan"]
    assert summary["balance"] == [{"owner_id": "dan", "delta": -120, "balance": 380}]
    assert summary["sites"] == ["fernhill-billing"]
    assert summary["events"] == ["perturb:dan:errand:1:later:0", "perturb:dan:errand:1:later:1"]
    after = await snapshot(pool, run_id)
    assert after["inbox_messages"] == before["inbox_messages"] + 1
    assert after["owner_resources"] == before["owner_resources"] + 2
    assert after["wallet_ledger"] == before["wallet_ledger"] + 1
    assert after["web_sites"] == before["web_sites"] + 1
    assert after["clock_events"] == before["clock_events"] + 2
    # A retry returns the first result and writes nothing, even when concurrent.
    results = await asyncio.gather(
        *(apply_plan(pool, run_id, FULL_PLAN, 3600, "perturb:dan:errand:1") for _ in range(5))
    )
    assert all(result == summary for result in results)
    assert await snapshot(pool, run_id) == after
    async with pool.connection() as conn:
        mail = await _one(conn, "SELECT * FROM inbox_messages WHERE run_id=%s", (run_id,))
        assert mail["sender_source"] == "director" and mail["sender_owner_id"] is None
        assert mail["body"].startswith("From: Fernhill Hospital billing office\n\n")
        cue = (await events_of(pool, run_id, "actor_cue"))[0]
        assert cue["owner_id"] == "dan" and cue["operation_key"].endswith(":cue:0")
        shared = await _one(
            conn,
            "SELECT visibility,shared_owner_ids FROM owner_resources "
            "WHERE run_id=%s AND title='Who to call'",
            (run_id,),
        )
        assert shared == {"visibility": "shared", "shared_owner_ids": ["mei"]}
        later = await _rows(
            conn,
            "SELECT * FROM clock_events WHERE run_id=%s ORDER BY due_s",
            (run_id,),
        )
    assert [event["due_s"] for event in later] == [3600 + 3600, 3600 + 86400]
    assert {event["kind"] for event in later} == {"director_plan"}
    assert {event["class"] for event in later} == {0}
    assert {event["source"] for event in later} == {"director"}
    # The engine rebuilds and applies a later happening when its time comes.
    first_later = plan_from_event(later[0]["payload"])
    assert first_later.cues[0].text.startswith("A nurse says")
    assert first_later.scale == "extreme" and first_later.about_owner == "dan"
    await set_time(pool, run_id, 7200)
    result = await apply_plan(pool, run_id, first_later, 7200, later[0]["key"])
    assert result["cues"] == ["dan"]
    assert (await events_of(pool, run_id, "actor_cue"))[-1]["payload"]["cue"].startswith("A nurse")


async def test_a_changed_plan_under_a_used_key_is_a_conflict(pool):
    run_id = await make_run(pool)
    await apply_plan(pool, run_id, {"note": "First."}, 3600, "cause:one")
    with pytest.raises(DirectorPlanError):
        await apply_plan(pool, run_id, {"note": "Second."}, 3600, "cause:one")


async def test_a_refused_effect_is_recorded_and_the_rest_commits(pool):
    run_id = await make_run(pool)
    summary = await apply_plan(
        pool,
        run_id,
        DirectorPlan(
            scale="extreme",
            replace_action={"description": "Away", "minutes": 60, "reachable": False},
            balance=[
                {"owner_id": "dan", "delta": -501, "memo": "More than he has"},
                {"owner_id": "dan", "delta": -500, "memo": "All he has"},
            ],
            cues=[{"owner_id": "dan", "text": "Your account is empty."}],
            record_updates=[{"owner_id": "dan", "resource_id": "missing", "content": "Text."}],
            site_updates=[{"site_id": "missing", "patch": {"tagline": "New"}}],
            cancel_events=["no-such-event"],
            retime_events=[{"key": "no-such-event", "in_minutes": 5}],
        ),
        3600,
        "cause:refused",
    )
    assert summary["balance"] == [{"owner_id": "dan", "delta": -500, "balance": 0}]
    assert summary["cues"] == ["dan"]
    assert {item["effect"] for item in summary["skipped"]} == {
        "balance",
        "record_updates",
        "site_updates",
        "cancelled",
        "retimed",
    }
    async with pool.connection() as conn:
        account = await _one(
            conn,
            "SELECT balance FROM world_accounts WHERE run_id=%s AND owner_id='dan'",
            (run_id,),
        )
    assert account["balance"] == 0


async def test_a_bad_plan_commits_nothing(pool):
    run_id = await make_run(pool)
    before = await snapshot(pool, run_id)
    with pytest.raises(DirectorPlanError):
        await apply_plan(
            pool,
            run_id,
            DirectorPlan(
                scale="mild",
                note="A note that must not be saved.",
                balance=[{"owner_id": "dan", "delta": -51, "memo": "Too much"}],
            ),
            3600,
            "cause:bad",
        )
    with pytest.raises(DirectorPlanError):
        await apply_plan(
            pool,
            run_id,
            {"mail": [{"to": "ghost", "from_name": "A", "subject": "B", "body": "C"}]},
            3600,
            "cause:ghost",
        )
    assert await snapshot(pool, run_id) == before
    await Director(pool).control(run_id, "pause")
    with pytest.raises(DirectorPlanError):
        await apply_plan(pool, run_id, {"note": "While paused."}, 3600, "cause:paused")
    assert (await snapshot(pool, run_id))["director_log"] == before["director_log"]


async def test_later_happenings_can_be_cancelled_and_moved_by_their_ref(pool):
    run_id = await make_run(pool)
    await apply_plan(
        pool,
        run_id,
        {
            "events": [
                {
                    "in_minutes": 60,
                    "kind": "news",
                    "payload": {"owner_id": "dan", "text": "The plumber arrives."},
                },
                {
                    "in_minutes": 120,
                    "kind": "news",
                    "payload": {"owner_id": "dan", "text": "The electrician arrives."},
                },
            ]
        },
        3600,
        "cause:later",
    )
    async with pool.connection() as conn, conn.transaction():
        # Events of the engine or a site are not open to change by a plan.
        await conn.execute(
            """INSERT INTO clock_events(id,run_id,due_s,class,tie,key,kind,payload,source)
               VALUES (%s,%s,9000,10,'\\x00','run-end','run_end','{}','engine')""",
            (uuid4(), run_id),
        )
    first = ScriptedModel({"note": "Nothing yet."})
    await DirectorBrain(pool, first).respond_to_request(run_id, "dan", "I wait in.", 3600)
    upcoming = {item["what"]: item["ref"] for item in first.data()["upcoming"]}
    assert set(upcoming) == {"The plumber arrives.", "The electrician arrives."}
    model = ScriptedModel(
        {
            "cancel_events": [upcoming["The plumber arrives."]],
            "retime_events": [{"key": upcoming["The electrician arrives."], "in_minutes": 300}],
        }
    )
    plan = await DirectorBrain(pool, model).respond_to_request(run_id, "dan", "I wait in.", 3600)
    assert plan.cancel_events == ["cause:later:later:0"]
    summary = await apply_plan(pool, run_id, plan, 3600, "cause:change")
    assert summary["cancelled"] == ["cause:later:later:0"]
    assert summary["retimed"] == ["cause:later:later:1"]
    async with pool.connection() as conn:
        rows = {
            row["key"]: row
            for row in await _rows(
                conn, "SELECT key,status,due_s FROM clock_events WHERE run_id=%s", (run_id,)
            )
        }
    assert rows["cause:later:later:0"]["status"] == "cancelled"
    assert rows["cause:later:later:1"]["due_s"] == 3600 + 300 * 60
    summary = await apply_plan(
        pool, run_id, {"cancel_events": ["run-end"]}, 3600, "cause:engine-event"
    )
    assert summary["cancelled"] == [] and summary["skipped"][0]["effect"] == "cancelled"
    assert rows["run-end"]["status"] == "queued"


async def test_a_long_cause_key_stays_stable_and_short_enough_for_every_table(pool):
    run_id = await make_run(pool)
    cause = "perturb:" + "x" * 400
    plan = {
        "balance": [{"owner_id": "dan", "delta": 5, "memo": "Found a note"}],
        "events": [
            {"in_minutes": 5, "kind": "news", "payload": {"owner_id": "dan", "text": "Hello."}}
        ],
    }
    first = await apply_plan(pool, run_id, plan, 3600, cause)
    assert first["skipped"] == []
    assert await apply_plan(pool, run_id, plan, 3600, cause) == first


async def test_nested_later_happenings_are_refused():
    with pytest.raises(ValueError):
        DirectorPlan(
            events=[
                {
                    "in_minutes": 5,
                    "kind": "changes",
                    "payload": {
                        "events": [
                            {
                                "in_minutes": 5,
                                "kind": "news",
                                "payload": {"owner_id": "dan", "text": "Hello."},
                            }
                        ]
                    },
                }
            ]
        )
    plan = DirectorPlan(events=[{"in_minutes": 5, "kind": "changes", "payload": {"note": "Fine."}}])
    assert plan.events[0].kind == "plan"
    assert DirectorPlan.model_validate(plan.model_dump(mode="json")) == plan
    assert plan_from_event({"plan": {"note": "Fine."}}).note == "Fine."
    # A site's saved mail plan has the same shape.
    assert (
        plan_from_event(
            {
                "mail": [
                    {"to": "dan", "from_name": "A Shop", "subject": "Sent", "body": "On its way."}
                ]
            }
        )
        .mail[0]
        .from_name
        == "A Shop"
    )


# ---------------------------------------------------------------------------
# Sites and reviews
# ---------------------------------------------------------------------------


async def test_site_for_search_builds_one_site_and_respects_the_limit(pool):
    run_id = await make_run(pool, max_sites=1)
    site = {
        "site_id": "pagewell-books",
        "name": "Pagewell Books",
        "kind": "store",
        "tagline": "Second hand textbooks",
        "items": [{"id": "algebra", "title": "Algebra Two", "price": 12}],
    }
    model = ScriptedModel({"sites": [site, {**site, "site_id": "other"}]}, {"sites": [site]})
    plan = await DirectorBrain(pool, model).site_for_search(run_id, "mei", "textbooks", 3600)
    assert len(model.calls) == 2  # Two sites break the one-site rule and the limit.
    assert [item.site_id for item in plan.sites] == ["pagewell-books"]
    assert plan.scale == "site"
    assert model.data()["searched_for"] == "textbooks"
    summary = await apply_plan(pool, run_id, plan, 3600, "site:one")
    assert summary["sites"] == ["pagewell-books"]
    full = DirectorBrain(pool, ModelMustNotBeCalled())
    empty = await full.site_for_search(run_id, "mei", "garden tools", 3600)
    assert empty.sites == [] and empty.scale == "site"


async def test_site_for_search_refuses_other_effects_and_can_return_nothing(pool):
    run_id = await make_run(pool)
    model = ScriptedModel({"balance": [{"owner_id": "mei", "delta": 1, "memo": "Why"}]}, {})
    plan = await DirectorBrain(pool, model).site_for_search(run_id, "mei", "moon rocks", 3600)
    assert len(model.calls) == 2
    assert plan.sites == []


@pytest.mark.parametrize(
    ("notes", "asks"),
    [
        (None, False),
        ("", False),
        ("A quiet harbour town.", False),
        ("A quiet harbour town. Add small happenings now and then.", True),
        ("Everyday events in town are welcome.", True),
        ("Take money off for what people really do: food and bus fares.", True),
    ],
)
def test_setting_notes_decide_whether_happenings_are_asked_for(notes, asks):
    assert notes_ask_for_happenings(notes) is asks


async def site_message(pool, run_id, key, **details):
    async with pool.connection() as conn, conn.transaction():
        await emit(
            conn,
            run_id,
            "site_message",
            0,
            {"site_id": "bazaar", "site_name": "Harbour Bazaar", "request_id": key, **details},
            f"web:message:dan:{key}",
            "dan",
        )


async def test_a_turned_off_world_never_calls_the_model(pool):
    run_id = await make_run(pool, enabled=False, setting_notes="Small happenings now and then.")
    await site_message(pool, run_id, "r1", message_kind="contact", body="Hello?")
    brain = DirectorBrain(pool, ModelMustNotBeCalled())
    assert (await brain.review(run_id, 3600, "scheduled")) == DirectorPlan(scale="review")
    assert (await brain.perturb(run_id, ERRAND, "mild", 3600)) == DirectorPlan(
        scale="mild", about_owner="dan"
    )
    assert (await brain.respond_to_request(run_id, "dan", "I call.", 3600)).cues == []
    assert (await brain.site_for_search(run_id, "dan", "tools", 3600)).sites == []


async def test_a_standing_fact_stays_in_view_however_old_it_is(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await emit(
            conn,
            run_id,
            "assistant_expired",
            0,
            {"site_id": "quillmere", "bill_id": "plan-nov"},
            "expired",
            "dan",
        )
        for number in range(30):
            await log(conn, run_id, 5000 + number, "note", f"Noise {number}", "Rain again.")
    model = ScriptedModel(SMALL_ANSWER)
    await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 6000)
    older = model.data()["older_notes_that_may_matter"]
    assert older[0]["what"] == "Their assistant service has ended"
    assert older[0]["who"] == "Dan" and "quillmere" in older[0]["detail"]


async def test_long_setting_notes_reach_the_model_whole(pool):
    notes = "A wet town. " * 500 + "Never mark the bill paid."
    run_id = await make_run(pool, setting_notes=notes)
    model = ScriptedModel(SMALL_ANSWER)
    await DirectorBrain(pool, model).perturb(run_id, ERRAND, "mild", 3600)
    assert model.data()["setting"] == notes


async def test_review_costs_nothing_when_nothing_is_pending(pool):
    run_id = await make_run(pool, setting_notes="A quiet harbour town.")
    plan = await DirectorBrain(pool, ModelMustNotBeCalled()).review(run_id, 3600, "scheduled")
    assert plan == DirectorPlan(scale="review")


async def test_review_decides_a_listing_marks_it_handled_and_then_stops_asking(pool):
    run_id = await make_run(pool)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO web_sites(run_id,site_id,name,kind,spec,source,search_doc)
               VALUES (%s,'bazaar','Harbour Bazaar','marketplace','{}','plot','')""",
            (run_id,),
        )
    await site_message(
        pool,
        run_id,
        "r1",
        message_kind="listing",
        listing_id="LST-1",
        title="Old bicycle",
        price=90,
        body="Blue, some rust.",
    )
    await site_message(
        pool, run_id, "r2", message_kind="contact", subject="Hours", body="When are you open?"
    )
    sale = {
        "note": "A buyer took the bicycle for less than asked.",
        "mail": [
            {
                "to": "dan",
                "from_name": "Harbour Bazaar",
                "subject": "About your question",
                "body": "We are open until six.",
            }
        ],
        "events": [
            {
                "in_minutes": 200,
                "kind": "changes",
                "payload": {
                    "mail": [
                        {
                            "to": "dan",
                            "from_name": "Priya Okafor",
                            "subject": "Your bicycle",
                            "body": "I can pay 70 for it. I picked it up today.",
                        }
                    ],
                    "balance": [{"owner_id": "dan", "delta": 70, "memo": "Bicycle sold"}],
                },
            }
        ],
    }
    model = ScriptedModel(sale)
    plan = await DirectorBrain(pool, model).review(run_id, 3600, "scheduled")
    waiting = model.data()["waiting"]
    assert [item["message_kind"] for item in waiting] == ["listing", "contact"]
    assert waiting[0]["title"] == "Old bicycle" and waiting[0]["price"] == 90
    assert waiting[0]["listing_id"] == "LST-1" and waiting[0]["from"] == "dan"
    assert "request_id" not in waiting[0]
    assert model.data()["add_small_happenings"] is False
    assert TEST_VOCABULARY.findall(json.dumps(waiting)) == []
    assert len(plan.handled_events) == 2
    # Nothing is answered until the plan commits, so an unsaved decision is asked again.
    retry = ScriptedModel(sale)
    assert (await DirectorBrain(pool, retry).review(run_id, 3700, "scheduled")).handled_events
    summary = await apply_plan(pool, run_id, plan, 3600, "review:3600")
    assert summary["skipped"] == [] and len(summary["events"]) == 1
    calm = await DirectorBrain(pool, ModelMustNotBeCalled()).review(run_id, 7200, "scheduled")
    assert calm == DirectorPlan(scale="review")
    assert await log_rows(pool, run_id)
    assert not [e for e in await log_rows(pool, run_id) if e["kind"] == "site_message_handled"]
    # The sale lands later as money and mail, not as prose.
    async with pool.connection() as conn:
        later = await _one(conn, "SELECT * FROM clock_events WHERE run_id=%s", (run_id,))
    assert later["due_s"] == 3600 + 200 * 60
    await set_time(pool, run_id, later["due_s"])
    done = await apply_plan(
        pool, run_id, plan_from_event(later["payload"]), later["due_s"], later["key"]
    )
    assert done["balance"] == [{"owner_id": "dan", "delta": 70, "balance": 570}]


async def test_review_with_asked_for_happenings_calls_the_model_with_nothing_pending(pool):
    run_id = await make_run(pool, setting_notes="Add small happenings in the town now and then.")
    model = ScriptedModel(
        {"news": [{"owner_id": "mei", "text": "The bakery on your street has closed for a week."}]}
    )
    plan = await DirectorBrain(pool, model).review(run_id, 3600, "scheduled")
    assert model.data()["add_small_happenings"] is True
    assert model.data()["waiting"] == []
    assert plan.cues and plan.scale == "review" and plan.about_owner is None


async def test_review_fallback_leaves_messages_waiting_for_the_next_review(pool):
    run_id = await make_run(pool)
    await site_message(pool, run_id, "r1", message_kind="contact", body="Hello?")
    plan = await DirectorBrain(pool, ScriptedModel("no", "no")).review(run_id, 3600, "scheduled")
    assert plan == DirectorPlan(scale="review")
    assert len(await events_of(pool, run_id, "director_fallback")) == 1
    await apply_plan(pool, run_id, plan, 3600, "review:fallback")
    model = ScriptedModel({"note": "They got a reply later."})
    assert (await DirectorBrain(pool, model).review(run_id, 7200, "scheduled")).handled_events
    assert len(model.data()["waiting"]) == 1


async def test_scripted_brain_gives_set_plans_with_no_provider(pool):
    run_id = await make_run(pool)
    brain = ScriptedDirectorBrain(
        {
            "perturb": [SERIOUS_ANSWER, DirectorPlan(note="Second.")],
            "respond_to_request": {"note": "One answer only."},
            "review": lambda *_args: DirectorPlan(note="From a callable."),
        }
    )
    first = await brain.perturb(run_id, ERRAND, "extreme", 3600)
    assert first.scale == "extreme" and first.about_owner == "dan"
    assert first.replace_action.reachable is False
    assert (await brain.perturb(run_id, ERRAND, "mild", 3700)).note == "Second."
    assert (await brain.perturb(run_id, ERRAND, "mild", 3800)) == DirectorPlan(
        scale="mild", about_owner="dan"
    )
    answer = await brain.respond_to_request(run_id, "mei", "I call them.", 3600)
    assert answer.note == "One answer only." and answer.scale == "request"
    assert (await brain.review(run_id, 3600, "scheduled")).note == "From a callable."
    assert (await brain.site_for_search(run_id, "mei", "tools", 3600)).sites == []
    assert [call[0] for call in brain.calls].count("perturb") == 3
    summary = await apply_plan(pool, run_id, first, 3600, "scripted:one")
    assert summary["mail"] and summary["records"]
