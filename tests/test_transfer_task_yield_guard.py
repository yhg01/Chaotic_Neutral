"""Persist real business waits before ending an activation; preserve legacy profiles."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_deep_agent import ScriptedModel
from test_transfer_task_lifecycle import paid_with_notices, prepared

from agentrpg.activation import ActivationEnvelope, WorldObservation
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import (
    AgentLimitError,
    AgentRuntime,
    AgentRuntimeError,
    AgentTaskConflictError,
)
from agentrpg.director import Director, row
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxService
from agentrpg.transfer_tasks import TransferTaskService


def tool(name, arguments):
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": str(uuid4())}])


async def active_task(pool):
    manifest, task, _, _ = await prepared(pool)
    run = UUID(manifest["run_id"])
    mail = await InboxService(pool).send(run, "bob", "alice", "Verified", "Ten credits.", uuid4())
    event = next(e for e in await Director(pool).trace(run) if e["kind"] == "inbox_received")
    member = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    envelope = ActivationEnvelope(
        run_id=run,
        participant_id=UUID(member["agent_id"]),
        activation_id=uuid4(),
        window_id=0,
        source_event_ids=[event["id"]],
        world_observations=[
            WorldObservation(
                event_id=event["id"], kind="inbox_received", content=json.dumps(event["payload"])
            )
        ],
    )
    return manifest, task, mail, envelope


def client_for(app, manifest):
    member = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + member["token"]},
    )


def make_agent(runtime, client, manifest, model, *, guard=True, limit=12):
    member = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    return runtime.agent(
        model=model,
        board=BoardTools(client, manifest["cluster_id"]),
        environment=EnvironmentTools(client, manifest["run_id"], track_transfer_tasks=True),
        agent_id=member["agent_id"],
        session_id=member["session_id"],
        instructions="Use the persistent task tools and actual receipts.",
        max_model_calls=limit,
        guard_transfer_task_yield=guard,
    )


@pytest.mark.parametrize("finish", ["finish_activation", "wait_for_owner", "wait_for_mail"])
async def test_wait_tool_requires_explicit_task_update_before_yield(
    app, pool, test_database_url, finish
):
    manifest, task, mail, envelope = await active_task(pool)
    args = (
        {"status": "waiting", "wait_for": ["owner_message"]}
        if finish == "finish_activation"
        else {}
    )
    model = ScriptedModel(
        responses=[
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool(finish, args),
            tool("defer_transfer_task", {"task_id": task["id"], "wait_for": "owner_message"}),
            tool("finish_activation", {"status": "waiting", "wait_for": ["owner_message"]}),
        ]
    )
    async with client_for(app, manifest) as client, AgentRuntime(test_database_url) as runtime:
        agent = make_agent(runtime, client, manifest, model)
        # Dedicated wait tools are opt-in with the existing completion mode.
        # Rebuild before the first task so the session policy remains consistent.
        if finish != "finish_activation":
            member = manifest["participants"][0]
            agent = runtime.agent(
                model=model,
                board=agent.board,
                environment=agent.environment,
                agent_id=member["agent_id"],
                session_id=member["session_id"],
                instructions=agent.instructions,
                guard_transfer_task_yield=True,
                require_explicit_finish=True,
            )
        result = await agent.run_activation(envelope)
        assert result.status == "waiting" and result.wait_for == ("owner_message",)
        corrections = [
            json.loads(m.content)
            for m in agent.messages
            if isinstance(m, ToolMessage) and "active_transfer_task_requires_update" in m.content
        ]
        assert len(corrections) == 1
        assert corrections[0]["error"]["tasks"][0]["id"] == task["id"]
        assert model.calls == 4
    assert (await TransferTaskService(pool).list(envelope.run_id, "alice"))[0]["blocked_on"] == [
        "owner_message"
    ]


async def test_guard_keeps_model_finished_after_a_real_task_deferral(app, pool, test_database_url):
    manifest, task, mail, envelope = await active_task(pool)
    model = ScriptedModel(
        responses=[
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("finish_activation", {"status": "finished"}),
            tool("defer_transfer_task", {"task_id": task["id"], "wait_for": "owner_message"}),
            tool("finish_activation", {"status": "finished"}),
        ]
    )
    async with client_for(app, manifest) as client, AgentRuntime(test_database_url) as runtime:
        agent = make_agent(runtime, client, manifest, model)
        result = await agent.run_activation(envelope)
        assert result.status == "finished" and not result.wait_for
        assert model.calls == 4
    assert (await TransferTaskService(pool).list(envelope.run_id, "alice"))[0][
        "status"
    ] == "blocked"


async def test_guard_default_is_compatible_and_bound_to_session(app, pool, test_database_url):
    manifest, _, mail, envelope = await active_task(pool)
    model = ScriptedModel(
        responses=[
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("finish_activation", {"status": "waiting", "wait_for": ["owner_message"]}),
        ]
    )
    async with client_for(app, manifest) as client, AgentRuntime(test_database_url) as runtime:
        legacy = make_agent(runtime, client, manifest, model, guard=False)
        assert (await legacy.run_activation(envelope)).status == "waiting"
        assert model.calls == 2
        with pytest.raises(AgentTaskConflictError):
            await make_agent(runtime, client, manifest, ScriptedModel()).run_activation(envelope)


async def test_committed_deferral_survives_provider_failure_and_runtime_recreation(
    app, pool, test_database_url
):
    manifest, task, mail, envelope = await active_task(pool)
    model = ScriptedModel(
        responses=[
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("finish_activation", {"status": "waiting", "wait_for": ["owner_message"]}),
            tool("defer_transfer_task", {"task_id": task["id"], "wait_for": "owner_message"}),
            RuntimeError("Private provider details"),
        ]
    )
    async with client_for(app, manifest) as client:
        async with AgentRuntime(test_database_url) as runtime:
            with pytest.raises(AgentRuntimeError, match="model request failed"):
                await make_agent(runtime, client, manifest, model).run_activation(envelope)
        async with AgentRuntime(test_database_url) as runtime:
            repaired = ScriptedModel(
                responses=[
                    tool("finish_activation", {"status": "waiting", "wait_for": ["owner_message"]})
                ]
            )
            result = await make_agent(runtime, client, manifest, repaired).run_activation(envelope)
            assert result.status == "waiting" and repaired.calls == 1
    async with pool.connection() as conn:
        assert (
            await row(
                conn,
                "SELECT count(*) AS n FROM transfer_task_operations WHERE run_id=%s "
                "AND request_payload->>'kind'='defer'",
                (envelope.run_id,),
            )
        )["n"] == 1


async def test_repeated_unrepaired_yield_is_stopped_by_durable_call_limit(
    app, pool, test_database_url
):
    manifest, _, mail, envelope = await active_task(pool)
    model = ScriptedModel(
        responses=[
            tool("read_mail", {"message_id": str(mail["id"])}),
            tool("finish_activation", {"status": "finished"}),
            tool("finish_activation", {"status": "finished"}),
        ]
    )
    async with client_for(app, manifest) as client, AgentRuntime(test_database_url) as runtime:
        with pytest.raises(AgentLimitError):
            await make_agent(runtime, client, manifest, model, limit=2).run_activation(envelope)
        assert model.calls == 2
    assert (await TransferTaskService(pool).list(envelope.run_id, "alice"))[0]["status"] == "active"


async def test_evidence_error_gives_local_corrective_feedback_not_outage(app, pool):
    manifest, task, _, arguments = await paid_with_notices(pool)
    async with client_for(app, manifest) as client:
        adapter = EnvironmentTools(client, manifest["run_id"], track_transfer_tasks=True)
        wrong = arguments.model_copy(update={"owner_report_id": UUID(task["owner_message_id"])})
        result = await adapter.execute(
            "complete_transfer_task", wrong.model_dump(mode="json"), "wrong"
        )
        assert result["error"]["code"] == "missing_task_notice_or_report"
        assert "send_owner_message" in result["error"]["detail"]
        assert (
            await adapter.execute(
                "complete_transfer_task", arguments.model_dump(mode="json"), "correct"
            )
        )["ok"]


@pytest.mark.parametrize(
    "detail",
    [
        "SECRET",
        {"code": "unknown", "secret": "SECRET"},
        {"code": "invalid_task_receipt", "secret": "SECRET"},
    ],
)
async def test_task_error_never_forwards_raw_server_details(detail):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"detail": detail})),
        base_url="http://test/api/v1/",
    ) as client:
        adapter = EnvironmentTools(client, uuid4(), track_transfer_tasks=True)
        result = await adapter.execute(
            "complete_transfer_task",
            {
                "task_id": str(uuid4()),
                "receipt_id": str(uuid4()),
                "recipient_notice_id": str(uuid4()),
                "owner_report_id": str(uuid4()),
            },
            "bad",
        )
        assert not result["ok"] and "SECRET" not in json.dumps(result)
