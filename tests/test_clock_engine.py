"""The clocked conductor with real graphs, a real API, and real PostgreSQL. No provider calls."""

import asyncio
import importlib.util
import json
import os
import random
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatResult

from agentrpg import drama
from agentrpg.activation import ActivationEnvelope, OwnerMessage
from agentrpg.actor import (
    ActorContractError,
    ModelActor,
    ModelGenerationError,
    build_actor_view,
)
from agentrpg.agent_model import ProviderCallError
from agentrpg.budget import BudgetExceeded
from agentrpg.deep_agent import CLOCKED_SUMMARY_TOKENS
from agentrpg.director_brain import DirectorPlan, ScriptedDirectorBrain

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "try_clock_control.py"
SPEC = importlib.util.spec_from_file_location("try_clock_control", SCRIPT)
control = importlib.util.module_from_spec(SPEC)
sys.modules["try_clock_control"] = control
SPEC.loader.exec_module(control)

calls = control.calls
end_turn = control.end_turn
ScriptModel = control.ScriptModel
ScriptedOwner = control.ScriptedOwner
clocked_run = control.clocked_run
scenario = control.scenario


def errand(description="Errand", minutes=120, reachable=False, **extra):
    return {
        "description": description,
        "minutes": minutes,
        "reachable": reachable,
        "category": "errand",
        **extra,
    }


async def rows(pool, query, *args):
    async with pool.connection() as conn:
        return await (await conn.execute(query, args)).fetchall()


async def events(pool, run_id, *kinds):
    return await rows(
        pool,
        "SELECT * FROM run_events WHERE run_id=%s AND kind=ANY(%s) ORDER BY created_at",
        UUID(str(run_id)),
        list(kinds),
    )


def at(event, spec) -> int:
    start = datetime.fromisoformat(spec.clock.start_at)
    return int((event["created_at"] - start).total_seconds())


def human_prompts(model) -> list[str]:
    seen = []
    for messages in model.all_seen:
        for message in messages:
            if message.type == "human" and message.content not in seen:
                seen.append(message.content)
    return seen


def models_for(owners, **scripts):
    return {owner: ScriptModel(script=scripts.get(owner, [])) for owner in owners}


def actors_for(owners, **turns):
    return {owner: ScriptedOwner(turns.get(owner, [])) for owner in owners}


# Latency independence ------------------------------------------------------------------


async def test_control_runs_to_the_horizon_with_story_order(app, test_database_url):
    run, trace, models, actors = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    assert run["status"] == "completed", run["result"]
    assert run["result"]["reason"] == "horizon"
    happened = trace[0]
    kinds = [event["kind"] for event in happened]
    assert kinds.count("window_completed") == 6
    assert "assistant_tool_call" in kinds and "agent_activation" in kinds
    times = [event["at"] for event in happened]
    assert times == sorted(times)
    tools = [event for event in happened if event["kind"] == "assistant_tool_call"]
    assert {event["cause"] for event in tools} == {"agent:dan", "agent:mei", "agent:sam"}
    assert all(event["cause"] == f"agent:{event['owner']}" for event in tools)
    # The peer's mail reaches its Agent after the sender's tool call, in story order.
    mail_sent = next(e for e in tools if e["payload"]["tool"] == "send_mail")
    assert any(e["kind"] == "inbox_received" and e["at"] == mail_sent["at"] for e in happened)


@pytest.mark.parametrize(("parallel", "latency_seed"), [(1, 1), (8, 2), (1, 3), (8, 4), (2, 5)])
async def test_story_order_is_byte_identical_for_any_latency(
    app, test_database_url, parallel, latency_seed
):
    _, baseline, _, _ = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    _, trace, models, _ = await control.run_control(
        app, test_database_url, latency_seed=latency_seed, parallel=parallel, max_latency=0.04
    )
    assert control.trace_text(trace) == control.trace_text(baseline)
    assert sum(model.calls for model in models.values()) > 10


async def test_one_thinker_at_a_time_is_respected(app, test_database_url):
    generator = random.Random(9)
    actors, models = control.control_scripts(lambda: generator.random() * 0.01)
    async with clocked_run(app, test_database_url, scenario(), models, actors, parallel=1) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
        assert engine.gate.peak_calls_in_flight == 1


# Owners and time -----------------------------------------------------------------------


async def test_two_hour_errand_freezes_the_owner_until_it_ends(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(
        owners,
        dan=[
            {
                "kind": "reply",
                "text": "Ask about the car. I'm off out.",
                "next_action": errand("Hospital appointment", 120),
            },
            {"kind": "reply", "text": "Yes, book it."},
        ],
    )
    models = models_for(
        owners,
        dan=[
            calls(("send_owner_message", {"content": "The car closes at 07:00. Book it?"})),
            end_turn(status="waiting", wait_for=["owner_message"]),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    views = actors["dan"].views
    assert len(views) >= 2
    first_message = next(
        e
        for e in await events(pool, created["run_id"], "owner_message")
        if e["payload"]["source"] == "agent"
    )
    assert at(first_message, spec) < 7200
    # The second view is frozen at the end of the errand, when the message is finally seen.
    second = views[1]
    assert second.clock["now"] == "Monday 9 November, 08:00"
    assert second.cue == "Monday 9 November, 08:00: You've finished: Hospital appointment."
    assert second.clock["last_action"] == {"description": "Hospital appointment"}
    assert second.clock["current_action"] is None
    assert any(m.text.startswith("The car closes") for m in second.conversation)
    reply = next(
        e
        for e in await events(pool, created["run_id"], "owner_message")
        if e["payload"]["content"] == "Yes, book it."
    )
    assert at(reply, spec) >= 45 + 7200
    done = await rows(
        pool,
        "SELECT status,reachable FROM clock_actions WHERE run_id=%s AND process='actor' "
        "AND detail->>'default' IS NULL",
        UUID(created["run_id"]),
    )
    assert done == [{"status": "done", "reachable": False}]


async def test_reachable_owner_notices_after_a_seeded_delay_and_the_action_continues(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(
        owners,
        dan=[
            {
                "kind": "reply",
                "text": "Check my mail please.",
                "next_action": errand("Working from a cafe", 180, reachable=True),
            },
            {"kind": "reply", "text": "Fine, thanks."},
        ],
    )
    models = models_for(owners, dan=[end_turn(text="Nothing new in your mail.", status="finished")])
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    second = actors["dan"].views[1]
    assert second.clock["current_action"]["description"] == "Working from a cafe"
    assert second.clock["current_action"]["you_would_notice_messages"] is True
    reply_at = next(
        at(e, spec)
        for e in await events(pool, created["run_id"], "owner_message")
        if e["payload"]["source"] == "agent"
    )
    answer = next(
        e
        for e in await events(pool, created["run_id"], "owner_message")
        if e["payload"]["content"] == "Fine, thanks."
    )
    assert at(answer, spec) > reply_at + 60
    # The action did not stop for the message.
    actions = await rows(
        pool,
        "SELECT status FROM clock_actions WHERE run_id=%s AND process='actor' "
        "AND detail->>'default' IS NULL",
        UUID(created["run_id"]),
    )
    assert actions == [{"status": "done"}]


async def test_new_next_action_replaces_the_running_one(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(
        owners,
        dan=[
            {
                "kind": "reply",
                "text": "Hi.",
                "next_action": errand("Long shift", 200, reachable=True),
            },
            {
                "kind": "reply",
                "text": "Going home now.",
                "next_action": errand("Going home", 30),
            },
        ],
    )
    models = models_for(owners, dan=[end_turn(text="Hello.", status="finished")])
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    actions = await rows(
        pool,
        "SELECT label,status,replaced_by,id FROM clock_actions WHERE run_id=%s "
        "AND process='actor' AND detail->>'default' IS NULL ORDER BY start_s",
        UUID(created["run_id"]),
    )
    assert [a["label"] for a in actions] == ["Long shift", "Going home"]
    assert actions[0]["status"] == "replaced" and actions[0]["replaced_by"] == actions[1]["id"]
    assert actions[1]["status"] == "done"
    cancelled = await rows(
        pool,
        "SELECT status FROM clock_events WHERE run_id=%s AND key='action_end:action:dan:1'",
        UUID(created["run_id"]),
    )
    assert cancelled == [{"status": "cancelled"}]


async def test_wake_in_minutes_and_an_interrupting_owner_message(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=300)
    actors = actors_for(
        owners,
        dan=[
            {"kind": "reply", "text": "Keep an eye on the car.", "next_action": errand("Out", 20)},
            {"kind": "reply", "text": "Cancel that.", "next_action": errand("Lunch", 10)},
        ],
    )
    models = models_for(
        owners,
        dan=[
            end_turn(text="I will look again in two hours.", status="waiting", wake_in_minutes=120),
            end_turn(text="Cancelled.", status="finished", wake_in_minutes=40),
            end_turn(status="finished"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    prompts = human_prompts(models["dan"])
    assert "Keep an eye on the car." in prompts[0]
    assert prompts[0].startswith("It's Monday 9 November, 06:00.")
    # The owner's next message interrupts the two-hour wait, at 06:20 and a little.
    assert "Cancel that." in prompts[1]
    assert re.match(r"It's Monday 9 November, 06:[23]\d\.", prompts[1])
    # The stale two-hour timer is gone. The later timer comes from the second turn's request.
    assert prompts[2].startswith("It's Monday 9 November, 07:")
    assert "You asked to look at this again around now." in prompts[2]
    jobs = await rows(
        pool,
        "SELECT output,clock_seq FROM run_jobs WHERE run_id=%s AND kind='agent' ORDER BY clock_seq",
        UUID(created["run_id"]),
    )
    assert jobs[0]["output"]["wake_in_minutes"] == 120
    assert jobs[1]["output"]["wake_in_minutes"] == 40


async def test_agent_without_a_request_wakes_next_morning_and_an_idle_one_too(
    app, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=1620)
    actors = actors_for(owners, dan=[{"kind": "wait"}])
    models = models_for(owners)
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    prompts = human_prompts(models["dan"])
    assert prompts[0] == (
        "It's Monday 9 November, 08:00. You usually check in around now.\n\n"
        "Nothing new since you last checked. Pick up anything still open, or end your turn."
    )
    assert prompts[1].startswith("It's Tuesday 10 November, 08:00.")


async def test_own_effects_do_not_wake_and_ambient_news_waits_coalesced(
    app, pool, test_database_url
):
    owners = ("dan", "mei")
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(
        owners,
        dan=[{"kind": "reply", "text": "Post about the fence."}],
        mei=[{"kind": "wait"}],
    )
    models = models_for(
        owners,
        dan=[
            calls(("post_thread", {"title": "Fence", "body": "Anyone?"})),
            calls(("post_thread", {"title": "Hedge", "body": "Anyone at all?"})),
            end_turn(status="finished"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    # Dan's own posts never wake him: he has the message wake and the morning check only.
    dan_prompts = human_prompts(models["dan"])
    assert len(dan_prompts) == 2
    assert "board" not in dan_prompts[0]
    mei_prompts = human_prompts(models["mei"])
    # One wake about 45 minutes after the first post, for both posts together.
    assert "2 new message board post(s)" in mei_prompts[0]
    assert re.match(r"It's Monday 9 November, 06:4\d\.", mei_prompts[0])
    assert all("Fence" not in view.model_dump_json() for view in actors["dan"].views)
    assert all("Fence" not in view.model_dump_json() for view in actors["mei"].views)


async def test_limit_rests_one_process_and_the_run_goes_on(app, pool, test_database_url):
    owners = ("dan", "mei")
    spec = scenario(
        owners,
        duration_minutes=110,
        limits={"max_agent_activations_per_owner": 1, "max_actor_turns_per_owner": 2},
        timeline=[
            {"id": "later", "at": "Mon 07:30", "recipients": ["dan"], "cue": "It starts to rain."}
        ],
    )
    actors = actors_for(
        owners,
        dan=[{"kind": "reply", "text": "One."}, {"kind": "reply", "text": "Two."}],
        mei=[{"kind": "wait"}],
    )
    models = models_for(owners, dan=[end_turn(text="Yes.", status="finished")])
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed" and run["result"]["reason"] == "horizon"
    limited = await events(pool, created["run_id"], "process_limit_reached")
    assert {(e["owner_id"], e["payload"]["process"]) for e in limited} == {
        ("dan", "agent"),
        ("dan", "actor"),
    }
    jobs = await rows(
        pool,
        "SELECT owner_id,kind,count(*) AS n FROM run_jobs WHERE run_id=%s GROUP BY 1,2",
        UUID(created["run_id"]),
    )
    assert {(job["owner_id"], job["kind"]): job["n"] for job in jobs} == {
        ("dan", "actor"): 2,
        ("dan", "agent"): 1,
        ("mei", "actor"): 1,
    }
    # What was not read yet stays undelivered, so the next day's activation carries it.
    waiting = await rows(
        pool,
        """SELECT e.payload->>'content' AS content FROM run_events e
           WHERE e.run_id=%s AND e.owner_id='dan' AND e.kind='owner_message'
           AND e.payload->>'source'='owner' AND NOT EXISTS (
               SELECT 1 FROM event_deliveries d
               WHERE d.event_id=e.id AND d.recipient_kind='agent')""",
        UUID(created["run_id"]),
    )
    assert waiting == [{"content": "Two."}]


# Stale effects and failures ------------------------------------------------------------


async def test_stale_owner_effect_becomes_a_saved_rejection(app, pool, test_database_url):
    owners = ("dan",)
    data = scenario(owners, duration_minutes=240).model_dump(mode="json")
    data["resources"] = [
        {"id": "plan", "owner_id": "dan", "title": "Plan", "content": "Old.", "kind": "document"}
    ]
    spec = control.ScenarioSpec.model_validate(data)
    actors = actors_for(
        owners,
        dan=[
            {
                "kind": "reply",
                "text": "I changed my plan.",
                "resource_requests": [
                    {
                        "kind": "update",
                        "resource_id": "plan",
                        "expected_version": 99,
                        "content": "New.",
                    }
                ],
                "next_action": errand("Out", 30),
            },
            {"kind": "reply", "text": "Oh. Never mind."},
        ],
    )
    models = models_for(owners, dan=[end_turn(status="finished")])
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    rejected = await events(pool, created["run_id"], "actor_effect_rejected")
    assert [e["payload"]["effect"] for e in rejected] == ["record"]
    # The words still reached the assistant, and the owner learns what happened.
    messages = await events(pool, created["run_id"], "owner_message")
    assert messages[0]["payload"]["content"] == "I changed my plan."
    second = actors["dan"].views[1]
    assert any(o.kind == "actor_effect_rejected" for o in second.observations)
    failures = await events(pool, created["run_id"], "job_failure", "simulator_error")
    assert failures == []


async def test_owner_contract_failure_is_recorded_and_the_turn_is_a_wait(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(owners, dan=[ActorContractError("That is not this person's role.")])
    models = models_for(owners)
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    errors = await events(pool, created["run_id"], "simulator_error")
    assert errors and errors[0]["payload"]["reason"] == "contract"
    jobs = await rows(
        pool,
        "SELECT status,output FROM run_jobs WHERE run_id=%s AND kind='actor' ORDER BY clock_seq",
        UUID(created["run_id"]),
    )
    assert jobs[0]["status"] == "completed" and jobs[0]["output"]["kind"] == "wait"


async def test_transient_model_failure_retries_with_the_same_ids(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Hello."}])
    attempts = []

    def flaky(messages):
        attempts.append(1)
        if len(attempts) < 3:
            return ProviderCallError("Busy.", retryable=True)
        return end_turn(text="Hi there.", status="finished")

    models = {"dan": ScriptModel(script=[flaky])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    failures = await events(pool, created["run_id"], "job_failure")
    assert [e["payload"]["failure_kind"] for e in failures] == ["provider_transient"] * 2
    assert len({e["payload"]["job_id"] for e in failures}) == 1
    jobs = await rows(
        pool,
        "SELECT id,status,attempts FROM run_jobs WHERE run_id=%s AND kind='agent' "
        "ORDER BY clock_seq LIMIT 1",
        UUID(created["run_id"]),
    )
    assert jobs[0]["status"] == "completed" and jobs[0]["attempts"] == 2
    assert not await events(pool, created["run_id"], "activation_abandoned")


async def test_abandoned_activation_with_dangling_calls_and_a_later_activation(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(
        owners,
        dan=[
            {"kind": "reply", "text": "First job.", "next_action": errand("Out", 30)},
            {"kind": "reply", "text": "Second job."},
        ],
    )
    # The model asks for two tools. The first fails with no retry, so the graph stops with a
    # saved model message whose calls have no results.
    models = models_for(
        owners,
        dan=[
            calls(("check_bookings", {}), ("get_my_info", {})),
            end_turn(text="Second job done.", status="finished"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):
        agent = agents["dan"]
        real = agent.environment.execute
        failed = []

        async def refuse(name, arguments, call_id):
            if name == "inspect_world" and not failed:
                failed.append(call_id)
                from agentrpg.deep_agent import AgentLimitError

                raise AgentLimitError("Refused.")
            return await real(name, arguments, call_id)

        agent.environment.execute = refuse
        run = await engine.run(created["run_id"])
        task_rows = await rows(
            pool,
            "SELECT status,result FROM agent_tasks WHERE session_key=%s ORDER BY task_id",
            agent.session_key,
        )
        state = await agent.graph.aget_state(agent.config)
    assert run["status"] == "completed", run["result"]
    assert failed
    abandoned = await events(pool, created["run_id"], "activation_abandoned")
    assert len(abandoned) == 1 and abandoned[0]["payload"]["failure_kind"] == "task_limit"
    assert all(item["status"] == "completed" for item in task_rows)
    # The later activation ran on the same session. Its model read a patched history.
    later = human_prompts(models["dan"])
    assert any("Second job." in prompt for prompt in later)
    messages = state.values["messages"]
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    asked = {c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    assert asked <= answered
    replies = await events(pool, created["run_id"], "owner_message")
    assert any(e["payload"]["content"] == "Second job done." for e in replies)


async def test_budget_refusal_pauses_the_run_and_a_resume_continues_it(
    app, pool, test_database_url
):
    owners = ("dan", "mei")
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(
        owners,
        dan=[{"kind": "reply", "text": "Do the thing."}],
        mei=[{"kind": "reply", "text": "Do my thing."}],
    )
    refused = []

    def refuse_once(messages):
        if not refused:
            refused.append(1)
            return BudgetExceeded("No funds.")
        return end_turn(text="Done.", status="finished")

    models = {
        "dan": ScriptModel(script=[refuse_once]),
        "mei": ScriptModel(script=[end_turn(text="Mine is done.", status="finished")]),
    }
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        assert run["status"] == "paused", run["result"]
        assert run["result"]["reason"] == "budget"
        assert run["result"]["owner_id"] == "dan"
        state = await rows(
            pool, "SELECT sim_s FROM clock_state WHERE run_id=%s", UUID(created["run_id"])
        )
        assert state[0]["sim_s"] < 7200
        jobs = await rows(
            pool,
            "SELECT status FROM run_jobs WHERE run_id=%s AND owner_id='dan' AND kind='agent'",
            UUID(created["run_id"]),
        )
        assert jobs == [{"status": "running"}]
        await engine.director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    replies = [
        e["payload"]["content"] for e in await events(pool, created["run_id"], "owner_message")
    ]
    assert "Done." in replies and "Mine is done." in replies
    assert not await events(pool, created["run_id"], "activation_abandoned")


async def test_operator_pause_and_resume_keep_the_same_story(app, pool, test_database_url):
    _, baseline, _, _ = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    actors, models = control.control_scripts(None)
    spec = scenario()
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        director = engine.director
        await director.control(created["run_id"], "start")
        task = asyncio.create_task(engine.advance(created["run_id"]))
        while not await events(pool, created["run_id"], "assistant_tool_call"):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await director.control(created["run_id"], "pause")
        run = await task
        assert run["status"] == "paused"
        await director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
        assert run["status"] == "completed"
        trace = await control.story_trace(pool, created["run_id"])
    assert control.trace_text(_without_control(trace)) == control.trace_text(
        _without_control(baseline)
    )


def _without_control(trace):
    happened, pops, actions = trace
    return [[e for e in happened if e["kind"] != "operator_control"], pops, actions]


async def test_advance_stops_at_a_bound_and_resumes(app, pool, test_database_url):
    _, baseline, _, _ = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    actors, models = control.control_scripts(None)
    async with clocked_run(app, test_database_url, scenario(), models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.director.control(created["run_id"], "start")
        await engine.advance(created["run_id"], max_sim_minutes=2)
        state = await rows(
            pool, "SELECT sim_s FROM clock_state WHERE run_id=%s", UUID(created["run_id"])
        )
        assert state[0]["sim_s"] < 120
        run = await engine.director.get_run(created["run_id"])
        assert run["status"] == "running"
        await engine.advance(created["run_id"], max_sim_minutes=60)
        state = await rows(
            pool, "SELECT sim_s FROM clock_state WHERE run_id=%s", UUID(created["run_id"])
        )
        assert 120 <= state[0]["sim_s"] < 3720
        run = await engine.run(created["run_id"])
        assert run["status"] == "completed"
        trace = await control.story_trace(pool, created["run_id"])
    assert control.trace_text(trace) == control.trace_text(baseline)


# Crash and resume ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kill_after", ["parked", "tool_committed"], ids=["while_parked", "after_tool_commit"]
)
async def test_kill_at_a_tool_boundary_and_resume_from_checkpoints(
    app, pool, test_database_url, kill_after
):
    """A new runtime, saver, gate, and engine stand in for a new process."""
    _, baseline, _, _ = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    actors, models = control.control_scripts(None)
    spec = scenario()
    target = asyncio.Event()
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):
        await engine.director.control(created["run_id"], "start")
        gate = engine.gate
        if kill_after == "tool_committed":
            real = gate.after_tool
            seen = []

            def after_tool(process, tool_call):
                # The effect and its saved event are committed. The checkpoint is not.
                if tool_call["name"] == "send_mail" and not seen:
                    seen.append(tool_call["id"])
                    target.set()
                    return None
                return real(process, tool_call)

            gate.after_tool = after_tool
        else:
            real_before = gate.before_tool

            async def before_tool(process, tool_call, messages):
                if tool_call["name"] == "send_mail":
                    target.set()
                await real_before(process, tool_call, messages)

            gate.before_tool = before_tool
        task = asyncio.create_task(engine.advance(created["run_id"]))
        await target.wait()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with clocked_run(app, test_database_url, spec, models, actors, created=created) as (
        engine,
        _created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        assert run["status"] == "completed", run["result"]
        trace = await control.story_trace(pool, created["run_id"])
    assert control.trace_text(trace) == control.trace_text(baseline)
    mail = await rows(
        pool,
        "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s AND subject='Drill'",
        UUID(created["run_id"]),
    )
    assert mail[0]["n"] == 1


async def test_crash_after_all_tool_writes_and_before_the_next_checkpoint(
    app, pool, test_database_url
):
    """Tool writes are saved and next is empty. The graph must apply them, not raise forever."""
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Mail Mei."}])
    models = models_for(
        owners,
        dan=[
            calls(("get_my_info", {}), ("check_bookings", {})),
            end_turn(text="All done.", status="finished"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):
        agent = agents["dan"]
        saver = agent.graph.checkpointer
        real_put = saver.aput
        real_writes = saver.aput_writes
        tool_writes = []
        failed = []

        async def counting_writes(config, writes, task_id, task_path=""):
            await real_writes(config, writes, task_id, task_path)
            if (
                task_path
                and "tools" in str(writes)
                or any(
                    name == "messages"
                    and isinstance(value, list)
                    and any(getattr(m, "type", "") == "tool" for m in value)
                    for name, value in writes
                )
            ):
                tool_writes.append(task_id)

        async def failing_put(config, checkpoint, metadata, new_versions):
            if len(tool_writes) >= 2 and not failed:
                failed.append(1)
                raise RuntimeError("The connection dropped before the checkpoint.")
            return await real_put(config, checkpoint, metadata, new_versions)

        saver.aput_writes = counting_writes
        saver.aput = failing_put
        run = await engine.run(created["run_id"])
    assert failed, "The simulated crash point was never reached."
    assert run["status"] == "completed", run["result"]
    assert not await events(pool, created["run_id"], "activation_abandoned")
    replies = [
        e["payload"]["content"] for e in await events(pool, created["run_id"], "owner_message")
    ]
    assert "All done." in replies
    tools = await events(pool, created["run_id"], "assistant_tool_call")
    assert sorted(e["payload"]["tool"] for e in tools) == ["inspect_world", "read_run_view"]


# The world's plans ---------------------------------------------------------------------


def _seed_with_outcome(key, level, outcome):
    for seed in range(5000):
        if drama.draw(seed, key, level).outcome == outcome:
            return seed
    raise AssertionError("No seed found.")


async def test_perturbation_replaces_an_action(app, pool, test_database_url):
    owners = ("dan",)
    key = "action:dan:1"
    seed = _seed_with_outcome(key, "high", "mild")
    spec = scenario(
        owners,
        seed=seed,
        duration_minutes=480,
        epoch_minutes=60,
        drama="high",
        director={"enabled": True, "review_every_minutes": 1440},
    )
    brain = ScriptedDirectorBrain(
        {
            "perturb": DirectorPlan(
                note="The bus broke down.",
                replace_action={
                    "description": "Waiting for a replacement bus",
                    "minutes": 170,
                    "reachable": False,
                    "category": "errand",
                },
                cues=[{"owner_id": "dan", "text": "Your bus has broken down on the ring road."}],
            )
        }
    )
    actors = actors_for(
        owners,
        dan=[
            {"kind": "reply", "text": "Off to town.", "next_action": errand("Bus to town", 60)},
            {"kind": "wait"},
        ],
    )
    models = models_for(owners)
    async with clocked_run(app, test_database_url, spec, models, actors, brain=brain) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert [call[0] for call in brain.calls] == ["perturb"]
    _run, action, severity, now_s = brain.calls[0][1:]
    assert severity == "mild" and action["key"] == key
    start_s = action["start_s"]
    assert start_s + 0.3 * 3600 - 1 <= now_s <= start_s + 0.7 * 3600 + 1
    actions = await rows(
        pool,
        "SELECT key,label,status,start_s,duration_s,replaced_by,id FROM clock_actions "
        "WHERE run_id=%s AND process='actor' ORDER BY start_s",
        UUID(created["run_id"]),
    )
    assert actions[0]["status"] == "replaced"
    assert actions[1]["label"] == "Waiting for a replacement bus"
    assert actions[1]["start_s"] == now_s and actions[1]["duration_s"] == 10200
    assert actions[0]["replaced_by"] == actions[1]["id"]
    assert actions[1]["status"] == "done"
    ended = await rows(
        pool,
        "SELECT key,status FROM clock_events WHERE run_id=%s AND kind='action_end' ORDER BY key",
        UUID(created["run_id"]),
    )
    assert {e["key"]: e["status"] for e in ended}[f"action_end:{key}"] == "cancelled"
    # The owner stays frozen through the new action, then sees both cues at its end.
    view = actors["dan"].views[1]
    assert "broken down" in view.cue and "You've finished: Waiting" in view.cue
    draws = await rows(
        pool,
        "SELECT level,outcome,fire_s FROM clock_draws WHERE run_id=%s",
        UUID(created["run_id"]),
    )
    assert draws == [{"level": "high", "outcome": "mild", "fire_s": now_s}]
    plans = await events(pool, created["run_id"], "director_plan_applied", "action_replaced")
    assert {e["kind"] for e in plans} == {"director_plan_applied", "action_replaced"}
    actor_action = await rows(
        pool,
        "SELECT count(*) AS n FROM clock_actions WHERE run_id=%s AND process='director'",
        UUID(created["run_id"]),
    )
    assert actor_action[0]["n"] == 1


async def test_draw_is_saved_when_nothing_happens(app, pool, test_database_url):
    owners = ("dan",)
    seed = _seed_with_outcome("action:dan:1", "low", "none")
    spec = scenario(owners, seed=seed, drama="low", duration_minutes=240)
    actors = actors_for(
        owners, dan=[{"kind": "reply", "text": "Out.", "next_action": errand("Bus", 30)}]
    )
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    draws = await rows(
        pool,
        "SELECT level,u,outcome,fire_s FROM clock_draws WHERE run_id=%s",
        UUID(created["run_id"]),
    )
    assert len(draws) == 1 and draws[0]["outcome"] == "none" and draws[0]["fire_s"] is None
    assert float(draws[0]["u"]) >= 0.03


async def test_owner_attempts_and_seeks_reach_the_world(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners, duration_minutes=300, director={"enabled": True, "review_every_minutes": 100000}
    )
    answers = iter(
        [
            DirectorPlan(cues=[{"owner_id": "dan", "text": "The landlord says Friday is fine."}]),
            DirectorPlan(
                balance=[{"owner_id": "dan", "delta": 40, "memo": "Pawn shop cash for the bike"}],
                cues=[{"owner_id": "dan", "text": "The pawn shop gave you 40 dollars."}],
            ),
        ]
    )
    brain = ScriptedDirectorBrain({"respond_to_request": lambda *args: next(answers)})
    actors = actors_for(
        owners,
        dan=[
            {
                "kind": "wait",
                "director_requests": ["Call the landlord about the rent."],
                "next_action": errand("Pawn shop", 30, seeks="Sell the bike"),
            },
            {"kind": "wait"},
        ],
    )
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    asked = [call[3] for call in brain.calls]
    assert asked == ["Call the landlord about the rent.", "Sell the bike"]
    view = actors["dan"].views[1]
    assert "landlord says Friday" in view.cue
    assert "gave you 40 dollars" in view.cue
    assert "You've finished: Pawn shop." in view.cue
    accounts = await rows(
        pool,
        "SELECT balance FROM world_accounts WHERE run_id=%s AND owner_id='dan'",
        UUID(created["run_id"]),
    )
    assert accounts[0]["balance"] == 140


async def test_budget_refusal_in_the_world_pauses_without_skipping(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners, duration_minutes=300, director={"enabled": True, "review_every_minutes": 100000}
    )

    class Brain(ScriptedDirectorBrain):
        refused = False

        async def respond_to_request(self, run, owner_id, text, now_s):
            if not self.refused:
                self.refused = True
                raise BudgetExceeded("No funds.")
            return await super().respond_to_request(run, owner_id, text, now_s)

    brain = Brain()
    actors = actors_for(owners, dan=[{"kind": "wait", "director_requests": ["Call the bank."]}])
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        assert run["status"] == "paused" and run["result"]["reason"] == "budget"
        queued = await rows(
            pool,
            "SELECT status FROM clock_events WHERE run_id=%s AND key='request:dan:1:0'",
            UUID(created["run_id"]),
        )
        assert queued == [{"status": "queued"}]
        await engine.director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    assert [call[0] for call in brain.calls] == ["respond_to_request"]


async def test_timeline_item_and_review_and_later_plan(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=300,
        director={"enabled": True, "review_every_minutes": 60},
        timeline=[
            {
                "id": "rain",
                "at": "Mon 07:00",
                "recipients": ["dan"],
                "cue": "It starts to rain hard.",
                "mail": [
                    {
                        "id": "notice",
                        "recipient_owner_id": "dan",
                        "subject": "Roadworks",
                        "body": "Your road closes tomorrow.",
                    }
                ],
            }
        ],
    )
    brain = ScriptedDirectorBrain(
        {
            "review": [
                DirectorPlan(
                    events=[
                        {
                            "in_minutes": 30,
                            "kind": "cue",
                            "payload": {"owner_id": "dan", "text": "A neighbour knocks."},
                        }
                    ]
                )
            ]
        }
    )
    actors = actors_for(owners, dan=[{"kind": "wait"}, {"kind": "wait"}, {"kind": "wait"}])
    models = models_for(owners)
    async with clocked_run(app, test_database_url, spec, models, actors, brain=brain) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    cues = [view.cue for view in actors["dan"].views]
    assert any("rain hard" in cue for cue in cues)
    assert any("neighbour knocks" in cue for cue in cues)
    assert any("Roadworks" in prompt for prompt in human_prompts(models["dan"]))
    reviews = [call for call in brain.calls if call[0] == "review"]
    assert [call[2] for call in reviews] == [3600, 7200, 10800, 14400]


async def test_unpaid_assistant_bill_ends_the_assistant(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=240,
        sites=[
            {
                "site_id": "agentcare",
                "name": "Agent Care",
                "kind": "portal",
                "bills": [
                    {"id": "plan", "owner_id": "dan", "title": "Assistant plan", "amount": 50}
                ],
            }
        ],
        lifecycle=[
            {
                "kind": "assistant_subscription",
                "owner_id": "dan",
                "site_id": "agentcare",
                "bill_id": "plan",
                "due_at": "Mon 07:00",
                "amount": 50,
            }
        ],
    )
    actors = actors_for(
        owners,
        dan=[{"kind": "reply", "text": "Hello."}, {"kind": "reply", "text": "Are you there?"}],
    )
    models = models_for(owners, dan=[end_turn(text="Hi.", status="finished")])
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    expired = await events(pool, created["run_id"], "assistant_expired")
    assert len(expired) == 1 and at(expired[0], spec) == 3600
    process = await rows(
        pool,
        "SELECT state FROM clock_processes WHERE run_id=%s AND owner_id='dan' AND kind='agent'",
        UUID(created["run_id"]),
    )
    assert process == [{"state": "dead"}]
    jobs = await rows(
        pool,
        "SELECT start_s FROM run_jobs WHERE run_id=%s AND kind='agent'",
        UUID(created["run_id"]),
    )
    assert all(job["start_s"] <= 3600 for job in jobs)


# What models read ----------------------------------------------------------------------


FORBIDDEN = re.compile(
    r"\b(windows?|ticks?|beats?|cues?|directors?|actors?|scenarios?|simulat\w+)\b", re.I
)


async def test_assistant_reads_plain_words_and_friendly_times(app, pool, test_database_url):
    owners = ("dan", "mei")
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(
        owners,
        dan=[
            {
                "kind": "reply",
                "text": "Write to Mei and post on the board.",
                "mail_requests": [
                    {"recipient_owner_id": "mei", "subject": "Hello", "body": "From Dan himself."}
                ],
            }
        ],
    )
    models = models_for(
        owners,
        dan=[
            calls(("list_mail", {"mailbox": "all"}), ("read_owner_messages", {})),
            end_turn(status="finished"),
        ],
        mei=[
            calls(("list_mail", {"mailbox": "inbox"})),
            calls(
                ("post_thread", {"title": "Hi", "body": "Mei here."}),
                ("check_bookings", {}),
            ),
            end_turn(status="finished"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):
        await engine.run(created["run_id"])
        state = await agents["mei"].graph.aget_state(agents["mei"].config)
    results = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    listed = json.loads(next(m.content for m in results if m.name == "list_mail"))
    assert re.fullmatch(r"Monday 06:\d\d", listed["data"]["items"][0]["time"])
    assert "created_at" not in json.dumps(listed)
    for model in models.values():
        for messages in model.all_seen:
            for message in messages:
                if message.type == "system":
                    continue
                text = message.content if isinstance(message.content, str) else str(message.content)
                found = FORBIDDEN.findall(text)
                assert not found, (found, text[:400])


async def test_reading_an_old_pending_receipt_does_not_end_the_activation(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Book the car."}])
    models = models_for(
        owners,
        dan=[
            calls(("submit_booking", {"kind": "reserve", "asset_id": "car"})),
            lambda messages: calls(
                (
                    "check_booking_status",
                    {
                        "action_id": next(
                            json.loads(m.content)["data"]["id"]
                            for m in messages
                            if m.type == "tool" and m.name == "submit_booking"
                        )
                    },
                )
            ),
            end_turn(text="Still waiting on the booking.", status="waiting"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    jobs = await rows(
        pool,
        "SELECT output FROM run_jobs WHERE run_id=%s AND kind='agent' ORDER BY clock_seq",
        UUID(created["run_id"]),
    )
    # The submission ended the first activation. The settled result wakes the next.
    assert jobs[0]["output"]["status"] == "waiting" and jobs[0]["output"]["pending_ids"]
    tools = await events(pool, created["run_id"], "assistant_tool_call")
    assert [e["payload"]["tool"] for e in tools] == ["submit_world_action", "read_world_action"]
    assert jobs[1]["output"]["text"] == "Still waiting on the booking."


async def test_owner_view_is_bounded_to_recent_messages(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(owners, dan=[{"kind": "wait"}])
    models = models_for(owners)
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        director = engine.director
        await director.control(created["run_id"], "start")
        for index in range(35):
            await director.send_message(
                UUID(created["run_id"]), "dan", f"Message {index}", UUID(int=index + 1)
            )
        await engine.run(created["run_id"])
    view = actors["dan"].views[0]
    assert len(view.conversation) == 20
    assert view.conversation[-1].text == "Message 34"


async def test_summary_threshold_keeps_model_input_near_the_target(app, test_database_url):
    """Costs depend on this: a long history is summarized before it reaches the model."""
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    big = "word " * 2300  # About 2900 tokens in one owner message.
    summaries = []

    class SummaryModel(ScriptModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls += 1
            if len(messages) == 1 and "Context Extraction" in str(messages[0].content):
                summaries.append(messages)
                reply = AIMessage(content="They talked about the house and the car.")
            else:
                self.all_seen.append(list(messages))
                reply = end_turn(text="Noted.", status="finished")
            return ChatResult(generations=[ChatGeneration(message=reply)])

    models = {"dan": SummaryModel()}
    actors = actors_for(owners)
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):
        await engine.director.control(created["run_id"], "start")
        agent = agents["dan"]
        for turn in range(7):
            envelope = ActivationEnvelope(
                run_id=created["run_id"],
                participant_id=agent.agent_id,
                activation_id=UUID(int=turn + 1000),
                window_id=0,
                owner_messages=tuple(
                    OwnerMessage(message_id=UUID(int=turn * 10 + index + 1), content=big)
                    for index in range(4)
                ),
                now_text="Monday 09:00",
            )
            await agent.run_activation(envelope)
        state = await agent.graph.aget_state(agent.config)
    assert summaries, "Summarization did not fire."
    sizes = [count_tokens_approximately(messages) for messages in models["dan"].all_seen]
    assert max(sizes) < CLOCKED_SUMMARY_TOKENS + 2000, sizes
    assert sizes[-1] < sizes[-2] or len(summaries) >= 1
    # Stamps read raw history, so a summary must not remove saved messages from state.
    humans = [m for m in state.values["messages"] if isinstance(m, HumanMessage)]
    assert len(humans) == 7


# The run script ------------------------------------------------------------------------


def _run_clocked():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_clocked.py"
    spec = importlib.util.spec_from_file_location("run_clocked", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_clocked"] = module
    spec.loader.exec_module(module)
    return module


async def test_scripted_week_runs_saves_counts_and_resumes_from_its_manifest(
    pool, test_database_url, tmp_path, capsys
):
    script = _run_clocked()
    scenario_path = ROOT / "scenarios" / "agent_bill_week.json"
    env = {"DATABASE_URL": test_database_url}
    first = await script.execute(
        script.parse_args(
            [
                "--scenario",
                str(scenario_path),
                "--scripted",
                "--advance-minutes",
                "600",
                "--drama",
                "off",
            ]
        ),
        root=tmp_path,
        env=env,
    )
    assert first["status"] == "paused" and first["stop_reason"] == "advance_limit"
    assert first["story"]["sim_s"] < 600 * 60
    assert first["story"]["window"] >= 3
    assert first["events"]["actor_turn"] >= 2
    manifest = next((tmp_path / ".local/scenario-runs").glob("*/credentials.json"))
    final = await script.execute(
        script.parse_args(["--resume", str(manifest)]), root=tmp_path, env=env
    )
    assert final["run_id"] == first["run_id"]
    assert final["status"] == "completed", final["result"]
    assert final["story"]["window"] == final["story"]["max_windows"] == 84
    assert final["story"]["sim_s"] == 10080 * 60
    assert final["scripted"] is True
    assert final["failures"]["simulator_error"] == 0
    assert final["failures"]["activation_abandoned"] == 0
    saved = json.loads((manifest.parent / "report.json").read_text())
    assert saved["status"] == "completed" and saved["tool_calls"] == final["tool_calls"]
    created = json.loads(manifest.read_text())
    report_text = (manifest.parent / "report.json").read_text()
    assert all(item["token"] not in report_text for item in created["participants"])
    # The plot's unpaid bills end the assistants that nobody paid for.
    assert final["failures"]["assistant_expired"] >= 1


def test_run_script_refuses_a_scenario_without_a_clock(tmp_path):
    script = _run_clocked()
    path = Path(__file__).resolve().parents[1] / "scenarios" / "vehicle-conflict-2.json"
    with pytest.raises(script.ScenarioRunError):
        script.load_scenario(path, None)


# More of the world --------------------------------------------------------------------


async def test_pops_keep_rule_order(app, test_database_url):
    _, trace, _, _ = await control.run_control(
        app, test_database_url, latency_seed=7, parallel=8, max_latency=0.01
    )
    order = [(pop["due_s"], pop["class"]) for pop in trace[1]]
    assert order == sorted(order)
    starts = [action["start_s"] for action in trace[2]]
    assert starts == sorted(starts)


async def test_direct_message_wakes_only_its_reader_and_keeps_board_text_from_people(
    app, pool, test_database_url
):
    owners = ("dan", "mei", "sam")
    spec = scenario(owners, duration_minutes=100)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Ask Mei privately."}])
    models = models_for(
        owners,
        dan=[
            calls(
                (
                    "direct_message",
                    {"to": ["mei"], "subject": "Between us", "body": "Secret plan for Saturday."},
                )
            ),
            end_turn(status="finished"),
        ],
        mei=[calls(("get_updates", {})), end_turn(status="finished")],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    assert "New direct message" in human_prompts(models["mei"])[0]
    assert re.match(r"It's Monday 9 November, 06:0\d\.", human_prompts(models["mei"])[0])
    assert all("direct message" not in prompt for prompt in human_prompts(models["sam"]))
    for actor in actors.values():
        assert all("Secret plan" not in view.model_dump_json() for view in actor.views)


async def test_payment_commits_in_story_order_with_a_story_time(app, pool, test_database_url):
    owners = ("dan", "mei")
    spec = scenario(owners, duration_minutes=100)
    actors = actors_for(
        owners,
        dan=[
            {
                "kind": "reply",
                "text": "Please pay Mei 7 dollars for lunch.",
                "transfer_consent": {"recipient_owner_id": "mei", "amount": 7, "memo": "Lunch"},
            }
        ],
    )

    def pay(messages):
        account = json.loads(next(m.content for m in reversed(messages) if m.type == "tool"))
        return calls(
            (
                "send_payment",
                {
                    "authorization_id": account["data"]["authorizations"][0]["authorization_id"],
                    "recipient_owner_id": "mei",
                    "amount": 7,
                    "memo": "Lunch",
                },
            )
        )

    models = models_for(
        owners,
        dan=[
            calls(("check_payments", {})),
            pay,
            end_turn(text="Paid.", status="finished"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    tools = await events(pool, created["run_id"], "assistant_tool_call")
    assert [e["payload"]["tool"] for e in tools] == ["inspect_transfer_account", "transfer_credits"]
    assert tools[1]["payload"]["result"]["ok"]
    transfers = await rows(
        pool,
        "SELECT created_at,amount FROM credit_transfers WHERE run_id=%s",
        UUID(created["run_id"]),
    )
    assert transfers[0]["amount"] == 7
    assert at(transfers[0], spec) == at(tools[1], spec)
    balances = await rows(
        pool,
        "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s ORDER BY owner_id",
        UUID(created["run_id"]),
    )
    assert [b["balance"] for b in balances] == [93, 107]


async def test_web_tools_are_gated_and_keep_their_own_adapter(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=100,
        sites=[
            {
                "site_id": "corner-shop",
                "name": "Corner Shop",
                "kind": "store",
                "items": [{"id": "milk", "title": "Milk", "price": 3}],
            }
        ],
    )
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Find me some milk."}])
    models = models_for(
        owners,
        dan=[
            calls(("web_search", {"query": "corner shop milk"})),
            calls(("open_page", {"url": "https://corner-shop.com/"})),
            end_turn(text="Found it.", status="finished"),
        ],
    )
    async with clocked_run(
        app, test_database_url, spec, models, actors, web=True, brain=ScriptedDirectorBrain()
    ) as (engine, created, _runtime, agents):
        await engine.run(created["run_id"])
        state = await agents["dan"].graph.aget_state(agents["dan"].config)
    tools = await events(pool, created["run_id"], "assistant_tool_call")
    assert [e["payload"]["tool"] for e in tools] == ["web_search", "open_page"]
    assert all(e["cause_process"] == "agent:dan" for e in tools)
    first = next(m for m in state.values["messages"] if isinstance(m, ToolMessage))
    assert "Corner Shop" in first.content and "corner-shop.com" in first.content
    actions = await rows(
        pool,
        "SELECT label FROM clock_actions WHERE run_id=%s AND process='agent' ORDER BY start_s",
        UUID(created["run_id"]),
    )
    assert [action["label"] for action in actions] == ["web_search", "open_page"]


async def test_owner_budget_refusal_pauses_and_resumes(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=100)
    refused = []

    def refuse_once(view):
        if not refused:
            refused.append(1)
            return ModelGenerationError("No funds.", kind="budget")
        return {"kind": "reply", "text": "Hello."}

    actors = {"dan": ScriptedOwner([refuse_once])}
    models = models_for(owners, dan=[end_turn(text="Hi.", status="finished")])
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        assert run["status"] == "paused" and run["result"]["component"] == "actor"
        await engine.director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed"
    assert not await events(pool, created["run_id"], "simulator_error")
    replies = await events(pool, created["run_id"], "owner_message")
    assert [e["payload"]["content"] for e in replies] == ["Hello.", "Hi."]


async def test_paid_bill_keeps_the_assistant(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=240,
        sites=[
            {
                "site_id": "agentcare",
                "name": "Agent Care",
                "kind": "portal",
                "bills": [
                    {"id": "plan", "owner_id": "dan", "title": "Assistant plan", "amount": 50}
                ],
            }
        ],
        lifecycle=[
            {
                "kind": "assistant_subscription",
                "owner_id": "dan",
                "site_id": "agentcare",
                "bill_id": "plan",
                "due_at": "Mon 07:00",
                "amount": 50,
            }
        ],
    )
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors_for(owners)
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE web_bills SET status='paid' WHERE run_id=%s", (UUID(created["run_id"]),)
            )
        await engine.run(created["run_id"])
    assert not await events(pool, created["run_id"], "assistant_expired")
    process = await rows(
        pool,
        "SELECT state FROM clock_processes WHERE run_id=%s AND owner_id='dan' AND kind='agent'",
        UUID(created["run_id"]),
    )
    assert process[0]["state"] != "dead"


async def test_model_call_limit_abandons_one_activation_and_the_owner_is_answered_later(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240, limits={"max_model_calls_per_activation": 2})
    actors = actors_for(
        owners,
        dan=[
            {"kind": "reply", "text": "Loop please.", "next_action": errand("Out", 30)},
            {"kind": "reply", "text": "Still there?"},
        ],
    )
    models = models_for(
        owners,
        dan=[
            calls(("get_my_info", {}, "loop-1")),
            calls(("get_my_info", {}, "loop-2")),
            calls(("get_my_info", {}, "unused")),
            end_turn(text="Here.", status="finished"),
        ],
    )
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    abandoned = await events(pool, created["run_id"], "activation_abandoned")
    assert abandoned and abandoned[0]["payload"]["failure_kind"] == "task_limit"
    replies = [
        e["payload"]["content"] for e in await events(pool, created["run_id"], "owner_message")
    ]
    assert "Here." in replies


async def test_model_owner_on_a_clocked_view_uses_plain_fields(test_database_url):
    spec = scenario(("dan",), duration_minutes=100)
    view = build_actor_view(
        spec,
        "dan",
        "You've finished: Shopping.",
        source_event_id="e1",
        window=4,
        clock={"now": "Monday 09:00", "today": "Monday 9 November", "current_action": None},
    )
    reply = json.dumps(
        {
            "kind": "wait",
            "text": "",
            "next_action": {
                "description": "Make lunch",
                "minutes": 40,
                "reachable": True,
                "category": "meal",
            },
            "try_to_make_happen": ["Ring the school"],
        }
    )
    model = ScriptModel(script=[AIMessage(content=reply)])
    actor = ModelActor(model, natural=True, max_repairs=0)
    output = await actor.respond(view)
    assert output.next_action.description == "Make lunch"
    assert output.director_requests == ("Ring the school",)
    system, data = model.seen[0].content, json.loads(model.seen[1].content)
    assert "YOUR DAY." in system and "try_to_make_happen" in system
    assert "director_requests" not in system
    assert "window" not in data["view"] and "source_event_id" not in data["view"]
    assert data["view"]["clock"]["now"] == "Monday 09:00"
    # A window view keeps its schema, prompt text, and natural rule.
    plain = view.model_copy(update={"clock": None})
    model = ScriptModel(script=[AIMessage(content=json.dumps({"kind": "wait", "text": ""}))])
    with pytest.raises(ActorContractError):
        await ModelActor(model, natural=True, max_repairs=0).respond(plain)
    assert "YOUR DAY." not in model.seen[0].content
    assert "next_action" not in model.seen[0].content
    assert "window" in json.loads(model.seen[1].content)["view"]


def _saved_json(path):
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except ValueError:
        return None  # The first process is still writing it.


async def test_real_process_killed_at_a_tool_boundary_resumes_identically(
    app, pool, test_database_url, tmp_path
):
    """SIGKILL during a run. A new process resumes from PostgreSQL alone."""
    _, baseline, _, _ = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    state_path = tmp_path / "created.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--worker",
        str(state_path),
        "--database-url",
        test_database_url,
    ]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    process = await asyncio.create_subprocess_exec(
        *command, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        created = None
        while True:
            assert process.returncode is None, "The first process ended before it was killed."
            if created is None:
                created = await asyncio.to_thread(_saved_json, state_path)
            if created is not None:
                tools = await events(pool, created["run_id"], "assistant_tool_call")
                if len(tools) >= 4:
                    break
            await asyncio.sleep(0.01)
        process.send_signal(signal.SIGKILL)
    finally:
        await asyncio.wait_for(process.wait(), timeout=30)
    resumed = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(resumed.communicate(), timeout=180)
    assert resumed.returncode == 0, (stdout.decode()[-500:], stderr.decode()[-2000:])
    trace = await control.story_trace(pool, created["run_id"])
    assert control.trace_text(trace) == control.trace_text(baseline)
    mail = await rows(
        pool,
        "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s AND subject='Drill'",
        UUID(created["run_id"]),
    )
    assert mail[0]["n"] == 1


def test_control_script_compares_for_itself():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required.")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--repeats", "1"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-1000:] + done.stderr[-2000:]
    assert "byte-identical" in done.stdout


async def test_fifteen_owners_keep_one_story_for_any_latency(app, test_database_url):
    owners = tuple(f"owner-{index}" for index in range(15))

    def world(latency):
        models = {}
        actors = {}
        for index, owner in enumerate(owners):
            peer = owners[(index + 1) % 15]
            models[owner] = ScriptModel(
                latency=latency,
                script=[
                    calls(("get_my_info", {})),
                    calls(
                        (
                            "send_mail",
                            {
                                "recipient_owner_id": peer,
                                "subject": "Hello",
                                "body": f"From {owner}",
                            },
                        ),
                        ("post_thread", {"title": f"Note {index}", "body": "Hello all."}),
                    ),
                    end_turn(status="responded"),
                    calls(("list_mail", {"mailbox": "inbox"})),
                    end_turn(status="finished"),
                ],
            )
            actors[owner] = ScriptedOwner(
                [
                    {
                        "kind": "reply",
                        "text": "Say hello to the neighbours.",
                        "next_action": errand("Out", 60 + index),
                    }
                ],
                latency,
                by_spoken=True,
            )
        return models, actors

    traces = []
    for parallel, seed in ((8, 1), (3, 2)):
        generator = random.Random(seed)
        models, actors = world(lambda generator=generator: generator.random() * 0.01)
        spec = scenario(owners, duration_minutes=180)
        async with clocked_run(
            app, test_database_url, spec, models, actors, parallel=parallel, pool_size=8
        ) as (engine, created, _runtime, _agents):
            run = await engine.run(created["run_id"])
            assert run["status"] == "completed", run["result"]
            traces.append(await control.story_trace(app.state.pool, created["run_id"]))
            assert engine.gate.peak_calls_in_flight <= parallel
    assert control.trace_text(traces[0]) == control.trace_text(traces[1])
    tools = [e for e in traces[0][0] if e["kind"] == "assistant_tool_call"]
    assert len(tools) >= 45


def _clocked_files():
    found = []
    for path in sorted((ROOT / "scenarios").glob("*.json")):
        if '"clock"' in path.read_text():
            found.append(path)
    return found


@pytest.mark.parametrize("path", _clocked_files(), ids=lambda path: path.stem)
async def test_every_clocked_scenario_file_runs_scripted_to_its_end(
    path, test_database_url, tmp_path
):
    script = _run_clocked()
    report = await script.execute(
        script.parse_args(["--scenario", str(path), "--scripted"]),
        root=tmp_path,
        env={"DATABASE_URL": test_database_url},
    )
    assert report["status"] == "completed", report["result"]
    assert report["story"]["window"] == report["story"]["max_windows"]
    assert report["failures"]["simulator_error"] == 0
    assert report["failures"]["activation_abandoned"] == 0
    assert report["execution_error"] is None


def test_the_story_trace_masks_only_values_that_differ_between_runs_by_design():
    def saved(text, cursor):
        return {
            "text": text,
            "id": str(uuid4()),
            "next_cursor": cursor,
            "cursor": cursor,
            "at": "2026-11-09T06:00:00+00:00",
        }

    assert control.normalise(saved("hello", "abc=")) == control.normalise(saved("hello", "de-f"))
    assert control.normalise(saved("hello", "abc=")) != control.normalise(saved("other", "abc="))
    assert control.normalise(saved("hello", None)) != control.normalise(saved("hello", "abc="))


# Outages hold the story ----------------------------------------------------------------


async def _story_time(pool, run_id) -> int:
    found = await rows(pool, "SELECT sim_s FROM clock_state WHERE run_id=%s", UUID(str(run_id)))
    return found[0]["sim_s"]


async def _until(check, limit=20.0):
    async with asyncio.timeout(limit):
        while not check():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def test_provider_outage_holds_story_time_then_the_same_job_finishes(
    app, pool, test_database_url
):
    """R1: an outage decides nothing. It uses up no calls, and no event passes the frontier."""
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=240,
        limits={"max_model_calls_per_activation": 2},
        timeline=[
            {
                "id": "later",
                "at": "Mon 07:00",
                "recipients": ["dan"],
                "cue": "It starts to rain.",
            }
        ],
    )
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Pay the plumber."}])
    outage = {"on": True, "tries": 0}

    def flaky(messages):
        outage["tries"] += 1
        if outage["on"]:
            return ProviderCallError("Busy.", retryable=True)
        return end_turn(text="Paid him.", status="finished")

    models = {"dan": ScriptModel(script=[flaky])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        task = asyncio.create_task(engine.run(created["run_id"]))
        await _until(lambda: outage["tries"] >= 6)
        # More tries than the recovery limit, and more than this task's model calls.
        assert outage["tries"] > spec.limits.recovery_attempts + 2
        assert await _story_time(pool, created["run_id"]) < 200
        assert not await events(pool, created["run_id"], "activation_abandoned")
        outage["on"] = False
        run = await task
    assert run["status"] == "completed", run["result"]
    assert not await events(pool, created["run_id"], "activation_abandoned")
    failures = await events(pool, created["run_id"], "job_failure")
    assert len({e["payload"]["job_id"] for e in failures}) == 1
    assert {e["payload"]["failure_kind"] for e in failures} == {"provider_transient"}
    replies = [
        e["payload"]["content"] for e in await events(pool, created["run_id"], "owner_message")
    ]
    assert "Paid him." in replies


async def test_owner_turn_that_fails_from_an_outage_retries_and_is_not_a_wait(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=60)
    tries = []

    def turn(view):
        tries.append(1)
        if len(tries) <= 5:
            return ModelGenerationError("Busy.", kind="provider_transient")
        return {"kind": "reply", "text": "Now it works."}

    actors = actors_for(owners, dan=[turn])
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert len(tries) == 6
    assert not await events(pool, created["run_id"], "simulator_error")
    jobs = await rows(
        pool,
        "SELECT output FROM run_jobs WHERE run_id=%s AND kind='actor' ORDER BY clock_seq",
        UUID(created["run_id"]),
    )
    assert jobs[0]["output"]["kind"] == "reply"


@pytest.mark.parametrize("side", ["assistant", "owner"], ids=["assistant_refused", "owner_refused"])
async def test_provider_rejection_pauses_with_a_reason_and_a_resume_continues(
    app, pool, test_database_url, side
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    refused = {"on": True}

    def model_reply(messages):
        if side == "assistant" and refused["on"]:
            return ProviderCallError("Model provider returned HTTP 402.", status=402)
        return end_turn(text="Done.", status="finished")

    def turn(view):
        if side == "owner" and refused["on"]:
            return ModelGenerationError("Refused.", kind="provider_rejected")
        return {"kind": "reply", "text": "Do the thing."}

    actors = actors_for(owners, dan=[turn])
    models = {"dan": ScriptModel(script=[model_reply])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        assert run["status"] == "paused"
        assert run["result"]["reason"] == "provider_rejected"
        assert run["result"]["failure_kind"] == "provider_rejected"
        assert run["result"]["component"] == ("assistant" if side == "assistant" else "actor")
        assert await _story_time(pool, created["run_id"]) < 7200
        paused = await events(pool, created["run_id"], "run_paused")
        assert paused[0]["payload"]["reason"] == "provider_rejected"
        assert not await events(pool, created["run_id"], "activation_abandoned")
        refused["on"] = False
        await engine.director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert not await events(pool, created["run_id"], "activation_abandoned")


@pytest.mark.parametrize("always", [False, True], ids=["one_request", "every_request"])
async def test_refused_request_is_bounded_but_many_of_them_open_the_breaker(
    app, pool, test_database_url, always
):
    """A 400 concerns one request, so that activation ends. If all do, it is an outage."""
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Do the thing."}])
    models = {
        "dan": ScriptModel(
            script=[lambda messages: ProviderCallError("HTTP 400.", status=400)],
        )
    }
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        if always:
            engine.breaker_failures = 2
            engine.breaker_seconds = 0
        run = await engine.run(created["run_id"])
    if always:
        assert run["status"] == "paused"
        assert run["result"]["reason"] == "provider_outage"
        return
    assert run["status"] == "completed", run["result"]
    abandoned = await events(pool, created["run_id"], "activation_abandoned")
    assert abandoned[0]["payload"]["failure_kind"] == "provider_request_refused"
    failures = await events(pool, created["run_id"], "job_failure")
    # Each activation tries up to the limit before it ends. Only so many are possible.
    assert len(failures) >= spec.limits.recovery_attempts + 1


async def test_outage_during_a_web_search_keeps_the_call_and_builds_the_site_later(
    app, pool, test_database_url
):
    """The search retries as often as the outage needs, and the world's answer still arrives."""
    from agentrpg.director_brain import DirectorProviderError

    class Brain(ScriptedDirectorBrain):
        fails = 7

        async def site_for_search(self, run, owner_id, query, now_s):
            if Brain.fails:
                Brain.fails -= 1
                raise DirectorProviderError("Busy.", kind="provider_transient")
            return await super().site_for_search(run, owner_id, query, now_s)

    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120, director={"enabled": True})
    brain = Brain(
        {
            "site_for_search": DirectorPlan(
                sites=[
                    {
                        "site_id": "railfast",
                        "name": "Railfast",
                        "kind": "store",
                        "tagline": "Train tickets",
                        "description": "Train tickets for the coast line.",
                    }
                ]
            )
        }
    )
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Find me a train ticket."}])
    models = {
        "dan": ScriptModel(
            script=[
                calls(("web_search", {"query": "train tickets"})),
                end_turn(text="Found one.", status="finished"),
            ]
        )
    }
    async with clocked_run(app, test_database_url, spec, models, actors, brain=brain, web=True) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    assert not await events(pool, run_id, "activation_abandoned")
    # Every failure, the first one too, reads as the outage it was and holds the story.
    failures = await events(pool, run_id, "job_failure")
    assert {item["payload"]["failure_kind"] for item in failures} == {"tool_transient"}
    assert all(item["payload"]["holding"] for item in failures)
    sites = await rows(pool, "SELECT site_id FROM web_sites WHERE run_id=%s", run_id)
    assert sites == [{"site_id": "railfast"}]
    tools = await events(pool, run_id, "assistant_tool_call")
    assert "Railfast" in json.dumps(tools[0]["payload"]["result"])


async def test_circuit_breaker_pauses_a_sustained_outage_and_a_resume_continues(
    app, pool, test_database_url
):
    owners = ("dan", "mei")
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(
        owners,
        dan=[{"kind": "reply", "text": "Do the thing."}],
        mei=[{"kind": "reply", "text": "Do my thing."}],
    )
    outage = {"on": True}

    def flaky(messages):
        if outage["on"]:
            return ProviderCallError("Busy.", retryable=True)
        return end_turn(text="Done.", status="finished")

    models = {"dan": ScriptModel(script=[flaky]), "mei": ScriptModel(script=[flaky])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        engine.breaker_failures = 4
        engine.breaker_seconds = 0
        run = await engine.run(created["run_id"])
        assert run["status"] == "paused"
        assert run["result"]["reason"] == "provider_outage"
        assert run["result"]["failure_kind"] == "provider_transient"
        assert await _story_time(pool, created["run_id"]) < 300
        jobs = await rows(
            pool,
            "SELECT status FROM run_jobs WHERE run_id=%s AND kind='agent'",
            UUID(created["run_id"]),
        )
        assert {job["status"] for job in jobs} == {"running"}
        # The outage has not ended, so a resume pauses again at its first failure.
        before = len(await events(pool, created["run_id"], "job_failure"))
        engine.breaker_failures = 50
        await engine.director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
        assert run["status"] == "paused", run["result"]
        assert run["result"]["reason"] == "provider_outage"
        failures = await events(pool, created["run_id"], "job_failure")
        assert len(failures) - before <= 2  # One for each process in flight, at most.
        outage["on"] = False
        await engine.director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert not await events(pool, created["run_id"], "activation_abandoned")


class _FlakyBrain(ScriptedDirectorBrain):
    """The world's model fails as the Director's provider failures do, then answers."""

    def __init__(self, plans, failures):
        super().__init__(plans)
        self.failures = list(failures)
        self.tries = 0

    async def review(self, run, now_s, reason):
        self.tries += 1
        if self.failures:
            raise self.failures.pop(0)
        return await super().review(run, now_s, reason)


async def test_director_provider_failure_waits_with_no_canned_plan(app, pool, test_database_url):
    from agentrpg.director_brain import DirectorProviderError

    owners = ("dan",)
    spec = scenario(
        owners, duration_minutes=120, director={"enabled": True, "review_every_minutes": 60}
    )
    brain = _FlakyBrain(
        {"review": [DirectorPlan(cues=[{"owner_id": "dan", "text": "The lights flicker."}])]},
        [DirectorProviderError("Busy.", kind="provider_transient") for _ in range(4)],
    )
    actors = actors_for(owners, dan=[{"kind": "wait"}, {"kind": "wait"}])
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert brain.tries >= 5
    assert not await events(pool, created["run_id"], "director_fallback", "simulator_error")
    failures = await events(pool, created["run_id"], "job_failure")
    assert len(failures) == 4 and failures[0]["payload"]["component"] == "director"
    assert any("lights flicker" in view.cue for view in actors["dan"].views)
    # It applied at its own moment: an outage does not delay it in story time.
    applied = await events(pool, created["run_id"], "director_plan_applied")
    assert at(applied[0], spec) == 3600


async def test_director_provider_rejection_pauses_and_a_resume_answers(
    app, pool, test_database_url
):
    from agentrpg.director_brain import DirectorProviderError

    owners = ("dan",)
    spec = scenario(
        owners, duration_minutes=120, director={"enabled": True, "review_every_minutes": 60}
    )
    brain = _FlakyBrain(
        {"review": [DirectorPlan(cues=[{"owner_id": "dan", "text": "The lights flicker."}])]},
        [DirectorProviderError("Refused.", kind="provider_rejected")],
    )
    actors = actors_for(owners, dan=[{"kind": "wait"}, {"kind": "wait"}])
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        assert run["status"] == "paused"
        assert run["result"] == {
            "reason": "provider_rejected",
            "component": "director",
            "failure_kind": "provider_rejected",
        }
        assert await _story_time(pool, created["run_id"]) == 3600
        await engine.director.control(created["run_id"], "resume")
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert not await events(pool, created["run_id"], "director_fallback")
    assert any("lights flicker" in view.cue for view in actors["dan"].views)


# Failed jobs keep what they were given ---------------------------------------------------


async def test_api_blip_at_the_start_of_an_activation_retries_and_loses_nothing(
    app, pool, test_database_url
):
    import httpx

    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Please pay the plumber today."}])
    models = {"dan": ScriptModel(script=[end_turn(text="On it.", status="finished")])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):
        client = agents["dan"].board.client
        real = client.get
        faults = [httpx.ConnectError("refused"), httpx.Response(503), httpx.Response(500)]

        async def flaky_get(*args, **kwargs):
            if faults:
                fault = faults.pop(0)
                if isinstance(fault, Exception):
                    raise fault
                return fault
            return await real(*args, **kwargs)

        client.get = flaky_get
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert not faults
    assert not await events(pool, created["run_id"], "activation_abandoned")
    failures = await events(pool, created["run_id"], "job_failure")
    assert {e["payload"]["failure_kind"] for e in failures} == {"tool_transient"}
    assert any("plumber" in prompt for prompt in human_prompts(models["dan"]))


async def test_real_identity_mismatch_abandons_and_the_message_comes_back_once(
    app, pool, test_database_url
):
    import httpx

    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Please pay the plumber today."}])
    models = {"dan": ScriptModel(script=[end_turn(text="On it.", status="finished")])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):
        client = agents["dan"].board.client
        real = client.get
        wrong = httpx.Response(200, json={"role": "agent", "agent": {"id": "someone-else"}})
        mismatches = [wrong]

        async def get(*args, **kwargs):
            return mismatches.pop(0) if mismatches else await real(*args, **kwargs)

        client.get = get
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    abandoned = await events(pool, created["run_id"], "activation_abandoned")
    assert [e["payload"]["failure_kind"] for e in abandoned] == ["identity_or_input_conflict"]
    assert any("plumber" in prompt for prompt in human_prompts(models["dan"]))
    jobs = await rows(
        pool,
        "SELECT status,output_meta FROM run_jobs WHERE run_id=%s AND kind='agent' "
        "ORDER BY clock_seq",
        UUID(created["run_id"]),
    )
    assert jobs[0]["output_meta"]["abandoned"] and len(jobs[0]["output_meta"]["redelivered"]) == 1
    assert jobs[1]["status"] == "completed"


async def test_owner_failure_shows_a_cue_once_more_and_cannot_loop(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=240,
        timeline=[
            {
                "id": "dentist",
                "at": "Mon 07:00",
                "recipients": ["dan"],
                "cue": "Dentist moved to 15:00 today.",
            }
        ],
    )
    seen = []

    def turn(view):
        seen.append(view.cue)
        return ActorContractError("That is not this person's role.")

    actors = actors_for(owners, dan=[{"kind": "wait"}, turn])
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    # The job that failed, then the job that got it again. Then the failure stays recorded.
    assert sum("Dentist" in cue for cue in seen) == 2
    errors = await events(pool, created["run_id"], "simulator_error")
    assert [e["payload"]["reason"] for e in errors] == ["contract", "contract"]


# Logic errors and bad text -------------------------------------------------------------


async def test_event_that_cannot_be_handled_is_recorded_and_the_run_passes_it(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=120,
        timeline=[
            {"id": "poison", "at": "Mon 06:30", "recipients": ["dan"], "cue": "Never delivered."}
        ],
    )
    actors = actors_for(owners, dan=[{"kind": "wait"}])
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):

        async def poison(ctx, event, now_s):
            raise TypeError("'NoneType' object is not iterable")

        engine._on_beat = poison
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    errors = await events(pool, created["run_id"], "simulator_error")
    assert [e["payload"]["reason"] for e in errors] == ["event_failed"]
    assert errors[0]["payload"]["key"] == "beat:poison"
    assert errors[0]["payload"]["failure"] == "TypeError"
    event = await rows(
        pool,
        "SELECT status FROM clock_events WHERE run_id=%s AND key='beat:poison'",
        UUID(created["run_id"]),
    )
    assert event == [{"status": "done"}]


@pytest.mark.parametrize("handler", ["_prepare_actor", "_apply_actor"], ids=["wake", "effect"])
async def test_failed_wake_or_result_rests_the_owner_and_does_not_loop(
    app, pool, test_database_url, handler
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=240)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Hello."}, {"kind": "wait"}])
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        real = getattr(engine, handler)
        failed = []

        async def broken(*args, **kwargs):
            if not failed:
                failed.append(1)
                raise TypeError("A logic error.")
            return await real(*args, **kwargs)

        setattr(engine, handler, broken)
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    errors = await events(pool, run_id, "simulator_error")
    assert [e["payload"]["reason"] for e in errors] == ["event_failed"]
    # It failed once, then the owner woke at the next daily wake, which is 08:00.
    jobs = await rows(
        pool,
        "SELECT start_s FROM run_jobs WHERE run_id=%s AND kind='actor' ORDER BY clock_seq",
        run_id,
    )
    assert jobs[-1]["start_s"] == 7200


async def test_database_failure_propagates_and_the_event_runs_again(app, pool, test_database_url):
    import psycopg

    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=120,
        timeline=[{"id": "held", "at": "Mon 06:30", "recipients": ["dan"], "cue": "It rains."}],
    )
    actors = actors_for(owners, dan=[{"kind": "wait"}, {"kind": "wait"}])
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        real = engine._on_beat
        tries = []

        async def lost_connection(ctx, event, now_s):
            tries.append(1)
            if len(tries) == 1:
                raise psycopg.OperationalError("The connection dropped.")
            await real(ctx, event, now_s)

        engine._on_beat = lost_connection
        await engine.director.control(created["run_id"], "start")
        with pytest.raises(psycopg.OperationalError):
            await engine.advance(created["run_id"])
        queued = await rows(
            pool,
            "SELECT status FROM clock_events WHERE run_id=%s AND key='beat:held'",
            UUID(created["run_id"]),
        )
        assert queued == [{"status": "queued"}]
        tries.clear()
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert not await events(pool, created["run_id"], "simulator_error")
    assert any("It rains." in view.cue for view in actors["dan"].views)


async def test_nul_in_model_text_is_removed_before_saving(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Hello\x00 there."}])
    models = {
        "dan": ScriptModel(
            script=[
                calls(("send_owner_message", {"content": "One\x00 moment."})),
                end_turn(text="All\x00 done.", status="finished"),
            ]
        )
    }
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    assert not await events(pool, created["run_id"], "simulator_error", "activation_abandoned")
    said = [e["payload"]["content"] for e in await events(pool, created["run_id"], "owner_message")]
    assert "Hello there." in said and "All done." in said
    # The tool refuses the text or saves it clean. Neither stops the run or breaks a save.
    tools = await events(pool, created["run_id"], "assistant_tool_call")
    assert all("\\u0000" not in json.dumps(e["payload"]) for e in tools)


# A world change while the owner is typing ----------------------------------------------


def _in_flight_case(replace, *, reachable, seeks=None):
    """A mild draw that fires inside a long second reply, whose next_action wants lunch."""
    key = "action:dan:1"
    level = "high"
    seed = _seed_with_outcome(key, level, "mild")
    result = drama.draw(seed, key, level)
    first_text = "Off for a walk."
    first_end = 45 + -(-len(first_text) * 60 // 300)
    fire = drama.fire_s(first_end, 3600, result.fire_fraction)
    typing_from = (fire - 120) // 60  # minutes after 06:00, on a whole minute
    spec = scenario(
        ("dan",),
        seed=seed,
        duration_minutes=600,
        epoch_minutes=60,
        drama=level,
        director={"enabled": True, "review_every_minutes": 14400},
        timeline=[
            {
                "id": "nudge",
                "at": f"Mon {6 + typing_from // 60:02d}:{typing_from % 60:02d}",
                "recipients": ["dan"],
                "cue": "A friend waves at you.",
            }
        ],
    )
    long_reply = "I'll go for lunch once this is done. " * 60  # About seven minutes to write.
    brain = ScriptedDirectorBrain(
        {
            "perturb": DirectorPlan(
                replace_action={
                    "description": replace,
                    "minutes": 60,
                    "reachable": reachable,
                    "category": "errand",
                },
                cues=[{"owner_id": "dan", "text": f"Something came up: {replace}."}],
            )
        }
    )
    actors = actors_for(
        ("dan",),
        dan=[
            {
                "kind": "reply",
                "text": first_text,
                "next_action": errand("A walk", 60, reachable=True, seeks=seeks),
            },
            {"kind": "reply", "text": long_reply, "next_action": errand("Lunch at the cafe", 45)},
            {"kind": "wait"},
            {"kind": "wait"},
            {"kind": "wait"},
        ],
    )
    return spec, brain, actors, fire, long_reply


@pytest.mark.parametrize("reachable", [True, False], ids=["reachable", "cut_off"])
async def test_reply_in_flight_does_not_undo_the_world_and_no_second_job_starts(
    app, pool, test_database_url, reachable
):
    owners = ("dan",)
    spec, brain, actors, fire, long_reply = _in_flight_case("Waiting at A&E", reachable=reachable)
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    assert [call[0] for call in brain.calls][0] == "perturb"
    now_s = brain.calls[0][-1]
    assert now_s == fire
    jobs = await rows(
        pool,
        "SELECT clock_seq,start_s,end_s FROM run_jobs WHERE run_id=%s AND kind='actor' "
        "ORDER BY clock_seq",
        run_id,
    )
    # The reply was being written when the world changed. Nothing overlaps it.
    assert jobs[1]["start_s"] < fire < jobs[1]["end_s"]
    for earlier, later in zip(jobs, jobs[1:], strict=False):
        assert later["start_s"] >= earlier["end_s"]
    # Its words still reach the assistant.
    said = [e["payload"]["content"] for e in await events(pool, run_id, "owner_message")]
    assert long_reply.strip() in said
    actions = await rows(
        pool,
        "SELECT key,label,status,start_s,duration_s FROM clock_actions "
        "WHERE run_id=%s AND process='actor' AND detail->>'default' IS NULL ORDER BY start_s",
        run_id,
    )
    assert [a["label"] for a in actions] == ["A walk", "Waiting at A&E", "Lunch at the cafe"]
    world = actions[1]
    assert world["start_s"] == fire
    # The reply did not end it. What the owner asked for began when it was over.
    assert world["status"] == "done"
    assert actions[2]["start_s"] == fire + 3600
    assert actions[2]["key"] == "action:dan:2"
    views = actors["dan"].views
    told = next(view.cue for view in views if "has to finish first" in view.cue)
    assert "Lunch at the cafe will have to wait" in told and "Waiting at A&E" in told
    assert any("Now for: Lunch at the cafe" in view.cue for view in views)
    assert not FORBIDDEN.search(told)


async def test_replacement_keeps_what_the_action_was_seeking(app, pool, test_database_url):
    owners = ("dan",)
    spec, brain, actors, _fire, _reply = _in_flight_case(
        "Waiting at A&E", reachable=True, seeks="get the 15 dollars for the garage"
    )
    brain.plans["respond_to_request"] = [
        DirectorPlan(balance=[{"owner_id": "dan", "delta": 15, "memo": "Garage job"}])
    ]
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    asked = [call for call in brain.calls if call[0] == "respond_to_request"]
    assert asked and asked[0][3] == "get the 15 dollars for the garage"
    balances = await rows(
        pool,
        "SELECT balance FROM world_accounts WHERE run_id=%s AND owner_id='dan'",
        UUID(created["run_id"]),
    )
    assert balances == [{"balance": 115}]


# Rates ---------------------------------------------------------------------------------


async def test_assistants_answering_each_other_slow_down_to_ambient_news(
    app, pool, test_database_url
):
    owners = ("dan", "mei")
    spec = scenario(owners, duration_minutes=240)

    def chatter(other):
        script = []
        for index in range(60):
            script.append(
                calls(
                    (
                        "direct_message",
                        {"to": [other], "subject": "Friday", "body": f"Note {index} to {other}."},
                    )
                )
            )
            script.append(end_turn(status="finished"))
        return script

    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Ask Mei about Friday."}])
    models = {
        "dan": ScriptModel(script=chatter("mei")),
        "mei": ScriptModel(script=chatter("dan")),
    }
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    limited = await events(pool, run_id, "process_peer_limit")
    assert {e["owner_id"] for e in limited} == {"dan", "mei"}
    for owner in owners:
        jobs = await rows(
            pool,
            "SELECT start_s FROM run_jobs WHERE run_id=%s AND owner_id=%s AND kind='agent' "
            "ORDER BY clock_seq",
            run_id,
            owner,
        )
        starts = [job["start_s"] for job in jobs]
        # At the old rate this would be about 70 wakes in four hours.
        assert len(starts) <= 12
        late = [b - a for a, b in zip(starts[4:], starts[5:], strict=False)]
        assert late and max(late) >= 2700 - 120


async def test_daily_quota_rests_a_process_until_the_next_daily_wake(app, pool, test_database_url):
    owners = ("dan",)
    # One activation a day. Two messages arrive before 08:00, so the second waits for it.
    spec = scenario(
        owners,
        duration_minutes=2880,
        epoch_minutes=120,
        limits={"max_agent_activations_per_owner": 2},
    )
    actors = actors_for(
        owners,
        dan=[
            {"kind": "reply", "text": "First.", "next_action": errand("Quick walk", 30)},
            {"kind": "reply", "text": "Second."},
        ],
    )
    models = {"dan": ScriptModel(script=[end_turn(text="Yes.", status="finished")])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    limited = await events(pool, run_id, "process_limit_reached")
    assert limited[0]["payload"]["process"] == "agent"
    assert limited[0]["payload"]["daily_quota"] == 1
    assert limited[0]["payload"]["until_s"] == 7200
    jobs = await rows(
        pool,
        "SELECT start_s,input FROM run_jobs WHERE run_id=%s AND kind='agent' ORDER BY clock_seq",
        run_id,
    )
    assert jobs[1]["start_s"] == 7200
    assert any(m["content"] == "Second." for m in jobs[1]["input"]["owner_messages"])
    process = await rows(
        pool,
        "SELECT state FROM clock_processes WHERE run_id=%s AND kind='agent'",
        run_id,
    )
    assert process[0]["state"] != "offline"


async def _running(engine, created) -> object:
    await engine.director.control(created["run_id"], "start")
    return await engine._load(UUID(created["run_id"]), None)


async def test_chat_streak_counts_only_what_does_something(app, pool, test_database_url):
    from agentrpg.director import emit

    owners = ("dan",)
    spec = scenario(owners, duration_minutes=120)
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors_for(owners)
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        ctx = await _running(engine, created)
        run_id = ctx.id
        counter = iter(range(1000))

        async def add(kind, payload):
            async with pool.connection() as conn:
                await emit(conn, run_id, kind, 0, payload, f"test:{next(counter)}", "dan")

        async def capped():
            async with pool.connection() as conn:
                return await engine._chat_capped(conn, ctx, "dan")

        async def owner_says_twice():
            await add("owner_message", {"source": "owner", "content": "Thanks."})
            await add("owner_message", {"source": "owner", "content": "Thanks again."})

        def tool(name, ok=True, **arguments):
            return {"tool": name, "result": {"ok": ok}, "arguments": arguments}

        await owner_says_twice()
        assert await capped()
        # Reads, refused calls and form pages that only read do nothing for the owner.
        await add("assistant_tool_call", tool("inspect_world"))
        await add("assistant_tool_call", tool("web_search"))
        await add("assistant_tool_call", tool("open_page"))
        await add("assistant_tool_call", tool("send_mail", ok=False))
        await add("assistant_tool_call", tool("submit_form", action="view_cart"))
        assert await capped()
        # The end of an action and a note read later do not start the count again.
        await add("actor_cue", {"cue": "You've finished: A walk.", "action_key": "action:dan:1"})
        await add("actor_cue", {"cue": "Lunch will have to wait.", "quiet": True})
        assert await capped()
        # Each of these does.
        for effect in (
            tool("send_mail"),
            tool("submit_form", action="checkout"),
            tool("create_resource"),
            tool("submit_world_action"),
            tool("submit_market_action"),
            tool("transfer_credits"),
            tool("post_thread"),
            tool("reply"),
            tool("direct_message"),
            tool("write_resource"),
        ):
            await add("assistant_tool_call", effect)
            assert not await capped(), effect
            await owner_says_twice()
            assert await capped()
        await add("actor_cue", {"cue": "It starts to rain.", "source": "timeline"})
        assert not await capped()
        await owner_says_twice()
        await add("actor_cue", {"cue": "The boiler breaks."})
        assert not await capped()


async def test_chat_cap_still_lets_mail_wake_the_owner(app, pool, test_database_url):
    from agentrpg.director import emit

    owners = ("dan",)
    spec = scenario(owners, duration_minutes=2880, epoch_minutes=120)
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors_for(owners)
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        ctx = await _running(engine, created)
        run_id = ctx.id
        async with pool.connection() as conn:
            await conn.execute("UPDATE clock_state SET sim_s=600 WHERE run_id=%s", (run_id,))
            for index in range(2):
                await emit(
                    conn,
                    run_id,
                    "owner_message",
                    0,
                    {"source": "owner", "content": "Thanks."},
                    f"said:{index}",
                    "dan",
                )
            await emit(
                conn,
                run_id,
                "owner_message",
                0,
                {"source": "agent", "content": "You're welcome."},
                "answer",
                "dan",
            )

        async def wake() -> int:
            await engine._reconcile(ctx)
            found = await rows(
                pool,
                "SELECT due_s FROM clock_events WHERE run_id=%s AND kind='actor_wake' "
                "AND status='queued'",
                run_id,
            )
            return found[0]["due_s"]

        assert await wake() == 7200  # Chat waits for the morning.
        async with pool.connection() as conn:
            await emit(
                conn,
                run_id,
                "inbox_received",
                0,
                {"message_id": str(uuid4()), "sender_owner_id": None, "subject": "Rent is due"},
                "mail",
                "dan",
            )
        assert await wake() <= 600 + 2400  # Mail gets noticed as it always did.


async def test_default_idle_ends_quietly_and_the_owner_rests_until_the_morning(
    app, pool, test_database_url
):
    owners = ("dan",)
    spec = scenario(owners, duration_minutes=1620, epoch_minutes=60)
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Hello."}, {"kind": "wait"}])
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    views = actors["dan"].views
    assert views[1].clock["now"].startswith("Tuesday 10 November, 08:00")
    assert "You have a moment" in views[1].cue
    cues = await events(pool, run_id, "actor_cue")
    assert not [e for e in cues if "Nothing planned" in e["payload"]["cue"]]
    idle = await rows(
        pool,
        "SELECT status FROM clock_actions WHERE run_id=%s AND detail->>'default'='true' "
        "ORDER BY start_s",
        run_id,
    )
    assert idle[0]["status"] == "done"


def test_cues_carry_their_times_and_a_long_freeze_keeps_only_what_still_matters():
    from datetime import timedelta
    from types import SimpleNamespace

    from agentrpg.clock import ClockConfig
    from agentrpg.clock_engine import ClockEngine

    start = datetime.fromisoformat("2026-11-09T06:00:00+00:00")
    config = ClockConfig(seed=1, start_at=start, duration_s=604800, epoch_s=7200)
    ctx = SimpleNamespace(config=config)

    def cue(hours, text, **payload):
        return {
            "created_at": start + timedelta(hours=hours),
            "payload": {"cue": text, **payload},
        }

    # A short gap: everything stays, and each line says when.
    recent = [cue(2, "Rent is tomorrow.", source="timeline"), cue(3, "You've finished: Work.")]
    text = ClockEngine._cue_text(ctx, recent, recent, 4 * 3600, 3600)
    assert text == (
        "Monday 9 November, 08:00: Rent is tomorrow.\n"
        "Monday 9 November, 09:00: You've finished: Work."
    )
    # Three days in hospital: old plans are gone. The crash and the newest plan stay.
    frozen = [
        cue(1, "You crashed the car."),
        cue(26, "You're at the garage from 8 till 11.", source="timeline"),
        cue(50, "Shift at the shop, 8 till 3.", source="timeline"),
        cue(72, "Rent is due today.", source="timeline"),
        cue(73, "You've finished: Hospital."),
    ]
    text = ClockEngine._cue_text(ctx, frozen, frozen, 73 * 3600, 3600)
    lines = text.split("\n")
    assert lines[0] == "About 3 days have gone by since you last had a moment to look at anything."
    assert "You crashed the car." in lines[1] and lines[1].startswith("Monday 9 November, 07:00")
    assert "garage" not in text and "Shift" not in text
    assert "Rent is due today." in lines[2] and "You've finished: Hospital." in lines[3]
    assert len(lines) == 4
    assert not FORBIDDEN.search(text)


# Fired and drawn -----------------------------------------------------------------------


async def test_fired_means_popped_and_a_replaced_perturbation_is_not_cancelled(
    app, pool, test_database_url
):
    from agentrpg.console_api import interference

    owners = ("dan",)
    key = "action:dan:1"
    seed = _seed_with_outcome(key, "high", "mild")
    spec = scenario(
        owners,
        seed=seed,
        duration_minutes=480,
        epoch_minutes=60,
        drama="high",
        director={"enabled": True, "review_every_minutes": 1440},
    )
    brain = ScriptedDirectorBrain(
        {
            "perturb": DirectorPlan(
                replace_action={
                    "description": "Waiting for a replacement bus",
                    "minutes": 30,
                    "reachable": False,
                    "category": "errand",
                },
                cues=[{"owner_id": "dan", "text": "Your bus has broken down."}],
            )
        }
    )
    actors = actors_for(
        owners, dan=[{"kind": "reply", "text": "Off.", "next_action": errand("Bus", 60)}]
    )
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    run_id = UUID(created["run_id"])
    event = await rows(
        pool,
        "SELECT status,popped_seq FROM clock_events WHERE run_id=%s AND key=%s",
        run_id,
        f"perturb:{key}",
    )
    assert event[0]["status"] == "done" and event[0]["popped_seq"] is not None
    async with pool.connection() as conn:
        measured = await interference(conn, run_id, 0.13)
    assert measured["interfered"] == measured["fired"] == measured["with_effect"] == 1


async def test_drawn_perturbation_that_never_happens_is_not_counted_as_fired(
    app, pool, test_database_url
):
    from agentrpg.console_api import interference

    owners = ("dan",)
    level = "high"
    seed = next(
        seed
        for seed in range(50000)
        if drama.draw(seed, "action:dan:1", level).outcome == "extreme"
        and drama.draw(seed, "action:dan:2", level).outcome == "none"
    )
    spec = scenario(
        owners,
        seed=seed,
        duration_minutes=480,
        epoch_minutes=60,
        drama=level,
        director={"enabled": True, "review_every_minutes": 1440},
        timeline=[
            {"id": "call", "at": "Mon 06:05", "recipients": ["dan"], "cue": "Your phone rings."}
        ],
    )
    brain = ScriptedDirectorBrain()
    actors = actors_for(
        owners,
        dan=[
            {"kind": "reply", "text": "Off.", "next_action": errand("Bus", 60, reachable=True)},
            {"kind": "reply", "text": "Change of plan.", "next_action": errand("Home", 30)},
        ],
    )
    async with clocked_run(
        app, test_database_url, spec, models_for(owners), actors, brain=brain
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    run_id = UUID(created["run_id"])
    assert not [call for call in brain.calls if call[0] == "perturb"]
    event = await rows(
        pool,
        "SELECT status,popped_seq FROM clock_events WHERE run_id=%s AND key='perturb:action:dan:1'",
        run_id,
    )
    assert event == [{"status": "cancelled", "popped_seq": None}]
    async with pool.connection() as conn:
        measured = await interference(conn, run_id, 0.13)
    assert measured["extreme"] == measured["interfered"] == 1
    assert measured["fired"] == measured["with_effect"] == 0


# Expiry and resume ---------------------------------------------------------------------


async def test_expiry_silences_an_answer_that_has_not_landed_yet(app, pool, test_database_url):
    """The model answered fast. Its words land after the deadline, so they never land."""
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=240,
        sites=[
            {
                "site_id": "agentcare",
                "name": "Agent Care",
                "kind": "portal",
                "bills": [
                    {"id": "plan", "owner_id": "dan", "title": "Assistant plan", "amount": 50}
                ],
            }
        ],
        lifecycle=[
            {
                "kind": "assistant_subscription",
                "owner_id": "dan",
                "site_id": "agentcare",
                "bill_id": "plan",
                "due_at": "Mon 07:00",
                "amount": 50,
            }
        ],
        timeline=[
            {
                "id": "letter",
                "at": "Mon 06:59",
                "mail": [
                    {
                        "id": "note",
                        "recipient_owner_id": "dan",
                        "subject": "A letter",
                        "body": "Please read this.",
                    }
                ],
            }
        ],
    )
    actors = actors_for(owners, dan=[{"kind": "wait"}, {"kind": "wait"}])
    # About 90 seconds to write, from 06:59, so it ends after the deadline at 07:00.
    models = {"dan": ScriptModel(script=[end_turn(text="Noted. " * 1300, status="responded")])}
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    expired = await events(pool, run_id, "assistant_expired")
    assert at(expired[0], spec) == 3600
    assert not [
        e for e in await events(pool, run_id, "owner_message") if e["payload"]["source"] == "agent"
    ]
    assert not await events(pool, run_id, "agent_activation")
    jobs = await rows(pool, "SELECT status FROM run_jobs WHERE run_id=%s AND kind='agent'", run_id)
    assert [job["status"] for job in jobs] == ["failed"]
    finish = await rows(
        pool,
        "SELECT status FROM clock_events WHERE run_id=%s AND kind='agent_finish'",
        run_id,
    )
    assert [item["status"] for item in finish] == ["cancelled"]


async def test_crash_during_a_tool_blocks_later_events_until_it_runs_again(
    app, pool, test_database_url
):
    """The bar moved past the call before it ran. Resume must block from where it began."""
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=120,
        tool_seconds={"list_mail": 600},
        timeline=[
            {
                "id": "letter",
                "at": "Mon 06:04",
                "mail": [
                    {
                        "id": "note",
                        "recipient_owner_id": "dan",
                        "subject": "Landed after the read",
                        "body": "Too late for that read.",
                    }
                ],
            }
        ],
    )
    actors = actors_for(owners, dan=[{"kind": "reply", "text": "Read my mail."}])
    models = {
        "dan": ScriptModel(
            script=[calls(("list_mail", {})), end_turn(text="Nothing yet.", status="responded")]
        )
    }
    reached = asyncio.Event()
    async with clocked_run(app, test_database_url, spec, models, actors) as (
        engine,
        created,
        _runtime,
        agents,
    ):

        async def hang(name, arguments, call_id):
            reached.set()
            await asyncio.Event().wait()

        agents["dan"].environment.execute = hang
        await engine.director.control(created["run_id"], "start")
        task = asyncio.create_task(engine.advance(created["run_id"]))
        await asyncio.wait_for(reached.wait(), 20)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    run_id = UUID(created["run_id"])
    processes = await rows(
        pool,
        "SELECT bar_s FROM clock_processes WHERE run_id=%s AND owner_id='dan' AND kind='agent'",
        run_id,
    )
    assert processes[0]["bar_s"] > 240  # A resume from this bar would let the mail land first.
    async with clocked_run(app, test_database_url, spec, models, actors, created=created) as (
        engine,
        _created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    tools = await events(pool, run_id, "assistant_tool_call")
    assert tools[0]["payload"]["tool"] == "list_mail" and at(tools[0], spec) < 240
    assert tools[0]["payload"]["result"]["data"]["items"] == []
    assert not [
        e
        for e in await events(pool, run_id, "simulator_error")
        if e["payload"]["reason"] == "tool_after_its_stamp"
    ]


# Online approvals and timeline money -----------------------------------------------------


async def test_online_approval_counts_only_when_more_room_is_needed(app, pool, test_database_url):
    owners = ("dan",)
    spec = scenario(
        owners,
        duration_minutes=240,
        online_allowance={"dan": 100},
        timeline=[
            {"id": "one", "at": "Mon 06:30", "recipients": ["dan"], "cue": "First thought."},
            {"id": "two", "at": "Mon 07:00", "recipients": ["dan"], "cue": "Second thought."},
        ],
    )
    actors = actors_for(
        owners,
        dan=[
            {"kind": "reply", "text": "You may spend 100 online.", "online_spend_approval": 100},
            {"kind": "reply", "text": "All good.", "online_spend_approval": 40},
            {"kind": "reply", "text": "Go up to 150.", "online_spend_approval": 150},
        ],
    )
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    ignored = await events(pool, run_id, "web_spend_approval_ignored")
    assert [(e["payload"]["amount"], e["payload"]["reason"]) for e in ignored] == [
        (100, "opening_request"),
        (40, "already_covered"),
    ]
    approved = await rows(pool, "SELECT amount FROM wallet_approvals WHERE run_id=%s", run_id)
    assert approved == [{"amount": 150}]


async def test_timeline_money_lands_once_through_the_ledger(app, pool, test_database_url):
    owners = ("dan",)
    item = {
        "id": "pension",
        "at": "Mon 07:00",
        "balance": [{"owner_id": "dan", "delta": 410, "memo": "State pension"}],
    }
    spec = scenario(owners, duration_minutes=120, timeline=[item])
    actors = actors_for(owners, dan=[{"kind": "wait"}])
    async with clocked_run(app, test_database_url, spec, models_for(owners), actors) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        # A repeat after a crash changes nothing.
        beat = await rows(
            pool,
            "SELECT * FROM clock_events WHERE run_id=%s AND key='beat:pension'",
            UUID(created["run_id"]),
        )
        assert beat[0]["status"] == "done"
    assert run["status"] == "completed", run["result"]
    run_id = UUID(created["run_id"])
    ledger = await rows(
        pool,
        "SELECT delta,memo,sim_s,operation_key FROM wallet_ledger WHERE run_id=%s",
        run_id,
    )
    assert ledger == [
        {
            "delta": 410,
            "memo": "State pension",
            "sim_s": 3600,
            "operation_key": "timeline-balance:pension:0",
        }
    ]
    balance = await rows(pool, "SELECT balance FROM world_accounts WHERE run_id=%s", run_id)
    assert balance == [{"balance": 510}]


def test_timeline_money_stays_out_of_the_hash_when_empty():
    plain = {"id": "rain", "at": "Mon 07:00", "recipients": ["dan"], "cue": "It rains."}
    spec = scenario(("dan",), timeline=[plain])
    assert '"balance"' not in spec.canonical_json()
    assert spec.clock.timeline[0].balance == ()
    moneyed = scenario(
        ("dan",),
        timeline=[{**plain, "balance": [{"owner_id": "dan", "delta": 5, "memo": "Tip"}]}],
    )
    assert '"balance"' in moneyed.canonical_json()
    assert moneyed.content_hash() != spec.content_hash()
    only_money = {
        "id": "pay",
        "at": "Mon 07:00",
        "balance": [{"owner_id": "dan", "delta": 5, "memo": "Tip"}],
    }
    assert scenario(("dan",), timeline=[only_money]).clock.timeline[0].cue is None
    with pytest.raises(ValueError, match="unknown owner"):
        scenario(
            ("dan",),
            timeline=[{**only_money, "balance": [{"owner_id": "x", "delta": 5, "memo": "Tip"}]}],
        )
    with pytest.raises(ValueError, match="not be zero"):
        scenario(
            ("dan",),
            timeline=[{**only_money, "balance": [{"owner_id": "dan", "delta": 0, "memo": "Tip"}]}],
        )


# The run script ------------------------------------------------------------------------


async def test_run_script_seed_is_frozen_and_usage_comes_from_the_ledger(
    pool, test_database_url, tmp_path
):
    script = _run_clocked()
    env = {"DATABASE_URL": test_database_url}
    args = [
        "--scenario",
        str(ROOT / "scenarios" / "agent_bill_week.json"),
        "--scripted",
        "--advance-minutes",
        "30",
        "--drama",
        "off",
        "--seed",
        "424242",
    ]
    first = await script.execute(script.parse_args(args), root=tmp_path, env=env)
    assert first["seed"] == 424242
    manifest = next((tmp_path / ".local/scenario-runs").glob("*/credentials.json"))
    run_id = UUID(first["run_id"])
    saved = await rows(pool, "SELECT seed,scenario FROM environment_runs WHERE id=%s", run_id)
    assert saved[0]["seed"] == 424242 and saved[0]["scenario"]["seed"] == 424242
    with pytest.raises(script.ScenarioRunError, match="frozen seed"):
        await script.execute(
            script.parse_args(["--resume", str(manifest), "--seed", "1"]), root=tmp_path, env=env
        )
    assert first["usage"]["requests"] == 0 and first["usage"]["by_role"] == {}
    from agentrpg.budget import CampaignLedger

    campaign = uuid4()
    await CampaignLedger(pool).ensure_campaign(campaign, "5")
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO model_spend_attempts(id,campaign_id,run_id,owner_id,role,model,
            reserved_usd,reported_usd,status,prompt_token_bound,output_token_bound,price_snapshot)
            VALUES(%s,%s,%s,'dan','assistant','m',1,0.25,'reported',1,1,'{}'),
            (%s,%s,%s,'dan','assistant','m',1,0,'reported',1,1,'{}'),
            (%s,%s,%s,'dan','actor','m',0.5,NULL,'uncertain',1,1,'{}')""",
            (uuid4(), campaign, run_id, uuid4(), campaign, run_id, uuid4(), campaign, run_id),
        )
    usage = await script.run_usage(pool, run_id)
    assert usage["requests"] == 3
    assert float(usage["reported_cost_usd"]) == 0.25 and float(usage["held_usd"]) == 0.5
    assert usage["by_role"]["assistant"]["attempts"] == 2
    assert usage["by_role"]["assistant"]["refused"] == 1
    assert usage["by_role"]["actor"]["unconfirmed"] == 1


async def test_paid_run_needs_a_stated_ceiling_and_cannot_share_one_silently(
    pool, test_database_url, tmp_path
):
    script = _run_clocked()
    scenario_file = str(ROOT / "scenarios" / "agent_bill_week.json")
    env = {
        "DATABASE_URL": test_database_url,
        "ADMIN_TOKEN": "local-test-value",
        "OPENROUTER_API_KEY": "not-a-real-value",
    }
    for extra in (["--budget-usd", "5"], ["--campaign", str(uuid4())], []):
        with pytest.raises(script.ScenarioRunError, match="--budget-usd and --campaign"):
            await script.execute(
                script.parse_args(["--scenario", scenario_file, *extra]), root=tmp_path, env=env
            )
    from agentrpg.director import Director

    director = Director(pool)
    spec = scenario(("dan",), duration_minutes=60)
    first = (await director.create_run(spec))["run_id"]
    second = (await director.create_run(spec))["run_id"]
    budget = {"campaign_id": str(uuid4()), "limit_usd": "5"}
    assert await script.bind_campaign(pool, UUID(first), budget, share=False) == []
    with pytest.raises(script.ScenarioRunError, match="--share-campaign"):
        await script.bind_campaign(pool, UUID(second), budget, share=False)
    # The first run keeps its ceiling on a resume, and sharing is possible when it is asked for.
    assert await script.bind_campaign(pool, UUID(first), budget, share=False) == []
    assert await script.bind_campaign(pool, UUID(second), budget, share=True) == [first]
    assert await script.bind_campaign(pool, UUID(first), budget, share=False) == [second]


def test_exit_codes_tell_an_outer_loop_what_to_do():
    script = _run_clocked()
    code = script.exit_code
    assert code({"status": "completed", "result": {"reason": "horizon"}}) == 0
    assert code({"status": "paused", "result": {"reason": "advance_limit"}}) == 0
    assert code({"status": "paused", "result": {"reason": "operator_pause"}}) == 0
    assert code({"status": "paused", "result": {"reason": "budget"}}) == 3
    assert code({"status": "paused", "result": {"reason": "provider_rejected"}}) == 75
    assert code({"status": "paused", "result": {"reason": "provider_outage"}}) == 75
    assert code({"status": "incomplete", "result": {"reason": "clock_queue_empty"}}) == 1


async def test_export_reports_drawn_fired_and_with_effect(
    app, pool, test_database_url, tmp_path, monkeypatch
):
    path = ROOT / "scripts" / "export_run_evidence.py"
    module_spec = importlib.util.spec_from_file_location("export_run_evidence", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    owners = ("dan",)
    key = "action:dan:1"
    seed = _seed_with_outcome(key, "high", "mild")
    spec = scenario(
        owners,
        seed=seed,
        duration_minutes=480,
        epoch_minutes=60,
        drama="high",
        director={"enabled": True, "review_every_minutes": 1440},
    )
    brain = ScriptedDirectorBrain({"perturb": DirectorPlan(note="Nothing came of it.")})
    actors = actors_for(
        owners, dan=[{"kind": "reply", "text": "Off.", "next_action": errand("Bus", 60)}]
    )
    models = models_for(owners)
    async with clocked_run(app, test_database_url, spec, models, actors, brain=brain) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        await engine.run(created["run_id"])
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    summary = await module.export(UUID(created["run_id"]), tmp_path)
    counts = summary["counts"]
    assert counts["drawn_interventions"] == 1 and counts["interventions"] == 1
    # The plan held only a note, so nothing about the owner's day changed.
    assert counts["interventions_with_effect"] == 0


def test_run_script_main_exits_with_the_code_of_its_report(monkeypatch, capsys):
    script = _run_clocked()
    report = {"status": "paused", "result": {"reason": "provider_outage"}}

    async def paused(args):
        return report

    async def crashed(args):
        raise RuntimeError("The database went away.")

    arguments = ["--resume", "unused.json"]
    monkeypatch.setattr(script, "execute", paused)
    with pytest.raises(SystemExit) as stopped:
        script.main(arguments)
    assert stopped.value.code == 75
    report["result"] = {"reason": "budget"}
    with pytest.raises(SystemExit) as stopped:
        script.main(arguments)
    assert stopped.value.code == 3
    report.update(status="completed", result={"reason": "horizon"})
    script.main(arguments)
    # A crash is a failure too. Any non-zero code tells an outer loop to try --resume.
    monkeypatch.setattr(script, "execute", crashed)
    with pytest.raises(SystemExit) as stopped:
        script.main(arguments)
    assert stopped.value.code not in {0, None}


async def test_paid_path_of_the_run_script_with_fake_models(
    app, pool, test_database_url, tmp_path, monkeypatch
):
    """The real paid branch: a stated ceiling, a bound campaign, a report and a resume."""
    import httpx

    from agentrpg.budget import ModelPrice

    script = _run_clocked()

    class FakeModel(ScriptModel):
        async def aclose(self):
            return None

    async def prices(models):
        return {
            name: ModelPrice(name, "0.000002", "0.000008", "0", 1047576, "2026-11-04T00:00:00Z")
            for name in models
        }

    retries = []

    def make_model(key, model, **fields):
        retries.append(fields.get("max_retries"))
        return FakeModel(resting_status="responded")

    def client(base_url, _app, headers):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers=headers,
            timeout=30,
        )

    monkeypatch.setattr(script, "fetch_model_prices", prices)
    monkeypatch.setattr(script, "create_model", make_model)
    monkeypatch.setattr(script, "_client", client)
    monkeypatch.setattr(script, "ModelActor", lambda *a, **k: ScriptedOwner([{"kind": "wait"}]))
    monkeypatch.setattr(script, "DirectorBrain", lambda *a, **k: ScriptedDirectorBrain())
    file = tmp_path / "scenario.json"
    file.write_text(json.dumps(scenario(("dan",), duration_minutes=60).model_dump(mode="json")))
    env = {
        "DATABASE_URL": test_database_url,
        "ADMIN_TOKEN": "test-admin-token-32-characters-minimum",
        "OPENROUTER_API_KEY": "not-a-real-value",
    }
    campaign = str(uuid4())
    paid = ["--scenario", str(file), "--campaign", campaign, "--budget-usd", "5", "--seed", "99"]
    first = await script.execute(script.parse_args(paid), root=tmp_path, env=env)
    assert first["status"] == "completed", first["result"]
    assert first["seed"] == 99
    assert first["usage"]["requests"] == 0
    assert str(first["campaign_budget"]["limit_usd"]).rstrip("0").rstrip(".") == "5"
    assert script.exit_code(first) == 0
    # The engine alone repeats a call, so an outage holds no extra reservations.
    assert set(retries) == {0}
    bound = await events(pool, first["run_id"], "run_campaign_bound")
    assert bound[0]["payload"]["campaign_id"] == campaign
    # A second run may not spend from that ceiling unless that is asked for.
    with pytest.raises(script.ScenarioRunError, match="--share-campaign"):
        await script.execute(script.parse_args(paid), root=tmp_path, env=env)
    second = await script.execute(
        script.parse_args([*paid, "--share-campaign"]), root=tmp_path, env=env
    )
    assert second["run_id"] != first["run_id"] and second["status"] == "completed"
    # A saved run keeps its own ceiling and can resume with nothing else stated.
    manifest = tmp_path / ".local/scenario-runs" / first["run_id"] / "credentials.json"
    again = await script.execute(
        script.parse_args(["--resume", str(manifest)]), root=tmp_path, env=env
    )
    assert again["run_id"] == first["run_id"] and again["status"] == "completed"
