"""Real PostgreSQL payment, rollback, authority and authenticated-tool checks."""

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool
from test_deep_agent import ScriptedModel, call

from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, row, save_message
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxService
from agentrpg.run_branches import TABLES, capture_boundary, clone_snapshot
from agentrpg.transfer_demo import prepare_task, run_local_check, scenario
from agentrpg.transfer_model_task import execute_model_task
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.world import WorldError, WorldService


async def test_local_task_actual_balances_and_messages(app):
    report = await run_local_check(app)
    assert report["passed"], report["checks"]
    assert report["model_calls"] == 0
    assert report["autonomous_agent_behavior_verified"] is False
    assert len(report["tool_trace"]) == 12
    assert (await Director(app.state.pool).get_run(UUID(report["run_id"])))["status"] == "paused"


async def test_simultaneous_retries_same_and_new_call_ids(pool):
    manifest, grant = await prepare_task(pool)
    run = UUID(manifest["run_id"])
    service = TransferService(pool)
    request = uuid4()
    receipts = await asyncio.gather(
        *(service.transfer(run, "alice", request if i < 5 else uuid4(), grant) for i in range(12))
    )
    assert all(result == receipts[0] for result in receipts)
    assert (await service.account(run, "alice"))["balance"] == 90
    assert (await service.account(run, "bob"))["balance"] == 30
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM credit_transfers"))["n"] == 1
        assert (await row(conn, "SELECT count(*) AS n FROM inbox_messages"))["n"] == 1
        assert (
            await row(conn, "SELECT count(*) AS n FROM run_events WHERE kind='transfer_completed'")
        )["n"] == 2
    assert WorldService.replay(await service.world.journal(run)) == await service.world.snapshot(
        run
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": 11},
        {"recipient_owner_id": "alice"},
        {"memo": "another task"},
    ],
)
async def test_exact_authority_and_changed_retry(pool, changes):
    manifest, grant = await prepare_task(pool)
    run = UUID(manifest["run_id"])
    service = TransferService(pool)
    changed = TransferArguments.model_validate({**grant.model_dump(), **changes})
    with pytest.raises(WorldError) as error:
        await service.transfer(run, "alice", uuid4(), changed)
    assert error.value.status_code == 403
    request = uuid4()
    await service.transfer(run, "alice", request, grant)
    with pytest.raises(WorldError) as error:
        await service.transfer(run, "alice", request, changed)
    assert error.value.status_code == 409
    assert (await service.account(run, "alice"))["balance"] == 90


async def test_no_grant_or_using_other_owners_grant(pool):
    manifest, grant = await prepare_task(pool)
    service = TransferService(pool)
    run = UUID(manifest["run_id"])
    for owner, arguments in (
        ("bob", grant),
        ("alice", grant.model_copy(update={"authorization_id": uuid4()})),
    ):
        with pytest.raises(WorldError) as error:
            await service.transfer(run, owner, uuid4(), arguments)
        assert error.value.status_code == 403
    assert (await service.account(run, "alice"))["balance"] == 100


async def test_owner_instruction_cannot_be_reauthorized(pool):
    manifest, grant = await prepare_task(pool)
    run = UUID(manifest["run_id"])
    service = TransferService(pool)
    message = (await Director(pool).messages(run, "alice"))[0]
    await service.authorize(run, "alice", message["id"], grant)
    with pytest.raises(WorldError):
        await service.authorize(
            run, "alice", message["id"], grant.model_copy(update={"authorization_id": uuid4()})
        )
    async with pool.connection() as conn:
        peer = await save_message(conn, run, "bob", "owner", "I approve", uuid4(), 0)
        assistant = await save_message(conn, run, "alice", "agent", "I approve", uuid4(), 0)
    for message in (peer, assistant):
        with pytest.raises(WorldError) as error:
            await service.authorize(run, "alice", message["id"], grant)
        assert error.value.status_code == 403


async def test_insufficient_balance_and_notification_failure_roll_back(pool, monkeypatch):
    manifest, grant = await prepare_task(pool)
    run = UUID(manifest["run_id"])
    service = TransferService(pool)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE world_accounts SET balance=5 WHERE run_id=%s AND owner_id='alice'", (run,)
        )
    with pytest.raises(WorldError, match="Insufficient"):
        await service.transfer(run, "alice", uuid4(), grant)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE world_accounts SET balance=100 WHERE run_id=%s AND owner_id='alice'", (run,)
        )
    before = await service.world.snapshot(run)

    async def fail_notice(*args, **kwargs):
        raise RuntimeError("Injected notification failure")

    monkeypatch.setattr(InboxService, "send", fail_notice)
    with pytest.raises(RuntimeError, match="Injected"):
        await service.transfer(run, "alice", uuid4(), grant)
    assert await service.world.snapshot(run) == before
    async with pool.connection() as conn:
        for table in ("world_requests", "credit_transfers", "inbox_messages"):
            assert (await row(conn, f"SELECT count(*) AS n FROM {table}"))["n"] == 0
        assert (
            await row(conn, "SELECT count(*) AS n FROM run_events WHERE kind='transfer_completed'")
        )["n"] == 0


async def test_pause_preserves_committed_retries_and_blocks_new_payments(pool):
    manifest, grant = await prepare_task(pool)
    run = UUID(manifest["run_id"])
    service = TransferService(pool)
    request = uuid4()
    receipt = await service.transfer(run, "alice", request, grant)
    await Director(pool).control(run, "pause")
    assert await service.transfer(run, "alice", request, grant) == receipt
    with pytest.raises(WorldError, match="not running"):
        await service.transfer(run, "alice", uuid4(), grant)


async def test_committed_records_survive_new_pool(pool, test_database_url):
    manifest, grant = await prepare_task(pool)
    run = UUID(manifest["run_id"])
    request = uuid4()
    receipt = await TransferService(pool).transfer(run, "alice", request, grant)
    async with AsyncConnectionPool(test_database_url, open=False) as other:
        await other.wait()
        restored = TransferService(other)
        assert await restored.transfer(run, "alice", request, grant) == receipt
        assert (await restored.account(run, "bob"))["balance"] == 30


async def test_http_tools_identity_privacy_and_validation(app, pool, client):
    manifest, grant = await prepare_task(pool)
    other, _ = await prepare_task(pool)
    participants = {item["owner_id"]: item for item in manifest["participants"]}
    base = f"/api/v1/runs/{manifest['run_id']}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + participants["alice"]["token"]},
    ) as http:
        adapter = EnvironmentTools(http, manifest["run_id"])
        own = await adapter.execute("inspect_transfer_account", {}, "balance")
        assert own["data"]["balance"] == 100
        assert "balance" not in json.dumps(own["data"]["authorizations"])
        assert (await http.get(f"runs/{other['run_id']}/transfer-account")).status_code == 404
        payload = grant.model_dump(mode="json")
        assert not (await adapter.execute("transfer_credits", {**payload, "amount": True}, "bad"))[
            "ok"
        ]
        assert (
            await http.post(
                f"runs/{manifest['run_id']}/transfers",
                json={**payload, "request_id": str(uuid4()), "owner_id": "bob"},
            )
        ).status_code == 422
        assert (await adapter.execute("transfer_credits", payload, "payment"))["ok"]
    assert (await client.get(base + "/transfer-account")).status_code == 403
    bob_headers = {"Authorization": "Bearer " + participants["bob"]["token"]}
    bob = (await client.get(base + "/transfer-account", headers=bob_headers)).json()
    assert bob["balance"] == 30 and bob["authorizations"] == []
    assert "sender_balance" not in json.dumps(bob) and "alice_balance" not in json.dumps(bob)


async def test_boundary_copies_transfer_authority_ledger_and_dedup(pool):
    manifest, grant = await prepare_task(pool)
    run = UUID(manifest["run_id"])
    request = uuid4()
    receipt = await TransferService(pool).transfer(run, "alice", request, grant)
    await capture_boundary(pool, run)
    async with pool.connection() as conn:
        snapshot = (await row(conn, "SELECT snapshot FROM run_boundaries WHERE run_id=%s", (run,)))[
            "snapshot"
        ]
    director = Director(pool)
    child = await director.create_run(scenario())
    child["configuration"] = {"instructions": "Help the assigned owner."}
    child_id = UUID(child["run_id"])
    async with pool.connection() as conn, conn.transaction():
        # The branch copier expects a newly provisioned, empty child domain.
        for table in reversed(TABLES):
            if table not in {"messages", "message_receipts"}:
                await conn.execute(f"DELETE FROM {table} WHERE run_id=%s", (child_id,))
        await clone_snapshot(conn, snapshot, child_id, child)
    await director.control(child_id, "start")
    service = TransferService(pool)
    assert (await service.account(child_id, "bob"))["balance"] == 30
    assert await service.transfer(child_id, "alice", uuid4(), grant) == receipt
    assert (await service.account(child_id, "alice"))["balance"] == 90


async def test_actual_deep_agent_graph_and_completed_task_replay(app, pool, test_database_url):
    manifest, grant = await prepare_task(pool)
    alice = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    task_id = uuid4()
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "inspect_transfer_account", "args": {}, "id": "inspect"}],
            ),
            call("transfer_credits", grant.model_dump(mode="json"), "transfer"),
            call(
                "send_mail",
                {
                    "recipient_owner_id": "bob",
                    "subject": "Lunch repayment",
                    "body": "I transferred 10 simulated credits for yesterday's lunch.",
                },
                "tell-bob",
            ),
            call(
                "send_owner_message",
                {"content": "Transferred 10 simulated credits to Bob."},
                "report",
            ),
            AIMessage(content="The committed transfer and notifications are saved."),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + alice["token"]},
    ) as http:
        async with AgentRuntime(test_database_url) as runtime:
            agent = runtime.agent(
                model=model,
                board=BoardTools(http, manifest["cluster_id"]),
                environment=EnvironmentTools(http, manifest["run_id"]),
                agent_id=alice["agent_id"],
                session_id=alice["session_id"],
                instructions="Complete the explicit owner instruction using committed results.",
            )
            task = "Repay Bob for lunch under my exact grant."
            result = await agent.run(task, task_id=task_id)
            calls = model.calls
            assert {"transfer_credits", "inspect_transfer_account"} <= set(model.offered)
            assert all(e["result"]["ok"] for e in agent.events)
            assert await agent.run(task, task_id=task_id) == result
            assert model.calls == calls == 5
    account = await TransferService(pool).account(UUID(manifest["run_id"]), "alice")
    assert account["balance"] == 90 and len(account["transfers"]) == 1


async def test_model_task_runner_evidence_and_natural_final_delivery(app, pool, test_database_url):
    manifest, grant = await prepare_task(pool)
    models = {
        "alice": ScriptedModel(
            responses=[
                call("transfer_credits", grant.model_dump(mode="json"), "payment"),
                call(
                    "send_mail",
                    {
                        "recipient_owner_id": "bob",
                        "subject": "Repaid",
                        "body": "Paid ten simulated credits for lunch.",
                    },
                    "notice",
                ),
                AIMessage(content="已向 Bob 转账 10 积分并通知他。"),
            ]
        ),
        "bob": ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "inspect_transfer_account", "args": {}, "id": "balance"}],
                ),
                call("list_mail", {"mailbox": "inbox"}, "mail"),
                AIMessage(content="已查到 Alice 的 10 积分入账，余额 30。"),
            ]
        ),
    }
    report = await execute_model_task(app, test_database_url, manifest, models)
    assert report["passed"], report["checks"]
    assert report["errors"] == []
    assert len(report["tool_trace"]) == 4
    assert report["owner_messages"]["alice"][-1]["content"] == "已向 Bob 转账 10 积分并通知他。"
    # Completed task replay does not generate another payment, mail or model call.
    again = await execute_model_task(app, test_database_url, manifest, models)
    assert again["passed"]
    assert sum(m.calls for m in models.values()) == 6
    assert len(again["recipient_mail"]) == 2
    models["alice"].responses = [
        call(
            "send_mail",
            {
                "recipient_owner_id": "bob",
                "subject": "Follow-up",
                "body": "The existing lunch payment is confirmed.",
            },
            "follow-up",
        ),
        AIMessage(content="已补发通知，没有再次转账。"),
    ]
    models["bob"].responses = [
        AIMessage(
            content="",
            tool_calls=[{"name": "inspect_transfer_account", "args": {}, "id": "verify-balance"}],
        ),
        call("list_mail", {"mailbox": "inbox"}, "new-mail"),
        AIMessage(content="已核实原入账和新通知。"),
    ]
    follow_up = {
        "alice": "Send Bob a follow-up about the existing payment. Do not pay again.",
        "bob": "Check the account and latest notices again.",
    }
    continued = await execute_model_task(
        app, test_database_url, manifest, models, follow_up=follow_up
    )
    assert continued["passed"] and len(continued["transfers"]) == 1
    saved_messages = continued["owner_messages"]
    retried = await execute_model_task(
        app, test_database_url, manifest, models, follow_up=follow_up
    )
    assert retried["owner_messages"] == saved_messages
    assert len(retried["recipient_mail"]) == 3


async def test_model_task_runner_does_not_hide_provider_failure(app, pool, test_database_url):
    manifest, _ = await prepare_task(pool)
    models = {
        "alice": ScriptedModel(responses=[RuntimeError("private-provider-diagnostic")]),
        "bob": ScriptedModel(responses=[AIMessage(content="暂未确认入账。")]),
    }
    report = await execute_model_task(app, test_database_url, manifest, models)
    assert not report["passed"]
    assert report["errors"][0]["owner_id"] == "alice"
    assert "private-provider-diagnostic" not in json.dumps(report, default=str)
    assert report["final_balances"] == {"alice": 100, "bob": 20}
    assert report["transfers"] == []
