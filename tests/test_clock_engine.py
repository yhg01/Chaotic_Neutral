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
from uuid import UUID

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
    assert second.clock["now"] == "Monday 08:00"
    assert second.cue == "You've finished: Hospital appointment."
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
    assert prompts[0].startswith("It's Monday 06:00.")
    # The owner's next message interrupts the two-hour wait, at 06:20 and a little.
    assert "Cancel that." in prompts[1]
    assert re.match(r"It's Monday 06:[23]\d\.", prompts[1])
    # The stale two-hour timer is gone. The later timer comes from the second turn's request.
    assert prompts[2].startswith("It's Monday 07:0")
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
        "It's Monday 08:00. You usually check in around now.\n\n"
        "Nothing new since you last checked. Pick up anything still open, or end your turn."
    )
    assert prompts[1].startswith("It's Tuesday 08:00.")


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
    assert re.match(r"It's Monday 06:4\d\.", mei_prompts[0])
    assert all("Fence" not in view.model_dump_json() for view in actors["dan"].views)
    assert all("Fence" not in view.model_dump_json() for view in actors["mei"].views)


async def test_limit_silences_one_process_and_the_run_goes_on(app, pool, test_database_url):
    owners = ("dan", "mei")
    spec = scenario(
        owners,
        duration_minutes=240,
        limits={"max_agent_activations_per_owner": 1, "max_actor_turns_per_owner": 1},
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
    assert {(e["owner_id"], e["payload"]["process"]) for e in limited} >= {
        ("dan", "agent"),
        ("dan", "actor"),
    }
    jobs = await rows(
        pool,
        "SELECT owner_id,kind,count(*) AS n FROM run_jobs WHERE run_id=%s GROUP BY 1,2",
        UUID(created["run_id"]),
    )
    assert all(job["n"] == 1 for job in jobs)


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
                from agentrpg.deep_agent import AgentProviderError

                raise AgentProviderError("Refused.")
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
    assert len(abandoned) == 1 and abandoned[0]["payload"]["failure_kind"] == "provider_rejected"
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
    assert len(view.conversation) == 14
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
        for turn in range(4):
            envelope = ActivationEnvelope(
                run_id=created["run_id"],
                participant_id=agent.agent_id,
                activation_id=UUID(int=turn + 1000),
                window_id=0,
                owner_messages=tuple(
                    OwnerMessage(message_id=UUID(int=turn * 10 + index + 1), content=big)
                    for index in range(3)
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
    assert len(humans) == 4


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
    assert re.match(r"It's Monday 06:0\d\.", human_prompts(models["mei"])[0])
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
                "transfer_credits",
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
            calls(("inspect_transfer_account", {})),
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
