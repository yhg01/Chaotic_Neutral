"""The ZeroClaw trial backend on clocked runs: real PostgreSQL, the real program, scripted models.

These tests need the ZeroClaw program. Set AGENTRPG_ZEROCLAW to its path; without it they skip.
No test makes a provider call.
"""

import importlib.util
import os
import re
import shutil
import socket
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agentrpg.agent_surface import INSTRUCTIONS
from agentrpg.deep_agent import AgentLimitError
from agentrpg.director import Director
from agentrpg.zeroclaw_agent import (
    BINARY_ENV,
    HARNESS,
    NOTE_TOOLS,
    OPEN_CALL,
    PREFIX,
    SUMMARY_LEAD,
    UNEXPECTED_CALL,
    ZEROCLAW,
    ZeroClawBridge,
    zeroclaw_factory,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "try_clock_control", ROOT / "scripts" / "try_clock_control.py"
)
control = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("try_clock_control", control)
SPEC.loader.exec_module(control)

pytestmark = pytest.mark.skipif(
    not os.environ.get(BINARY_ENV), reason=f"{BINARY_ENV} must name the ZeroClaw program."
)

NEXT = {"description": "Shift", "minutes": 60, "reachable": False, "category": "work"}
# The words that tests/test_clock_realism.py bans from everything a clocked assistant reads.
BANNED = re.compile(
    r"\b(windows?|\w+_window|window_\w+|ticks?|ticked|runs?|running|run_id|turn[- ]counts?|beats?"
    r"|cues?|directors?|actors?|scenarios?|simulat\w*|credits?|\w+_credits)\b",
    re.I,
)
WALL_CLOCK = re.compile(r"^\[(CURRENT DATE & TIME: )?\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")


class Recording(control.ScriptModel):
    """A scripted model that also keeps the tools it was offered."""

    offered: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.offered = list(tools)
        return self


def text_of(messages) -> str:
    return "\n".join(m.content if isinstance(m.content, str) else str(m.content) for m in messages)


async def events(pool, run_id, kind):
    async with pool.connection() as conn:
        return await (
            await conn.execute(
                "SELECT owner_id, payload FROM run_events WHERE run_id=%s AND kind=%s "
                "ORDER BY created_at, id",
                (UUID(run_id), kind),
            )
        ).fetchall()


async def one_owner(app, pool, url, model, factory, *, minutes=60, turns=None):
    """One owner asks once. The assistant runs its script. Returns the agent and the run ID."""
    spec = control.scenario(("dan",), duration_minutes=240)
    actors = {
        "dan": control.ScriptedOwner(
            turns
            or [{"kind": "reply", "text": "Tell me what is new, please.", "next_action": NEXT}]
        )
    }
    async with control.clocked_run(
        app, url, spec, {"dan": model}, actors, agent_factory=factory
    ) as (engine, created, _runtime, agents):
        await Director(pool).control(UUID(created["run_id"]), "start")
        await engine.advance(created["run_id"], max_sim_minutes=minutes)
        return agents["dan"], created["run_id"]


async def test_the_control_story_is_the_same_on_both_backends(app, pool, test_database_url):
    _, baseline, first, _ = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    made = []

    async with ZeroClawBridge() as bridge:

        def factory(runtime, **arguments):
            made.append(zeroclaw_factory(bridge)(runtime, **arguments))
            return made[-1]

        run, trace, models, _ = await control.run_control(
            app,
            test_database_url,
            latency_seed=3,
            parallel=4,
            max_latency=0.03,
            agent_factory=factory,
        )
    assert run["status"] == "completed", run["result"]
    assert run["result"]["reason"] == "horizon"
    # The check replaces random IDs, date-time text and cursors with marks. After that the
    # story text is the same: the same events at the same story seconds in the same order.
    assert control.trace_text(trace) == control.trace_text(baseline)
    assert {owner: model.calls for owner, model in models.items()} == {
        owner: model.calls for owner, model in first.items()
    }
    # No attempt failed on either backend, and every program ended by itself.
    async with pool.connection() as conn:
        failures = await (
            await conn.execute("SELECT count(*) AS n FROM run_events WHERE kind='job_failure'")
        ).fetchone()
    assert failures["n"] == 0
    assert len(made) == 3
    for agent in made:
        # One program served every turn of its assistant.
        assert agent.stats.early_exits == 0 and agent.stats.processes == 1
        assert agent.stats.turns >= 2
        assert agent.stats.unexpected_calls == agent.stats.unanswered_calls == 0
        assert agent.stats.changed_arguments == agent.stats.replayed_messages == 0


async def test_the_harness_mode_shows_the_model_only_our_own_surface(app, pool, test_database_url):
    model = Recording(
        script=[
            control.calls(("get_updates", {}), ("read_owner_messages", {})),
            control.calls(("send_owner_message", {"content": "Nothing new today."})),
            control.end_turn(status="finished"),
        ]
    )
    async with ZeroClawBridge() as bridge:
        agent, run_id = await one_owner(
            app, pool, test_database_url, model, zeroclaw_factory(bridge, mode=HARNESS)
        )
        stats = agent.stats
    first = model.all_seen[0]
    assert first[0].type == "system" and first[0].content.startswith(INSTRUCTIONS)
    names = [tool["function"]["name"] for tool in model.offered]
    assert "send_owner_message" in names and "end_turn" in names
    assert not [name for name in names if name.startswith(PREFIX)]
    everything = "\n".join(text_of(messages) for messages in model.all_seen)
    assert "zeroclaw" not in everything.lower() and PREFIX not in everything
    assert "Host:" not in everything and "Working directory" not in everything
    assert not [m for seen in model.all_seen for m in seen if WALL_CLOCK.match(str(m.content))]
    # The model read the owner's words, though the program was given only a placeholder.
    assert "Tell me what is new, please." in everything
    # The program ran the loop: three model steps and four tool calls, and a clean exit.
    assert stats.processes == 1 and stats.early_exits == 0
    assert stats.model_calls == 3 and stats.tool_asks == 4
    assert stats.unexpected_calls == stats.changed_arguments == stats.unanswered_calls == 0
    said = await events(pool, run_id, "owner_message")
    assert "Nothing new today." in [row["payload"].get("content") for row in said]
    called = [row["payload"]["tool"] for row in await events(pool, run_id, "assistant_tool_call")]
    assert called == ["get_updates", "read_owner_messages", "send_owner_message"]


async def test_the_pass_through_mode_shows_the_model_the_program_s_own_text(
    app, pool, test_database_url
):
    model = Recording(
        script=[
            control.calls((PREFIX + "send_owner_message", {"content": "Nothing new today."})),
            control.calls((PREFIX + "end_turn", {"status": "finished", "text": ""})),
        ]
    )
    async with ZeroClawBridge() as bridge:
        agent, run_id = await one_owner(
            app, pool, test_database_url, model, zeroclaw_factory(bridge, mode=ZEROCLAW)
        )
        stats = agent.stats
    system, user = model.all_seen[0][0], model.all_seen[0][1]
    # What ZeroClaw 0.8.7 adds, and configuration cannot remove.
    assert "## Safety" in system.content
    assert f"Host: {socket.gethostname().split('.')[0]}" in system.content
    assert "Working directory:" in system.content
    assert WALL_CLOCK.match(user.content)
    assert BANNED.search(system.content.replace(INSTRUCTIONS, ""))
    # With ZeroClaw's default compact setting, its section about messaging channels is absent.
    assert "Channel Capabilities" not in system.content
    names = [tool["function"]["name"] for tool in model.offered]
    assert names and all(name.startswith(PREFIX) for name in names)
    # The world effect is still ours: one owner message, saved under the internal tool name.
    assert stats.early_exits == 0 and stats.unexpected_calls == 0
    called = [row["payload"]["tool"] for row in await events(pool, run_id, "assistant_tool_call")]
    assert called == ["send_owner_message"]


@pytest.mark.parametrize("after_the_effect", [False, True])
async def test_a_failed_tool_call_is_retried_in_a_new_process_without_a_second_effect(
    app, pool, test_database_url, after_the_effect
):
    model = Recording(
        script=[
            control.calls(("send_owner_message", {"content": "First note."}, "note-1")),
            control.calls(("send_owner_message", {"content": "Second note."}, "note-2")),
            control.end_turn(status="finished"),
        ]
    )
    failed = []

    def factory(runtime, **arguments):
        agent = zeroclaw_factory(bridge)(runtime, **arguments)
        real = agent.environment.execute

        async def execute(name, payload, call_id):
            if call_id == "note-2" and not failed:
                failed.append(call_id)
                if after_the_effect:
                    # The message is committed, and the answer is lost on the way back.
                    await real(name, payload, call_id)
                raise httpx.ConnectError("down")
            return await real(name, payload, call_id)

        agent.environment.execute = execute
        return agent

    async with ZeroClawBridge() as bridge:
        agent, run_id = await one_owner(app, pool, test_database_url, model, factory)
        stats = agent.stats
    assert failed == ["note-2"]
    # The second process got both saved model messages and the saved first result again.
    assert stats.processes == 2 and stats.early_exits == 0
    assert stats.replayed_messages == 2 and stats.replayed_results == 1
    assert model.calls == 3
    said = [row["payload"].get("content") for row in await events(pool, run_id, "owner_message")]
    assert said.count("First note.") == 1 and said.count("Second note.") == 1
    called = [
        row["payload"]["call_id"] for row in await events(pool, run_id, "assistant_tool_call")
    ]
    assert called == ["note-1", "note-2"]
    assert len(await events(pool, run_id, "job_failure")) == 1
    async with pool.connection() as conn:
        task = await (
            await conn.execute(
                "SELECT status, model_calls FROM agent_tasks WHERE session_key=%s",
                (agent.session_key,),
            )
        ).fetchall()
    assert [row["status"] for row in task] == ["completed"] and task[0]["model_calls"] == 3


async def test_repeated_and_duplicate_calls_all_run(app, pool, test_database_url):
    """With its default settings ZeroClaw skips a duplicate call and stops a repeated one."""
    model = Recording(
        script=[
            control.calls(("get_updates", {}, "same-1"), ("get_updates", {}, "same-2")),
            *[control.calls(("get_updates", {}, f"again-{n}")) for n in range(6)],
            control.end_turn(status="finished"),
        ]
    )
    async with ZeroClawBridge() as bridge:
        agent, run_id = await one_owner(
            app, pool, test_database_url, model, zeroclaw_factory(bridge)
        )
        stats = agent.stats
    assert stats.early_exits == 0
    assert stats.unanswered_calls == stats.unexpected_calls == 0
    called = [
        row["payload"]["call_id"] for row in await events(pool, run_id, "assistant_tool_call")
    ]
    assert called == ["same-1", "same-2", *[f"again-{n}" for n in range(6)]]


async def test_a_name_that_is_not_offered_gets_our_words_and_not_the_program_s(
    app, pool, test_database_url
):
    model = Recording(
        script=[
            control.calls(("run_shell", {"command": "ls"}, "shell-1")),
            control.end_turn(status="finished"),
        ]
    )
    async with ZeroClawBridge() as bridge:
        agent, _ = await one_owner(app, pool, test_database_url, model, zeroclaw_factory(bridge))
        stats = agent.stats
    assert stats.early_exits == 0 and stats.unanswered_calls == 1
    results = [m for m in model.all_seen[1] if isinstance(m, ToolMessage)]
    assert [m.content for m in results] == [UNEXPECTED_CALL]
    assert PREFIX not in text_of(model.all_seen[1])


async def test_private_notes_are_kept_between_turns_and_after_a_restart(
    app, pool, test_database_url, tmp_path
):
    """The instructions name private file tools, so this backend offers the library's own."""
    first = Recording(
        script=[
            control.calls(
                ("write_file", {"file_path": "/plan.md", "content": "Call the hall.\n"}, "w-1")
            ),
            control.calls(
                (
                    "edit_file",
                    {"file_path": "/plan.md", "old_string": "hall", "new_string": "caterer"},
                    "e-1",
                )
            ),
            control.calls(("read_file", {"file_path": "/missing.md"}, "r-0")),
            control.end_turn(status="waiting"),
            control.calls(
                ("read_file", {"file_path": "/plan.md"}, "r-1"), ("ls", {"path": "/"}, "l-1")
            ),
            control.end_turn(status="finished"),
        ]
    )
    turns = [
        {
            "kind": "reply",
            "text": "Keep a plan for me, please.",
            "next_action": {**NEXT, "minutes": 30},
        },
        {"kind": "reply", "text": "What is in the plan now?"},
    ]
    async with ZeroClawBridge(root=tmp_path) as bridge:
        agent, _ = await one_owner(
            app, pool, test_database_url, first, zeroclaw_factory(bridge), minutes=235, turns=turns
        )
        stats = agent.stats
    async with pool.connection() as conn:
        saved = (
            await (
                await conn.execute(
                    "SELECT files FROM zeroclaw_files WHERE session_key=%s", (agent.session_key,)
                )
            ).fetchone()
        )["files"]
    offered = {tool["function"]["name"] for tool in first.offered}
    assert set(NOTE_TOOLS) <= offered and "execute" not in offered and "delete" not in offered
    assert stats.unexpected_calls == stats.unanswered_calls == stats.early_exits == 0
    results = {m.tool_call_id: m.content for m in agent.messages if isinstance(m, ToolMessage)}
    assert "Call the caterer." in results["r-1"] and "/plan.md" in results["l-1"]
    assert "Call the caterer." not in results["r-0"] and "missing.md" in results["r-0"]
    # The files are in the database with the session, not in the program's folder.
    assert list(saved) == ["/plan.md"]
    assert not list(tmp_path.rglob("plan.md"))


async def test_an_abandoned_activation_leaves_a_history_that_a_provider_accepts(
    app, pool, test_database_url
):
    model = Recording(
        script=[
            control.calls(("check_bookings", {}, "open-1"), ("get_my_info", {}, "open-2")),
            control.end_turn(text="Second job done.", status="finished"),
        ]
    )
    refused = []

    def factory(runtime, **arguments):
        agent = zeroclaw_factory(bridge)(runtime, **arguments)
        real = agent.environment.execute

        async def execute(name, payload, call_id):
            if call_id == "open-1":
                refused.append(call_id)
                raise AgentLimitError("Refused.")
            return await real(name, payload, call_id)

        agent.environment.execute = execute
        return agent

    turns = [
        {
            "kind": "reply",
            "text": "First job.",
            "next_action": {**NEXT, "minutes": 30, "reachable": False},
        },
        {"kind": "reply", "text": "Second job."},
    ]
    async with ZeroClawBridge() as bridge:
        agent, run_id = await one_owner(
            app, pool, test_database_url, model, factory, minutes=235, turns=turns
        )
        stats = agent.stats
        # The whole saved history of the session, as the last activation left it.
        messages = agent.messages
    assert refused == ["open-1"]
    abandoned = await events(pool, run_id, "activation_abandoned")
    assert len(abandoned) == 1 and abandoned[0]["payload"]["failure_kind"] == "task_limit"
    # The later activation ran, and every call in its history has one result.
    assert stats.open_calls_closed == 2
    answered = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    asked = [c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
    assert sorted(asked) == sorted(answered)
    closed = next(m for m in messages if getattr(m, "tool_call_id", None) == "open-1")
    assert closed.content == OPEN_CALL.format(name="check_bookings", call_id="open-1")
    replies = [r["payload"].get("content") for r in await events(pool, run_id, "owner_message")]
    assert "Second job done." in replies


class Stepping(Recording):
    """Replies in order of its own calls, and writes a summary when it is asked for one."""

    step: int = 0
    summaries: int = 0

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if len(messages) == 1 and messages[0].type == "human":
            self.summaries += 1
            reply = AIMessage(content="Dan asked what was new. Three checks found nothing new.")
            return ChatResult(generations=[ChatGeneration(message=reply)])
        self.all_seen.append(list(messages))
        reply = self.script[min(self.step, len(self.script) - 1)]
        self.step += 1
        return ChatResult(generations=[ChatGeneration(message=reply)])


async def test_a_long_history_is_summarised_for_the_model_and_kept_whole_for_the_clock(
    app, pool, test_database_url
):
    note = "Nothing new in this check. " * 12
    model = Stepping(
        script=[
            *[
                control.calls(("send_owner_message", {"content": f"{n}. {note}"}, f"note-{n}"))
                for n in range(5)
            ],
            control.end_turn(status="finished"),
        ]
    )

    def factory(runtime, **arguments):
        agent = zeroclaw_factory(bridge)(runtime, **arguments)
        # About 300 tokens of history start a summary, and about 120 stay as they are.
        agent.summary_tokens, agent.summary_keep_tokens = 300, 120
        return agent

    async with ZeroClawBridge() as bridge:
        agent, run_id = await one_owner(app, pool, test_database_url, model, factory)
        stats = agent.stats
    assert stats.early_exits == 0
    assert stats.summaries >= 1 and model.summaries == stats.summaries
    summarised = [seen for seen in model.all_seen if str(seen[1].content).startswith(SUMMARY_LEAD)]
    assert summarised
    for seen in summarised:
        # After the summary the model reads whole exchanges: no result without its call.
        assert seen[2].type != "tool"
        asked = {c["id"] for m in seen if isinstance(m, AIMessage) for c in m.tool_calls}
        assert {m.tool_call_id for m in seen if isinstance(m, ToolMessage)} <= asked
        assert len(seen) < len(agent.messages) + 1
    # The saved history still holds every message, and every note went out once.
    assert agent.messages[0].content.endswith("Tell me what is new, please.")
    assert sum(1 for m in agent.messages if isinstance(m, ToolMessage)) == 6
    called = [r["payload"]["call_id"] for r in await events(pool, run_id, "assistant_tool_call")]
    assert called == [f"note-{n}" for n in range(5)]
    # Each summary is one counted model call of the task.
    async with pool.connection() as conn:
        task = await (
            await conn.execute(
                "SELECT model_calls FROM agent_tasks WHERE session_key=%s", (agent.session_key,)
            )
        ).fetchone()
    assert task["model_calls"] == 6 + stats.summaries


async def test_in_pass_through_mode_the_program_keeps_its_own_history_between_turns(
    app, pool, test_database_url
):
    model = Stepping(
        script=[
            control.calls((PREFIX + "send_owner_message", {"content": "First job done."})),
            control.calls((PREFIX + "end_turn", {"status": "finished", "text": ""})),
            control.calls((PREFIX + "end_turn", {"status": "finished", "text": "Second done."})),
        ]
    )
    turns = [
        {"kind": "reply", "text": "First job.", "next_action": {**NEXT, "minutes": 30}},
        {"kind": "reply", "text": "Second job."},
    ]
    async with ZeroClawBridge() as bridge:
        agent, _ = await one_owner(
            app,
            pool,
            test_database_url,
            model,
            zeroclaw_factory(bridge, mode=ZEROCLAW),
            minutes=235,
            turns=turns,
        )
        stats = agent.stats
    # One program, several turns. In a later turn the model reads the earlier turn as the
    # program kept it: the owner's first words, its own tool call and the result.
    assert stats.processes == 1 and stats.turns >= 2 and stats.early_exits == 0
    later = next(seen for seen in model.all_seen if "Second job." in text_of(seen))
    said = [m.content for m in later if m.type == "human"]
    assert len(said) >= 2 and "First job." in said[0] and "Second job." in said[-1]
    assert any(isinstance(m, ToolMessage) for m in later)
    assert any(isinstance(m, AIMessage) and m.tool_calls for m in later)


async def test_the_program_has_no_host_tools_and_no_keys(app, pool, test_database_url):
    model = Recording(script=[control.end_turn(status="finished")])
    async with ZeroClawBridge(keep=True) as bridge:
        agent, _ = await one_owner(
            app, pool, test_database_url, model, zeroclaw_factory(bridge, mode=ZEROCLAW)
        )
        config = (agent._seat.directory / "config" / "config.toml").read_text()
        root = bridge.root
    try:
        assert agent.stats.early_exits == 0 and agent.stats.turns >= 1
        names = [tool["function"]["name"] for tool in model.offered]
        # Only our tools, under ZeroClaw's prefix. Its shell, file and memory tools are absent.
        assert len(names) >= 20 and all(name.startswith(PREFIX) for name in names)
        # The configuration holds no provider key and no board credential.
        assert "Bearer" not in config and "openrouter" not in config.lower()
        assert 'api_key = "local-bridge"' in config
    finally:
        shutil.rmtree(root, ignore_errors=True)
