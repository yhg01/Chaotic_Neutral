"""Actual Deep Agents graphs, PostgreSQL checkpoints, and PostgreSQL board API."""

import asyncio
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import (
    AgentBusyError,
    AgentIdentityError,
    AgentLimitError,
    AgentRuntime,
    AgentRuntimeError,
    AgentTaskConflictError,
)


class ScriptedModel(BaseChatModel):
    """Deterministic model exercising the real graph/tool/checkpoint path."""

    responses: list[Any] = Field(default_factory=list)
    calls: int = 0
    seen: list[Any] = Field(default_factory=list)
    offered: list[str] = Field(default_factory=list)
    gate: Any = None
    entered: Any = None

    @property
    def _llm_type(self):
        return "test-scripted"

    def bind_tools(self, tools, **kwargs):
        self.offered = [tool.name if hasattr(tool, "name") else tool["name"] for tool in tools]
        for tool in tools:
            if hasattr(tool, "tool_call_schema"):
                schema = tool.tool_call_schema
                if hasattr(schema, "model_json_schema"):
                    assert "tool_call_id" not in schema.model_json_schema().get("properties", {})
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.seen = messages
        if self.entered:
            self.entered.set()
        if self.gate:
            await self.gate.wait()
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


def call(name="post_thread", args=None, call_id="provider-call-1"):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args or {"title": "Durable post", "body": "Saved once"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def board_client(app, token):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": f"Bearer {token}"},
    )


def make_agent(runtime, client, cluster, identity, model, session=None, **kwargs):
    return runtime.agent(
        model=model,
        board=BoardTools(client, cluster["id"]),
        agent_id=identity["agent"]["id"],
        session_id=session or uuid4(),
        instructions="Use the board when asked. Preserve each user's private conversation.",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_post_restore_new_turn_and_completed_task_replay(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    session, task_id = uuid4(), uuid4()
    async with board_client(app, identities[0]["token"]) as client:
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(responses=[call(), AIMessage(content="Posted.")])
            agent = make_agent(runtime, client, cluster, identities[0], model, session)
            assert await agent.run("Remember orange. Post my note.", task_id=task_id) == "Posted."
            assert model.calls == 2
            assert "task" not in model.offered and "execute" not in model.offered
            assert {"post_thread", "reply", "read_thread"} <= set(model.offered)
            assert agent.events[0]["result"]["ok"]
        # New pool, saver, graph, model, and adapter recreate a process restart.
        async with AgentRuntime(test_database_url) as runtime:
            restored_model = ScriptedModel(responses=[AIMessage(content="Orange.")])
            restored = make_agent(runtime, client, cluster, identities[0], restored_model, session)
            assert (
                await restored.run("Remember orange. Post my note.", task_id=task_id) == "Posted."
            )
            assert restored_model.calls == 0
            assert await restored.run("What should you remember?", task_id=uuid4()) == "Orange."
            assert any(
                isinstance(message, HumanMessage) and "orange" in message.content
                for message in restored_model.seen
            )
            response = await client.get(f"clusters/{cluster['id']}/threads")
            assert len(response.json()["items"]) == 1
            with pytest.raises(AgentTaskConflictError, match="different text"):
                await restored.run("Changed payload", task_id=task_id)


@pytest.mark.asyncio
async def test_retry_after_committed_tool_does_not_duplicate(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    session, task_id = uuid4(), uuid4()
    async with board_client(app, identities[0]["token"]) as client:
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(responses=[call(), AIMessage(content="Recovered.")])
            agent = make_agent(runtime, client, cluster, identities[0], model, session)
            execute = agent.board.execute

            async def commit_then_fail(*args, **kwargs):
                result = await execute(*args, **kwargs)
                assert result["ok"]
                raise AgentRuntimeError("Simulated process interruption after commit.")

            agent.board.execute = commit_then_fail
            with pytest.raises(AgentRuntimeError, match="Simulated"):
                await agent.run("Post the durable note.", task_id=task_id)
            snapshot = await agent.graph.aget_state(agent.config)
            assert snapshot.next
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(responses=[AIMessage(content="Recovered.")])
            resumed = make_agent(runtime, client, cluster, identities[0], model, session)
            assert await resumed.run("Post the durable note.", task_id=task_id) == "Recovered."
            response = await client.get(f"clusters/{cluster['id']}/threads")
            assert len(response.json()["items"]) == 1
            assert resumed.events[0]["result"]["ok"]
            assert model.calls == 1


@pytest.mark.asyncio
async def test_identity_and_state_isolation(test_database_url, app, provision):
    cluster, identities = await provision(count=2)
    session = uuid4()
    async with (
        board_client(app, identities[0]["token"]) as first,
        board_client(app, identities[1]["token"]) as second,
        AgentRuntime(test_database_url) as runtime,
    ):
        wrong = make_agent(runtime, first, cluster, identities[1], ScriptedModel())
        with pytest.raises(AgentIdentityError):
            await wrong.run("Read secrets", task_id=uuid4())
        one = make_agent(
            runtime,
            first,
            cluster,
            identities[0],
            ScriptedModel(responses=[AIMessage(content="Saved.")]),
            session,
        )
        await one.run("My private code is alpha.", task_id=uuid4())
        other_model = ScriptedModel(responses=[AIMessage(content="No earlier conversation.")])
        two = make_agent(runtime, second, cluster, identities[1], other_model, session)
        await two.run("Read my conversation.", task_id=uuid4())
        assert one.session_key != two.session_key
        assert not any("alpha" in str(message.content) for message in other_model.seen)


@pytest.mark.asyncio
async def test_session_contention_across_runtimes(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    session, entered, gate = uuid4(), asyncio.Event(), asyncio.Event()
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as first_runtime,
        AgentRuntime(test_database_url) as second_runtime,
    ):
        first = make_agent(
            first_runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(
                responses=[AIMessage(content="Done.")],
                gate=gate,
                entered=entered,
            ),
            session,
        )
        second = make_agent(
            second_runtime, client, cluster, identities[0], ScriptedModel(), session
        )
        task_id = uuid4()
        running = asyncio.create_task(first.run("Hold this turn.", task_id=task_id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            with pytest.raises(AgentBusyError):
                await second.run("Hold this turn.", task_id=task_id)
            with pytest.raises(AgentBusyError):
                await first.run("Another turn.", task_id=uuid4())
        finally:
            gate.set()
            assert await running == "Done."
        assert await second.run("Hold this turn.", task_id=task_id) == "Done."


@pytest.mark.asyncio
async def test_failed_model_is_sanitized_and_can_resume(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    task_id, session = uuid4(), uuid4()
    secret = "SECRET-provider-response-with-key"
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        bad = make_agent(
            runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(responses=[RuntimeError(secret)]),
            session,
        )
        with pytest.raises(AgentRuntimeError) as caught:
            await bad.run("Please finish", task_id=task_id)
        assert secret not in str(caught.value)
        state = await bad.graph.aget_state(bad.config)
        assert secret not in str(state)
        with pytest.raises(AgentTaskConflictError, match="unfinished"):
            await bad.run("Skip unfinished task", task_id=uuid4())
        good = make_agent(
            runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(responses=[AIMessage(content="Finished.")]),
            session,
        )
        assert await good.run("Please finish", task_id=task_id) == "Finished."


@pytest.mark.asyncio
async def test_model_budget_survives_retry(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    task_id, session = uuid4(), uuid4()
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        model = ScriptedModel(responses=[call(), AIMessage(content="Never reached")])
        agent = make_agent(
            runtime, client, cluster, identities[0], model, session, max_model_calls=1
        )
        with pytest.raises(AgentLimitError, match="model-call"):
            await agent.run("Post then finish", task_id=task_id)
        assert model.calls == 1
        again = make_agent(
            runtime, client, cluster, identities[0], model, session, max_model_calls=1
        )
        with pytest.raises(AgentLimitError, match="model-call"):
            await again.run("Post then finish", task_id=task_id)
        assert model.calls == 1
        assert len((await client.get(f"clusters/{cluster['id']}/threads")).json()["items"]) == 1


@pytest.mark.asyncio
async def test_timeout_releases_session_lock(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    task_id, session = uuid4(), uuid4()
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        blocked = make_agent(
            runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(
                responses=[AIMessage(content="Too late")],
                gate=asyncio.Event(),
            ),
            session,
            timeout_seconds=0.3,
        )
        with pytest.raises(AgentLimitError, match="time limit"):
            await blocked.run("Please finish", task_id=task_id)
        recovered = make_agent(
            runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(responses=[AIMessage(content="Recovered.")]),
            session,
        )
        assert await recovered.run("Please finish", task_id=task_id) == "Recovered."


@pytest.mark.asyncio
async def test_task_namespaces_separate_reused_provider_tool_ids(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        model = ScriptedModel(
            responses=[call(), AIMessage(content="One"), call(), AIMessage(content="Two")]
        )
        agent = make_agent(runtime, client, cluster, identities[0], model)
        await agent.run("First user request", task_id=uuid4())
        await agent.run("Second user request", task_id=uuid4())
        assert len((await client.get(f"clusters/{cluster['id']}/threads")).json()["items"]) == 2


@pytest.mark.asyncio
async def test_cancel_releases_lock_and_resumes_saved_task(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    task_id, session, entered = uuid4(), uuid4(), asyncio.Event()
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        agent = make_agent(
            runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(
                responses=[AIMessage(content="Never completed")],
                gate=asyncio.Event(),
                entered=entered,
            ),
            session,
        )
        running = asyncio.create_task(agent.run("Resume me", task_id=task_id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        replacement = make_agent(
            runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(responses=[AIMessage(content="Resumed")]),
            session,
        )
        assert await replacement.run("Resume me", task_id=task_id) == "Resumed"


@pytest.mark.asyncio
async def test_completed_graph_without_receipt_is_recovered(test_database_url, app, provision):
    cluster, identities = await provision(count=1)
    task_id, session = uuid4(), uuid4()
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        agent = make_agent(
            runtime,
            client,
            cluster,
            identities[0],
            ScriptedModel(responses=[call(), AIMessage(content="Already done")]),
            session,
        )
        await agent.run("Commit my post", task_id=task_id)
        # Reproduce the durable boundary after the graph's last checkpoint and
        # before the separate completion receipt, without losing that checkpoint.
        async with runtime.pool.connection() as connection:
            await connection.execute(
                """UPDATE agent_tasks SET status='pending', result=NULL, completed_at=NULL
                   WHERE session_key=%s AND task_id=%s""",
                (agent.session_key, task_id),
            )
        model = ScriptedModel()
        restored = make_agent(runtime, client, cluster, identities[0], model, session)
        assert await restored.run("Commit my post", task_id=task_id) == "Already done"
        assert model.calls == 0
        assert len((await client.get(f"clusters/{cluster['id']}/threads")).json()["items"]) == 1


@pytest.mark.asyncio
async def test_model_cannot_override_injected_tool_id(test_database_url, app, provision):
    from uuid import uuid5

    cluster, identities = await provision(count=1)
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        model = ScriptedModel(
            responses=[
                call(
                    args={
                        "title": "Bound identity",
                        "body": "Safe",
                        "tool_call_id": "malicious-override",
                    }
                ),
                AIMessage(content="Done"),
            ]
        )
        agent = make_agent(runtime, client, cluster, identities[0], model)
        await agent.run("Post this", task_id=uuid4())
        async with runtime.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT request_id FROM messages WHERE cluster_id=%s",
                (cluster["id"],),
            )
            row = await cursor.fetchone()
        assert row["request_id"] == uuid5(agent.board.operation_namespace, "provider-call-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_size", [4, None], ids=["four-connections", "default-pool"])
async def test_fifteen_sessions_run_with_small_checkpoint_pool(
    test_database_url, app, provision, pool_size
):
    from contextlib import AsyncExitStack

    cluster, identities = await provision(count=15)
    gate = asyncio.Event()
    async with AsyncExitStack() as stack:
        options = {} if pool_size is None else {"pool_size": pool_size}
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url, **options))
        assert runtime.pool.max_size == (8 if pool_size is None else pool_size)
        assert runtime.lock_pool.max_size == 15
        entered = [asyncio.Event() for _ in identities]
        agents = []
        for identity, event in zip(identities, entered, strict=True):
            client = await stack.enter_async_context(board_client(app, identity["token"]))
            agents.append(
                make_agent(
                    runtime,
                    client,
                    cluster,
                    identity,
                    ScriptedModel(
                        responses=[call(), AIMessage(content="Posted")],
                        gate=gate,
                        entered=event,
                    ),
                )
            )
        running = [
            asyncio.create_task(agent.run("Post concurrently", task_id=uuid4())) for agent in agents
        ]
        try:
            # All 15 models must be in flight before any is allowed to finish.
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), timeout=10)
        finally:
            gate.set()
        assert await asyncio.gather(*running) == ["Posted"] * 15
        async with runtime.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS total FROM messages WHERE cluster_id=%s",
                (cluster["id"],),
            )
            assert (await cursor.fetchone())["total"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_response",
    [
        AIMessage(content="Partial answer", response_metadata={"finish_reason": "length"}),
        AIMessage(content=""),
        AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "name": "post_thread",
                    "args": "{",
                    "id": "invalid",
                    "error": "Malformed JSON",
                }
            ],
            tool_calls=[
                {
                    "name": "post_thread",
                    "args": {"title": "Do not write", "body": "Invalid batch"},
                    "id": "valid-part",
                    "type": "tool_call",
                }
            ],
        ),
    ],
)
async def test_incomplete_model_output_is_not_completed(
    test_database_url,
    app,
    provision,
    bad_response,
):
    cluster, identities = await provision(count=1)
    task_id = uuid4()
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        model = ScriptedModel(responses=[bad_response, AIMessage(content="Complete response")])
        agent = make_agent(runtime, client, cluster, identities[0], model)
        with pytest.raises(AgentRuntimeError):
            await agent.run("Complete this task", task_id=task_id)
        assert not (await client.get(f"clusters/{cluster['id']}/threads")).json()["items"]
        assert await agent.run("Complete this task", task_id=task_id) == "Complete response"
        assert model.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1, 2])
async def test_summary_uses_same_durable_model_budget(
    test_database_url,
    app,
    provision,
    monkeypatch,
    budget,
):
    from deepagents.middleware.summarization import SummarizationMiddleware

    from agentrpg import deep_agent

    def small_summary(model, backend, *, summarizer):
        return SummarizationMiddleware(
            model=model,
            backend=backend,
            trigger=("messages", 3),
            keep=("messages", 1),
            summarizer=summarizer,
        )

    monkeypatch.setattr(deep_agent, "create_summarization_middleware", small_summary)
    cluster, identities = await provision(count=1)
    async with (
        board_client(app, identities[0]["token"]) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        model = ScriptedModel(
            responses=[
                AIMessage(content="First answer"),
                AIMessage(content="Summary of first turn"),
                AIMessage(content="Second answer"),
            ]
        )
        agent = make_agent(runtime, client, cluster, identities[0], model, max_model_calls=budget)
        await agent.run("First request", task_id=uuid4())
        task_id = uuid4()
        if budget == 1:
            with pytest.raises(AgentLimitError):
                await agent.run("Second request", task_id=task_id)
            assert model.calls == 2  # First turn plus the second turn's summary.
        else:
            assert await agent.run("Second request", task_id=task_id) == "Second answer"
            snapshot = await agent.graph.aget_state(agent.config)
            assert snapshot.values["_summarization_event"]["summary_message"].content
            assert model.calls == 3
        async with runtime.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT model_calls FROM agent_tasks WHERE session_key=%s AND task_id=%s",
                (agent.session_key, task_id),
            )
            assert (await cursor.fetchone())["model_calls"] == budget
