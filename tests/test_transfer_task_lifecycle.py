"""Real PostgreSQL business work, authority, recovery and graph integration."""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_deep_agent import ScriptedModel, call

from agentrpg.activation import ActivationEnvelope, OwnerMessage
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime, AgentTaskConflictError
from agentrpg.director import Director, row, save_message
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxService
from agentrpg.run_branches import TABLES, capture_boundary, clone_snapshot
from agentrpg.transfer_task_demo import run_lifecycle_case
from agentrpg.transfer_task_schema import (
    CompleteTransferTask,
    DeferTransferTask,
    TransferTaskRequest,
)
from agentrpg.transfer_tasks import TransferTaskService
from agentrpg.transfer_validation import validation_report, validation_scenario
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.world import WorldError


async def prepared(pool, *, authorized=False):
    director = Director(pool)
    manifest = await director.create_run(validation_scenario("ambiguous"))
    run_id = UUID(manifest["run_id"])
    service = TransferTaskService(pool)
    await service.enable(run_id)
    await director.control(run_id, "start")
    async with pool.connection() as conn:
        message = await save_message(
            conn, run_id, "alice", "owner", "请核实金额后归还午饭钱。", uuid4(), 0
        )
    task = await service.owner_request(
        run_id,
        "alice",
        message["id"],
        uuid4(),
        TransferTaskRequest(
            kind="open",
            task_key="lunch",
            recipient_owner_id="bob",
            description="Repay lunch once verified",
            blocked_on="inbox_received",
        ),
    )
    grant = TransferArguments(
        authorization_id=uuid4(), recipient_owner_id="bob", amount=10, memo="Lunch"
    )
    if authorized:
        await TransferService(pool).authorize(
            run_id, "alice", message["id"], grant, task_key="lunch"
        )
    return manifest, task, grant, message


async def cancel(pool, run_id, *, owner="alice", message_id=None, request_id=None):
    if message_id is None:
        async with pool.connection() as conn:
            message = await save_message(
                conn, run_id, owner, "owner", "Cancel this unpaid task.", uuid4(), 0
            )
        message_id = message["id"]
    return await TransferTaskService(pool).owner_request(
        run_id,
        owner,
        message_id,
        request_id or uuid4(),
        TransferTaskRequest(kind="cancel", task_key="lunch"),
    )


@pytest.mark.parametrize("case", ["ack", "amount", "cancel"])
async def test_three_director_branches_preserve_business_state(app, case):
    report = await run_lifecycle_case(app, case)
    assert report["passed"], report["checks"]
    assert report["model_calls"] == 0 and not report["autonomous_agent_behavior_verified"]
    assert all(j["status"] == "completed" for j in report["jobs"])


async def test_task_open_retry_conflict_and_parallel_owner_commands(pool):
    manifest, task, _, message = await prepared(pool)
    run = UUID(manifest["run_id"])
    async with pool.connection() as conn:
        message = await save_message(
            conn, run, "alice", "owner", "Cancel this unpaid task.", uuid4(), 0
        )
    command = TransferTaskRequest(kind="cancel", task_key="lunch")
    op = uuid4()
    replies = await asyncio.gather(
        *(
            TransferTaskService(pool).owner_request(run, "alice", message["id"], op, command)
            for _ in range(8)
        )
    )
    assert all(r == replies[0] for r in replies)
    assert replies[0]["status"] == "cancelled"
    with pytest.raises(WorldError, match="different arguments"):
        await TransferTaskService(pool).owner_request(
            run,
            "alice",
            message["id"],
            op,
            TransferTaskRequest(
                kind="open",
                task_key="other",
                recipient_owner_id="bob",
                description="Other",
                blocked_on="owner_message",
            ),
        )
    assert len(await TransferTaskService(pool).list(run, "alice")) == 1


async def test_owner_cancellation_cannot_come_from_peer_or_assistant_message(pool):
    manifest, _, _, _ = await prepared(pool)
    run = UUID(manifest["run_id"])
    async with pool.connection() as conn:
        peer = await save_message(conn, run, "bob", "owner", "Cancel Alice's task.", uuid4(), 0)
        assistant = await save_message(conn, run, "alice", "agent", "Cancel it.", uuid4(), 0)
    for message in (peer, assistant):
        with pytest.raises(WorldError) as error:
            await cancel(pool, run, message_id=message["id"])
        assert error.value.status_code == 403
    assert (await TransferTaskService(pool).list(run, "alice"))[0]["status"] == "blocked"


async def test_new_mail_and_replayed_old_mail_are_not_payment_authority(pool):
    manifest, task, grant, _ = await prepared(pool)
    run = UUID(manifest["run_id"])
    inbox = InboxService(pool)
    mail_op = uuid4()
    mail = await inbox.send(run, "bob", "alice", "Verified", "Ten credits", mail_op)
    current = (await TransferTaskService(pool).list(run, "alice"))[0]
    assert current["status"] == "active" and str(current["review_mail_id"]) == str(mail["id"])
    assert not (await TransferService(pool).account(run, "alice"))["authorizations"]
    with pytest.raises(WorldError) as error:
        await TransferService(pool).transfer(run, "alice", uuid4(), grant)
    assert error.value.status_code == 403
    args = DeferTransferTask(task_id=task["id"], wait_for="owner_message")
    await TransferTaskService(pool).update(run, "alice", uuid4(), "defer", args)
    assert await inbox.send(run, "bob", "alice", "Verified", "Ten credits", mail_op) == mail
    assert (await TransferTaskService(pool).list(run, "alice"))[0]["blocked_on"] == [
        "owner_message"
    ]


async def test_cancelled_authorization_cannot_be_spent_or_reactivated(pool):
    manifest, _, grant, message = await prepared(pool, authorized=True)
    run = UUID(manifest["run_id"])
    await cancel(pool, run)
    with pytest.raises(WorldError) as error:
        await TransferService(pool).transfer(run, "alice", uuid4(), grant)
    assert error.value.status_code == 403
    await TransferService(pool).authorize(run, "alice", message["id"], grant, task_key="lunch")
    assert (await TransferTaskService(pool).list(run, "alice"))[0]["status"] == "cancelled"
    assert (await TransferService(pool).account(run, "alice"))["balance"] == 100


async def test_payment_cancel_race_preserves_one_committed_outcome(pool):
    manifest, _, grant, _ = await prepared(pool, authorized=True)
    run = UUID(manifest["run_id"])
    outcomes = await asyncio.gather(
        TransferService(pool).transfer(run, "alice", uuid4(), grant),
        cancel(pool, run),
        return_exceptions=True,
    )
    assert sum(isinstance(r, WorldError) for r in outcomes) == 1
    account = await TransferService(pool).account(run, "alice")
    task = (await TransferTaskService(pool).list(run, "alice"))[0]
    assert (
        task["status"] == "cancelled" and not account["transfers"] and account["balance"] == 100
    ) or (
        task["status"] == "active" and len(account["transfers"]) == 1 and account["balance"] == 90
    )


async def paid_with_notices(pool):
    manifest, task, grant, _ = await prepared(pool, authorized=True)
    run = UUID(manifest["run_id"])
    receipt = await TransferService(pool).transfer(run, "alice", uuid4(), grant)
    notice = await InboxService(pool).send(
        run, "alice", "bob", "Payment", "Repaid ten credits.", uuid4()
    )
    async with pool.connection() as conn:
        report = await save_message(
            conn, run, "alice", "agent", "Your payment committed.", uuid4(), 0
        )
    args = CompleteTransferTask(
        task_id=task["id"],
        receipt_id=receipt["request_id"],
        recipient_notice_id=notice["id"],
        owner_report_id=report["id"],
    )
    return manifest, task, grant, args


async def test_completion_needs_exact_real_receipt_notice_and_report(pool):
    manifest, task, _, _ = await prepared(pool)
    run = UUID(manifest["run_id"])
    args = CompleteTransferTask(
        task_id=task["id"], receipt_id=uuid4(), recipient_notice_id=uuid4(), owner_report_id=uuid4()
    )
    with pytest.raises(WorldError, match="committed payment"):
        await TransferTaskService(pool).update(run, "alice", uuid4(), "complete", args)
    manifest, _, _, args = await paid_with_notices(pool)
    run = UUID(manifest["run_id"])
    for field in ("receipt_id", "recipient_notice_id", "owner_report_id"):
        with pytest.raises(WorldError):
            await TransferTaskService(pool).update(
                run, "alice", uuid4(), "complete", args.model_copy(update={field: uuid4()})
            )
    result = await TransferTaskService(pool).update(run, "alice", uuid4(), "complete", args)
    assert result["status"] == "completed" and len(result["completion_evidence"]) == 3
    with pytest.raises(WorldError, match="already-paid"):
        await cancel(pool, run)


async def test_completed_operation_replay_after_pause_new_pool_and_no_double_payment(
    pool, test_database_url
):
    manifest, _, grant, args = await paid_with_notices(pool)
    run = UUID(manifest["run_id"])
    operation = uuid4()
    result = await TransferTaskService(pool).update(run, "alice", operation, "complete", args)
    await Director(pool).control(run, "pause")
    async with AsyncConnectionPool(
        test_database_url, kwargs={"row_factory": dict_row}, open=False
    ) as replacement:
        assert (
            await TransferTaskService(replacement).update(run, "alice", operation, "complete", args)
            == result
        )
        replay = await TransferService(replacement).transfer(run, "alice", args.receipt_id, grant)
        assert str(replay["request_id"]) == str(args.receipt_id)
        await Director(replacement).control(run, "resume")
        assert await TransferService(replacement).transfer(run, "alice", uuid4(), grant) == replay
        assert len((await TransferService(replacement).account(run, "alice"))["transfers"]) == 1


async def test_agent_http_scope_and_no_owner_command_endpoint(app, pool):
    manifest, task, _, _ = await prepared(pool)
    run = UUID(manifest["run_id"])
    other, _, _, _ = await prepared(pool)
    for member in manifest["participants"]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer " + member["token"]},
        ) as client:
            adapter = EnvironmentTools(client, run, track_transfer_tasks=True)
            listed = await adapter.execute("list_transfer_tasks", {}, "list")
            assert listed["ok"]
            assert len(listed["data"]["items"]) == (1 if member["owner_id"] == "alice" else 0)
            assert (await client.get(f"runs/{other['run_id']}/transfer-tasks")).status_code == 404
            if member["owner_id"] == "bob":
                result = await adapter.execute(
                    "defer_transfer_task",
                    {"task_id": task["id"], "wait_for": "owner_message"},
                    "foreign",
                )
                assert result["error"]["status"] == 404
            assert (
                await client.post(f"runs/{run}/transfer-tasks/cancel", json={})
            ).status_code == 404


async def test_real_graph_task_tool_write_replay_and_profile_binding(app, pool, test_database_url):
    manifest, task, _, _ = await prepared(pool)
    run = UUID(manifest["run_id"])
    member = manifest["participants"][0]
    message = OwnerMessage(message_id=uuid4(), content="Wait until I confirm the payment.")
    envelope = ActivationEnvelope(
        run_id=run,
        participant_id=UUID(member["agent_id"]),
        activation_id=uuid4(),
        window_id=0,
        source_event_ids=[message.message_id],
        owner_messages=[message],
    )
    responses = [
        AIMessage(
            content="", tool_calls=[{"name": "list_transfer_tasks", "args": {}, "id": "tasks"}]
        ),
        call("defer_transfer_task", {"task_id": task["id"], "wait_for": "owner_message"}, "defer"),
        call("finish_activation", {"status": "finished"}, "finish"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + member["token"]},
    ) as client:
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(responses=responses)
            agent = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, track_transfer_tasks=True),
                agent_id=member["agent_id"],
                session_id=member["session_id"],
                instructions="Track your task.",
            )
            result = await agent.run_activation(envelope)
            assert result.status == "finished" and model.calls == 3
            assert len(agent.events) == 2, [
                (getattr(m, "name", None), m.content) for m in agent.messages if m.type == "tool"
            ]
            assert all(e["result"]["ok"] for e in agent.events), agent.events
            assert (await TransferTaskService(pool).list(run, "alice"))[0]["blocked_on"] == [
                "owner_message"
            ]
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel()
            restored = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, track_transfer_tasks=True),
                agent_id=member["agent_id"],
                session_id=member["session_id"],
                instructions="Track your task.",
            )
            assert await restored.run_activation(envelope) == result and model.calls == 0
            incompatible = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run),
                agent_id=member["agent_id"],
                session_id=member["session_id"],
                instructions="Track your task.",
            )
            with pytest.raises(AgentTaskConflictError):
                await incompatible.run_activation(envelope)


@pytest.mark.parametrize("stage", ["blocked", "authorized", "paid", "completed"])
async def test_task_snapshot_clone_isolated_and_old_boundary_compatible(pool, stage):
    if stage in {"paid", "completed"}:
        manifest, _, _, args = await paid_with_notices(pool)
        if stage == "completed":
            await TransferTaskService(pool).update(
                UUID(manifest["run_id"]), "alice", uuid4(), "complete", args
            )
    else:
        manifest, _, _, _ = await prepared(pool, authorized=stage == "authorized")
    run = UUID(manifest["run_id"])
    await capture_boundary(pool, run)
    async with pool.connection() as conn:
        snapshot = (await row(conn, "SELECT snapshot FROM run_boundaries WHERE run_id=%s", (run,)))[
            "snapshot"
        ]
    child = await Director(pool).create_run(validation_scenario("ambiguous"))
    child["configuration"] = {"instructions": "Help your owner."}
    child_id = UUID(child["run_id"])
    async with pool.connection() as conn, conn.transaction():
        for table in reversed(TABLES):
            if table not in {"messages", "message_receipts"}:
                await conn.execute(f"DELETE FROM {table} WHERE run_id=%s", (child_id,))
        await clone_snapshot(conn, snapshot, child_id, child)
    copied = (await TransferTaskService(pool).list(child_id, "alice"))[0]
    source = (await TransferTaskService(pool).list(run, "alice"))[0]
    assert str(copied["run_id"]) == str(child_id) and copied["status"] == source["status"]
    await Director(pool).control(child_id, "start")
    if stage in {"blocked", "authorized"}:
        await cancel(pool, child_id)
    else:
        with pytest.raises(WorldError):
            await cancel(pool, child_id)
    assert (await TransferTaskService(pool).list(run, "alice"))[0] == source


async def test_new_profile_evaluation_retains_raw_activation_diagnostic(app, pool):
    local = await run_lifecycle_case(app, "ack")
    report = await validation_report(pool, UUID(local["run_id"]), "ambiguous")
    assert report["validation_revision"] == "persistent-transfer-task-v4"
    assert report["activation_only_wait_check"] is False
    assert report["checks"]["payer_persistent_business_wait"] is True
    assert "payer_declared_unresolved_wait" not in report["checks"]
    assert report["passed"], report["checks"]


async def test_complete_tool_http_replay_uses_actual_ids(app, pool):
    manifest, _, _, args = await paid_with_notices(pool)
    member = manifest["participants"][0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + member["token"]},
    ) as client:
        adapter = EnvironmentTools(client, manifest["run_id"], track_transfer_tasks=True)
        response = await adapter.execute(
            "complete_transfer_task", args.model_dump(mode="json"), "complete"
        )
        assert response["ok"] and response["data"]["status"] == "completed"
        assert (
            await adapter.execute(
                "complete_transfer_task", args.model_dump(mode="json"), "complete"
            )
            == response
        )


async def test_default_profile_has_no_task_tools_or_actor_effects(app, pool):
    manifest = await Director(pool).create_run(validation_scenario("ambiguous"))
    member = manifest["participants"][0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + member["token"]},
    ) as client:
        adapter = EnvironmentTools(client, manifest["run_id"])
        assert "list_transfer_tasks" not in {d["function"]["name"] for d in adapter.definitions()}
        assert not (await adapter.execute("list_transfer_tasks", {}, "unoffered"))["ok"]
    await Director(pool).control(UUID(manifest["run_id"]), "start")
    with pytest.raises(WorldError, match="before starting"):
        await TransferTaskService(pool).enable(UUID(manifest["run_id"]))
