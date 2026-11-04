"""Activation contracts with real Deep Agents graphs and PostgreSQL receipts.

Environment HTTP fixtures test the client/runtime boundary. Director integration
tests cover the actual environment routes, storage, and settlement service.
"""

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from test_deep_agent import ScriptedModel, call

from agentrpg.activation import ActivationEnvelope, ActivationResult, OwnerMessage, WorldObservation
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import (
    AgentIdentityError,
    AgentRuntime,
    AgentRuntimeError,
    AgentTaskConflictError,
)
from agentrpg.environment_tools import EnvironmentTools


class EnvironmentTransport(httpx.AsyncBaseTransport):
    def __init__(self, app, cluster, identity, run_id, session_id):
        self.board = httpx.ASGITransport(app=app)
        self.view = {
            "run_id": str(run_id),
            "cluster_id": cluster["id"],
            "agent_id": identity["agent"]["id"],
            "session_id": str(session_id),
        }
        self.actions = {}
        self.requests = []

    async def handle_async_request(self, request):
        if "/runs/" not in request.url.path:
            return await self.board.handle_async_request(request)
        self.requests.append(request)
        if request.url.path.endswith("/view"):
            return httpx.Response(200, json=self.view)
        if request.url.path.endswith("/actions") and request.method == "POST":
            body = json.loads(request.content)
            action = self.actions.setdefault(
                body["request_id"], {"id": str(uuid4()), "status": "pending", **body}
            )
            return httpx.Response(201, json=action)
        if "/actions/" in request.url.path:
            return httpx.Response(
                200, json={"id": request.url.path.rsplit("/", 1)[1], "status": "pending"}
            )
        if request.url.path.endswith("/owner/messages"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"assets": []})


@asynccontextmanager
async def agent_setup(app, provision, test_database_url, responses, **kwargs):
    cluster, identities = await provision(count=1)
    identity, session, run_id = identities[0], uuid4(), uuid4()
    transport = EnvironmentTransport(app, cluster, identity, run_id, session)
    async with (
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test/api/v1/",
            headers={"Authorization": f"Bearer {identity['token']}"},
        ) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        model = ScriptedModel(responses=responses)
        agent = runtime.agent(
            model=model,
            board=BoardTools(client, cluster["id"]),
            environment=EnvironmentTools(client, run_id),
            agent_id=identity["agent"]["id"],
            session_id=session,
            instructions="Help your owner and use the tools.",
            **kwargs,
        )
        envelope = ActivationEnvelope(
            run_id=run_id,
            participant_id=UUID(identity["agent"]["id"]),
            activation_id=uuid4(),
            window_id=0,
            owner_messages=(OwnerMessage(message_id=uuid4(), content="Please reserve transport."),),
        )
        yield agent, envelope, model, transport


def test_envelope_provenance_validation():
    event = uuid4()
    fields = dict(run_id=uuid4(), participant_id=uuid4(), activation_id=uuid4(), window_id=0)
    observation = WorldObservation(event_id=event, kind="reservation_settled", content="Accepted")
    with pytest.raises(ValidationError, match="source_event_ids"):
        ActivationEnvelope(**fields, world_observations=(observation,))
    envelope = ActivationEnvelope(
        **fields, source_event_ids=(event,), world_observations=(observation,)
    )
    assert ActivationEnvelope.model_validate_json(envelope.model_dump_json()) == envelope
    with pytest.raises(ValidationError):
        ActivationEnvelope(**{**fields, "window_id": True})
    with pytest.raises(ValidationError):
        ActivationEnvelope(**fields, hidden_plot="secret")
    with pytest.raises(ValidationError):
        ActivationResult(status="finished", pending_ids=(uuid4(),))


@pytest.mark.asyncio
async def test_activation_plain_response_replay_and_input_conflict(
    app, provision, test_database_url
):
    async with agent_setup(
        app, provision, test_database_url, [AIMessage(content="I will check transport.")]
    ) as (agent, envelope, model, transport):
        result = await agent.run_activation(envelope)
        assert result == ActivationResult(status="responded", text="I will check transport.")
        assert await agent.run_activation(envelope) == result
        assert model.calls == 1
        prompt = next(item.content for item in model.seen if isinstance(item, HumanMessage))
        assert "owner_messages" in prompt and "world_observations" in prompt
        assert "Authorization" not in prompt
        with pytest.raises(AgentTaskConflictError, match="different text"):
            await agent.run_activation(envelope.model_copy(update={"window_id": 1}))
        with pytest.raises(AgentIdentityError):
            await agent.run_activation(envelope.model_copy(update={"participant_id": uuid4()}))


@pytest.mark.asyncio
async def test_pending_action_yields_and_next_activation_runs(app, provision, test_database_url):
    async with agent_setup(
        app,
        provision,
        test_database_url,
        [
            call("submit_world_action", {"kind": "reserve", "asset_id": "car"}),
            AIMessage(content="Done."),
        ],
        max_model_calls=1,
    ) as (agent, envelope, model, transport):
        result = await agent.run_activation(envelope)
        assert result.status == "waiting"
        assert result.text == ""  # An automatic pending receipt is not owner dialogue.
        assert len(result.pending_ids) == 1
        assert model.calls == 1
        assert len(transport.actions) == 1
        async with agent.runtime.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT status,result FROM agent_tasks WHERE session_key=%s AND task_id=%s",
                (agent.session_key, envelope.activation_id),
            )
            saved = await cursor.fetchone()
            assert saved["status"] == "completed"
            assert ActivationResult.model_validate_json(saved["result"]) == result
        assert await agent.run_activation(envelope) == result
        next_envelope = envelope.model_copy(update={"activation_id": uuid4(), "window_id": 1})
        assert (await agent.run_activation(next_envelope)).text == "Done."
        assert model.calls == 2


@pytest.mark.asyncio
async def test_pending_read_stops_polling(app, provision, test_database_url):
    pending_id = uuid4()
    async with agent_setup(
        app,
        provision,
        test_database_url,
        [call("read_world_action", {"action_id": str(pending_id)})],
    ) as (agent, envelope, model, transport):
        result = await agent.run_activation(envelope)
        assert result.pending_ids == (pending_id,)
        assert result.text == ""
        assert model.calls == 1
        assert len([r for r in transport.requests if "/actions/" in r.url.path]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["", "Your request is submitted; I will confirm the result later."]
)
@pytest.mark.parametrize("reverse_tools", [False, True])
async def test_explicit_wait_completed_and_parallel_pending_overrides_finished(
    app, provision, test_database_url, text, reverse_tools
):
    async with agent_setup(
        app,
        provision,
        test_database_url,
        [call("finish_activation", {"status": "waiting", "text": "Waiting for my owner."})],
    ) as (agent, envelope, model, transport):
        result = await agent.run_activation(envelope)
        assert result.status == "waiting" and model.calls == 1
        assert result.text == "Waiting for my owner."
        tool_calls = [
            {
                "name": "finish_activation",
                "id": "finish",
                "args": {"status": "finished", "text": text},
            },
            {
                "name": "submit_world_action",
                "id": "reserve",
                "args": {"kind": "reserve", "asset_id": "car"},
            },
        ]
        if reverse_tools:
            tool_calls.reverse()
        model.responses.append(
            AIMessage(
                content="",
                tool_calls=tool_calls,
            )
        )
        result = await agent.run_activation(envelope.model_copy(update={"activation_id": uuid4()}))
        assert result.status == "waiting" and len(result.pending_ids) == 1
        assert result.text == text


@pytest.mark.asyncio
async def test_activation_recovery_after_action_commit_and_tool_failure(
    app, provision, test_database_url
):
    async with agent_setup(
        app,
        provision,
        test_database_url,
        [call("submit_world_action", {"kind": "reserve", "asset_id": "car"})],
    ) as (agent, envelope, model, transport):
        execute = agent.environment.execute

        async def commit_and_fail(*args, **kwargs):
            await execute(*args, **kwargs)
            raise AgentRuntimeError("Simulated interruption")

        agent.environment.execute = commit_and_fail
        with pytest.raises(AgentRuntimeError, match="Simulated"):
            await agent.run_activation(envelope)
        agent.environment.execute = execute
        result = await agent.run_activation(envelope)
        assert result.status == "waiting"
        assert model.calls == 1
        assert len(transport.actions) == 1
        posts = [json.loads(r.content) for r in transport.requests if r.method == "POST"]
        assert len(posts) == 2 and posts[0]["request_id"] == posts[1]["request_id"]


@pytest.mark.asyncio
async def test_activation_rejects_wrong_session_and_changed_scope(
    app, provision, test_database_url
):
    async with agent_setup(app, provision, test_database_url, [AIMessage(content="Saved")]) as (
        agent,
        envelope,
        model,
        transport,
    ):
        transport.view["session_id"] = str(uuid4())
        with pytest.raises(AgentIdentityError):
            await agent.run_activation(envelope)
        transport.view["session_id"] = str(agent.session_id)
        await agent.run_activation(envelope)
        changed = uuid4()
        agent.environment.run_id = str(changed)
        transport.view["run_id"] = str(changed)
        # Recreate with another scoped run, exactly as a new worker would.
        replacement = agent.runtime.agent(
            model=ScriptedModel(),
            board=agent.board,
            environment=EnvironmentTools(agent.board.client, changed),
            agent_id=agent.agent_id,
            session_id=agent.session_id,
            instructions=agent.instructions,
        )
        with pytest.raises(AgentTaskConflictError, match="instructions"):
            await replacement.run_activation(
                envelope.model_copy(
                    update={
                        "run_id": changed,
                        "activation_id": uuid4(),
                    }
                )
            )


@pytest.mark.asyncio
async def test_environment_adapter_ids_permissions_and_error_sanitizing():
    seen = []

    def respond(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(403, json={"detail": "secret-provider-and-agent-token"})
        return httpx.Response(200, json={"id": str(uuid4()), "status": "pending"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test/"
    ) as client:
        tools = EnvironmentTools(client, uuid4(), uuid4())
        args = {"kind": "reserve", "asset_id": "car"}
        await tools.execute("submit_world_action", args, "same-call")
        await tools.execute("submit_world_action", args, "same-call")
        assert json.loads(seen[0].content) == json.loads(seen[1].content)
        assert not (
            await tools.execute("submit_world_action", {**args, "owner_id": str(uuid4())}, "bad")
        )["ok"]
        assert len(seen) == 2
        result = await tools.execute("inspect_world", {}, "read")
        assert result["error"]["status"] == 403
        assert "secret" not in json.dumps(result)
        for definition in tools.definitions():
            properties = definition["function"]["parameters"].get("properties", {})
            assert not {"owner_id", "agent_id", "run_id", "request_id", "token"} & set(properties)


@pytest.mark.asyncio
async def test_waiting_checkpoint_survives_new_runtime_before_receipt_save(
    app, provision, test_database_url
):
    async with agent_setup(
        app,
        provision,
        test_database_url,
        [call("submit_world_action", {"kind": "reserve", "asset_id": "car"})],
    ) as (agent, envelope, model, transport):
        result = await agent.run_activation(envelope)
        headers = dict(agent.board.client.headers)
        # Simulate loss of the receipt update after the graph's durable finish.
        async with agent.runtime.pool.connection() as connection:
            await connection.execute(
                """UPDATE agent_tasks SET status='pending', result=NULL, completed_at=NULL
                   WHERE session_key=%s AND task_id=%s""",
                (agent.session_key, envelope.activation_id),
            )
    async with (
        httpx.AsyncClient(
            transport=transport, base_url="http://test/api/v1/", headers=headers
        ) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        restored_model = ScriptedModel(responses=[AIMessage(content="Next window.")])
        restored = runtime.agent(
            model=restored_model,
            board=BoardTools(client, agent.board.cluster_id),
            environment=EnvironmentTools(client, envelope.run_id),
            agent_id=agent.agent_id,
            session_id=agent.session_id,
            instructions=agent.instructions,
        )
        assert await restored.run_activation(envelope) == result
        assert restored_model.calls == 0 and len(transport.actions) == 1
        next_envelope = envelope.model_copy(update={"activation_id": uuid4(), "window_id": 1})
        assert (await restored.run_activation(next_envelope)).text == "Next window."
        assert restored_model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_size", [4, None], ids=["four-connections", "default-pool"])
async def test_fifteen_independent_activations_wait_concurrently(
    app, provision, test_database_url, pool_size
):
    cluster, identities = await provision(count=15)
    run_id = uuid4()
    gate = asyncio.Event()
    async with AsyncExitStack() as stack:
        options = {} if pool_size is None else {"pool_size": pool_size}
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url, **options))
        assert runtime.pool.max_size == (8 if pool_size is None else pool_size)
        assert runtime.lock_pool.max_size == 15
        agents, envelopes, entered = [], [], []
        for identity in identities:
            session = uuid4()
            transport = EnvironmentTransport(app, cluster, identity, run_id, session)
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test/api/v1/",
                    headers={"Authorization": f"Bearer {identity['token']}"},
                )
            )
            event = asyncio.Event()
            entered.append(event)
            agent = runtime.agent(
                model=ScriptedModel(
                    responses=[call("finish_activation", {"status": "waiting"})],
                    gate=gate,
                    entered=event,
                ),
                board=BoardTools(client, cluster["id"]),
                environment=EnvironmentTools(client, run_id),
                agent_id=identity["agent"]["id"],
                session_id=session,
                instructions="Help your owner.",
            )
            agents.append(agent)
            envelopes.append(
                ActivationEnvelope(
                    run_id=run_id,
                    participant_id=agent.agent_id,
                    activation_id=uuid4(),
                    window_id=0,
                )
            )
        tasks = [
            asyncio.create_task(agent.run_activation(envelope))
            for agent, envelope in zip(agents, envelopes, strict=True)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), timeout=10)
        finally:
            gate.set()
        results = await asyncio.gather(*tasks)
        assert len(results) == 15 and all(item.status == "waiting" for item in results)
        async with runtime.pool.connection() as connection:
            cursor = await connection.execute(
                """SELECT count(*) AS total FROM agent_tasks t
                   JOIN agent_sessions s USING(session_key)
                   WHERE s.cluster_id=%s AND t.status='completed'""",
                (cluster["id"],),
            )
            assert (await cursor.fetchone())["total"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args,suffix,method",
    [
        ("read_run_view", {}, "view", "GET"),
        ("read_owner_messages", {}, "owner/messages", "GET"),
        ("send_owner_message", {"content": "May I spend 30 credits?"}, "owner/messages", "POST"),
        ("inspect_world", {}, "world", "GET"),
        (
            "submit_world_action",
            {"kind": "cancel", "asset_id": "car", "expected_version": 0},
            "actions",
            "POST",
        ),
    ],
)
async def test_environment_adapter_paths(name, args, suffix, method):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    run_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test/"
    ) as client:
        adapter = EnvironmentTools(client, run_id)
        assert (await adapter.execute(name, args, "path-test"))["ok"]
    assert requests[0].url.path == f"/runs/{run_id}/{suffix}"
    assert requests[0].method == method
    if method == "POST":
        payload = json.loads(requests[0].content)
        assert "request_id" in payload
        assert {key: value for key, value in payload.items() if key != "request_id"} == args
