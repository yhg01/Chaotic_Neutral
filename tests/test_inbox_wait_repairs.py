"""Real mailbox references, durable notification evidence and event-driven waits."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field, ValidationError
from test_deep_agent import ScriptedModel

from agentrpg.activation import ActivationEnvelope, ActivationResult, WorldObservation
from agentrpg.actor import ActorOutput
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import (
    AgentRuntime,
    AgentRuntimeError,
    AgentTaskConflictError,
    _unobserved_mail,
)
from agentrpg.director import Director, DirectorError, row, save_message
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxError, InboxService
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario
from agentrpg.transfer_actor_task import actor_scenario


def tool(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(uuid4())}])


@asynccontextmanager
async def mailbox_agent(
    app, pool, database_url, responses, *, strict=False, guard=False, model_type=ScriptedModel
):
    director = Director(pool)
    manifest = await director.create_run(actor_scenario())
    run_id = UUID(manifest["run_id"])
    await director.control(run_id, "start")
    mail = await InboxService(pool).send(
        run_id, None, "alice", "Receipt", "Actual bank body.", uuid4(), source="director"
    )
    async with pool.connection() as conn:
        chat = await save_message(
            conn, run_id, "alice", "owner", "Please check my messages.", uuid4(), 0
        )
    event = next(e for e in await director.trace(run_id) if e["kind"] == "inbox_received")
    member = manifest["participants"][0]
    model = model_type(responses=responses(mail, chat))
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer " + member["token"]},
        ) as client,
        AgentRuntime(database_url) as runtime,
    ):
        agent = runtime.agent(
            model=model,
            board=BoardTools(client, manifest["cluster_id"]),
            environment=EnvironmentTools(client, run_id),
            agent_id=member["agent_id"],
            session_id=member["session_id"],
            instructions="Help your owner; read actual Inbox evidence.",
            require_explicit_finish=strict,
            guard_empty_scratch=guard,
        )
        envelope = ActivationEnvelope(
            run_id=run_id,
            participant_id=UUID(member["agent_id"]),
            activation_id=uuid4(),
            window_id=0,
            source_event_ids=[event["id"]],
            world_observations=[
                WorldObservation(
                    event_id=event["id"],
                    kind="inbox_received",
                    content=json.dumps(event["payload"]),
                )
            ],
        )
        yield agent, envelope, model, mail, chat


class ToolHistoryModel(ScriptedModel):
    tool_history: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        result = super().bind_tools(tools, **kwargs)
        self.tool_history.append(list(self.offered))
        return result


async def test_empty_scratch_reads_are_hidden_and_unoffered_calls_get_correction(
    app, pool, test_database_url
):
    def responses(mail, chat):
        return [
            tool("grep", {"pattern": "Alice"}),
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("wait_for_mail", {"text": "The amount is unknown; awaiting evidence."}),
        ]

    async with mailbox_agent(
        app,
        pool,
        test_database_url,
        responses,
        strict=True,
        guard=True,
        model_type=ToolHistoryModel,
    ) as (agent, envelope, model, mail, chat):
        result = await agent.run_activation(envelope)
        assert result.status == "waiting" and result.wait_for == ("inbox_received",)
        for offered in model.tool_history:
            assert not {"ls", "glob", "grep", "read_file"} & set(offered)
            assert {"write_file", "read_mail", "inspect_transfer_account"} <= set(offered)
        corrections = [m for m in agent.messages if isinstance(m, ToolMessage) and m.name == "grep"]
        assert len(corrections) == 1
        assert json.loads(corrections[0].content)["error"]["code"] == "empty_scratch"
        assert "account" in corrections[0].content
        assert await agent.run_activation(envelope) == result and model.calls == 3
        replacement = agent.runtime.agent(
            model=ScriptedModel(),
            board=agent.board,
            environment=agent.environment,
            agent_id=agent.agent_id,
            session_id=agent.session_id,
            instructions=agent.instructions,
            require_explicit_finish=True,
        )
        with pytest.raises(AgentTaskConflictError, match="instructions"):
            await replacement.run_activation(envelope)


async def test_scratch_notes_remain_readable_after_write_and_runtime_recreation(
    app, pool, test_database_url
):
    def responses(mail, chat):
        return [
            tool("write_file", {"file_path": "/note.txt", "content": "Private note: orange."}),
            tool("read_file", {"file_path": "/note.txt"}),
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("finish_activation", {"status": "finished"}),
        ]

    async with mailbox_agent(
        app,
        pool,
        test_database_url,
        responses,
        strict=True,
        guard=True,
        model_type=ToolHistoryModel,
    ) as (agent, envelope, model, mail, chat):
        result = await agent.run_activation(envelope)
        assert result.status == "finished"
        assert "grep" not in model.tool_history[0] and "grep" in model.tool_history[1]
        assert any(
            m.name == "read_file" and "orange" in m.content
            for m in agent.messages
            if isinstance(m, ToolMessage)
        )
        async with AgentRuntime(test_database_url) as runtime:
            restored_model = ToolHistoryModel(
                responses=[
                    tool("read_file", {"file_path": "/note.txt"}),
                    tool("finish_activation", {"status": "finished"}),
                ]
            )
            restored = runtime.agent(
                model=restored_model,
                board=agent.board,
                environment=agent.environment,
                agent_id=agent.agent_id,
                session_id=agent.session_id,
                instructions=agent.instructions,
                require_explicit_finish=True,
                guard_empty_scratch=True,
            )
            assert await restored.run_activation(envelope) == result
            assert restored_model.calls == 0
            next_input = envelope.model_copy(
                update={
                    "activation_id": uuid4(),
                    "window_id": 1,
                    "source_event_ids": (),
                    "world_observations": (),
                    "owner_messages": (),
                }
            )
            await restored.run_activation(next_input)
            assert "grep" in restored_model.tool_history[0]
            assert any(
                m.name == "read_file" and "orange" in m.content
                for m in restored.messages
                if isinstance(m, ToolMessage)
            )


async def test_scratch_filter_does_not_change_concurrent_legacy_agent(app, pool, test_database_url):
    def responses(mail, chat):
        return [
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("finish_activation", {"status": "finished"}),
        ]

    async with (
        mailbox_agent(
            app,
            pool,
            test_database_url,
            responses,
            strict=True,
            guard=True,
            model_type=ToolHistoryModel,
        ) as guarded,
        mailbox_agent(
            app, pool, test_database_url, responses, strict=True, model_type=ToolHistoryModel
        ) as legacy,
    ):
        await asyncio.gather(
            guarded[0].run_activation(guarded[1]), legacy[0].run_activation(legacy[1])
        )
        assert "grep" not in guarded[2].tool_history[0]
        assert "grep" in legacy[2].tool_history[0]


async def test_owner_chat_is_not_mail_and_mixed_ack_is_atomic(pool):
    director = Director(pool)
    manifest = await director.create_run(actor_scenario())
    run = UUID(manifest["run_id"])
    await director.control(run, "start")
    service = InboxService(pool)
    mail = await service.send(run, "bob", "alice", "Lunch", "Ten credits.", uuid4())
    async with pool.connection() as conn:
        own = await save_message(conn, run, "alice", "owner", "Hello.", uuid4(), 0)
        peer = await save_message(conn, run, "bob", "owner", "Private.", uuid4(), 0)
    for chat_id, status in [(own["id"], 422), (peer["id"], 404)]:
        with pytest.raises(InboxError) as error:
            await service.acknowledge(run, "alice", [mail["id"], chat_id])
        assert error.value.status_code == status
        with pytest.raises(InboxError) as error:
            await service.read(run, "alice", chat_id)
        assert error.value.status_code == status
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM inbox_receipts"))["n"] == 0
    assert (await service.acknowledge(run, "alice", [mail["id"]]))["acknowledged_ids"]


async def test_finished_requires_body_not_ack_and_survives_new_runtime(
    app, pool, test_database_url
):
    def responses(mail, chat):
        return [
            tool("acknowledge_mail", {"message_ids": [str(chat["id"])]}),
            tool("acknowledge_mail", {"message_ids": [str(mail["id"])]}),
            tool("finish_activation", {"status": "finished", "text": "Premature checked claim."}),
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool(
                "finish_activation",
                {"status": "finished", "text": "I read the actual bank notice."},
            ),
        ]

    async with mailbox_agent(app, pool, test_database_url, responses) as (
        agent,
        envelope,
        model,
        mail,
        chat,
    ):
        result = await agent.run_activation(envelope)
        assert result.text == "I read the actual bank notice." and model.calls == 5
        replies = [json.loads(m.content) for m in agent.messages if isinstance(m, ToolMessage)]
        assert any(r.get("error", {}).get("status") == 422 for r in replies)
        assert any(
            r.get("error", {}).get("code") == "unobserved_inbox_notifications" for r in replies
        )
        async with AgentRuntime(test_database_url) as runtime:
            restored_model = ScriptedModel()
            restored = runtime.agent(
                model=restored_model,
                board=agent.board,
                environment=agent.environment,
                agent_id=agent.agent_id,
                session_id=agent.session_id,
                instructions=agent.instructions,
            )
            assert await restored.run_activation(envelope) == result
            assert restored_model.calls == 0


async def test_empty_mail_query_cannot_finish_but_real_list_body_can(app, pool, test_database_url):
    def responses(mail, chat):
        return [
            tool("list_mail", {"query": "not-present-in-this-mailbox"}),
            tool("finish_activation", {"status": "finished"}),
            tool("list_mail", {"mailbox": "inbox"}),
            tool("finish_activation", {"status": "finished", "text": "Read actual notice."}),
        ]

    async with mailbox_agent(app, pool, test_database_url, responses) as (
        agent,
        envelope,
        model,
        mail,
        chat,
    ):
        assert (await agent.run_activation(envelope)).text == "Read actual notice."
        assert model.calls == 4


async def test_unread_notification_survives_a_new_activation(app, pool, test_database_url):
    def responses(mail, chat):
        return [
            tool("finish_activation", {"status": "responded", "text": "I will check later."}),
            tool("finish_activation", {"status": "finished"}),
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("finish_activation", {"status": "finished", "text": "Now read."}),
        ]

    async with mailbox_agent(app, pool, test_database_url, responses, strict=True) as (
        agent,
        envelope,
        model,
        mail,
        chat,
    ):
        assert (await agent.run_activation(envelope)).status == "responded"
        next_input = envelope.model_copy(
            update={
                "activation_id": uuid4(),
                "window_id": 1,
                "source_event_ids": (),
                "world_observations": (),
            }
        )
        assert (await agent.run_activation(next_input)).text == "Now read."
        assert model.calls == 4
        assert any(
            isinstance(m, ToolMessage) and "unobserved_inbox_notifications" in m.content
            for m in agent.messages
        )


def test_acknowledgement_and_unrelated_body_are_not_notification_evidence():
    mail = str(uuid4())
    envelope = {
        "world_observations": [
            {"kind": "inbox_received", "content": json.dumps({"message_id": mail})}
        ]
    }
    messages = [
        ToolMessage(
            name="acknowledge_mail",
            tool_call_id="ack",
            content=json.dumps({"ok": True, "data": {"acknowledged_ids": [mail]}}),
        )
    ]
    assert _unobserved_mail(envelope, messages) == [mail]
    messages.append(
        ToolMessage(
            name="read_mail",
            tool_call_id="read",
            content=json.dumps({"ok": True, "data": {"id": str(uuid4()), "body": "Other mail"}}),
        )
    )
    assert _unobserved_mail(envelope, messages) == [mail]


def test_explicit_wait_contract():
    assert ActivationResult(status="waiting", wait_for=["inbox_received"]).wait_for == (
        "inbox_received",
    )
    for values in (
        {"status": "finished", "wait_for": ["inbox_received"]},
        {"status": "waiting", "wait_for": ["inbox_received", "inbox_received"]},
        {"status": "waiting", "wait_for": ["anything"]},
    ):
        with pytest.raises(ValidationError):
            ActivationResult(**values)


async def test_explicit_finish_mode_rejects_plain_provider_reply(app, pool, test_database_url):
    def responses(mail, chat):
        return [
            tool("read_mail", {"message_id": str(mail["id"])}),
            AIMessage(content="Plain reply."),
        ]

    async with mailbox_agent(app, pool, test_database_url, responses, strict=True) as (
        agent,
        envelope,
        model,
        mail,
        chat,
    ):
        with pytest.raises(AgentRuntimeError, match="requires finish_activation"):
            await agent.run_activation(envelope)
        assert model.calls == 2


@pytest.mark.parametrize(
    "wait_tool,event", [("wait_for_mail", "inbox_received"), ("wait_for_owner", "owner_message")]
)
async def test_explicit_finish_wait_is_saved_and_mode_is_session_bound(
    app, pool, test_database_url, wait_tool, event
):
    def responses(mail, chat):
        return [
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool(
                wait_tool,
                {"text": "Awaiting a verified amount."},
            ),
        ]

    async with mailbox_agent(app, pool, test_database_url, responses, strict=True) as (
        agent,
        envelope,
        model,
        mail,
        chat,
    ):
        result = await agent.run_activation(envelope)
        assert result.wait_for == (event,) and model.calls == 2
        assert await agent.run_activation(envelope) == result
        replacement = agent.runtime.agent(
            model=ScriptedModel(),
            board=agent.board,
            environment=agent.environment,
            agent_id=agent.agent_id,
            session_id=agent.session_id,
            instructions=agent.instructions,
        )
        with pytest.raises(AgentTaskConflictError, match="instructions"):
            await replacement.run_activation(envelope)


class WaitingOwner:
    def __init__(self):
        self.views = []

    async def respond(self, view):
        self.views.append(view)
        if view.window == 0 or "new request" in view.cue:
            return ActorOutput(
                kind="reply",
                text="Wait for the next real message.",
                source_event_id=view.source_event_id,
            )
        return ActorOutput(kind="wait", source_event_id=view.source_event_id)


class WaitingAssistant:
    def __init__(self):
        self.inputs = []

    async def run_activation(self, envelope):
        self.inputs.append(envelope)
        return ActivationResult(status="waiting", wait_for=["inbox_received"])


async def waiting_run(pool, future=False):
    data = vehicle_scenario(2).model_dump(mode="json")
    data["max_windows"] = 8
    data["beats"] = data["beats"][:2]
    if future:
        data["beats"].append(
            {
                "id": "later",
                "trigger": {"kind": "window_reached", "window": 3},
                "recipients": ["owner-1"],
                "cue": "A new request arrived. Ask the assistant to check.",
            }
        )
    spec = ScenarioSpec.model_validate(data)
    director = Director(pool)
    manifest = await director.create_run(spec)
    run = UUID(manifest["run_id"])
    await director.control(run, "start")
    owners = {o.id: WaitingOwner() for o in spec.owners}
    agents = {o.id: WaitingAssistant() for o in spec.owners}
    return director, run, RunEngine(pool, actors=owners, agents=agents), owners, agents


async def test_explicit_wait_pauses_without_idle_calls_and_mail_wakes(pool):
    director, run, engine, owners, agents = await waiting_run(pool)
    state = await engine.advance(run)
    assert state["status"] == "paused" and state["result"]["reason"] == "waiting_for_events"
    assert [len(a.inputs) for a in agents.values()] == [1, 1]
    with pytest.raises(DirectorError):
        await engine.advance(run)
    assert [len(a.inputs) for a in agents.values()] == [1, 1]
    await director.control(run, "resume")
    await InboxService(pool).send(
        run, "owner-2", "owner-1", "New fact", "The amount is confirmed.", uuid4()
    )
    state = await engine.advance(run)
    assert [len(a.inputs) for a in agents.values()] == [2, 1]
    assert owners["owner-1"].views[-1].assistant_wait_for == ("inbox_received",)
    assert any(o.kind == "inbox_received" for o in agents["owner-1"].inputs[-1].world_observations)
    assert state["status"] == "paused"


async def test_future_cue_survives_wait_without_clock_polling(pool):
    director, run, engine, owners, agents = await waiting_run(pool, future=True)
    for _ in range(4):
        state = await engine.advance(run)
    assert state["status"] == "paused" and state["current_window"] == 4
    assert [e.window_id for e in agents["owner-1"].inputs] == [0, 3]
    assert [e.window_id for e in agents["owner-2"].inputs] == [0]
