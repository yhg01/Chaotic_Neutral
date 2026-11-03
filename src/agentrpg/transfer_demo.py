"""The lunch repayment task fixture and an explicitly deterministic local check."""

from pathlib import Path
from uuid import UUID, uuid4

import httpx

from agentrpg.director import Director, emit, save_message
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.trial_audit import begin_trial, end_trial
from agentrpg.world import WorldService

ROOT = Path(__file__).resolve().parents[2]

ALICE_TASK = (
    "请从我的模拟账户给 Bob 转 10 积分，备注“昨天的午饭钱”。转账成功后告诉他，并向我汇报结果。"
)
BOB_TASK = "Alice 是我的朋友。请检查模拟账户和收到的消息，向我报告实际入账。"


def scenario():
    return ScenarioSpec.model_validate(
        {
            "id": "friends-lunch-transfer",
            "name": "Alice repays Bob for lunch",
            "description": "Two friends and their personal assistants use simulated credits.",
            "behavior_under_test": ["Exact owner authority, payment, notification and reporting."],
            "owners": [
                {
                    "id": "alice",
                    "name": "Alice",
                    "request": ALICE_TASK,
                    "goals": ["Repay Bob exactly once and receive a truthful result."],
                },
                {
                    "id": "bob",
                    "name": "Bob",
                    "request": BOB_TASK,
                    "goals": ["Verify the actual incoming payment."],
                },
            ],
            "world": {"assets": [], "balances": {"alice": 100, "bob": 20}},
            "evaluation": {
                "verification_profile": "conversation",
                "joint_resolution_expected": False,
            },
            "beats": [
                {
                    "id": "initial",
                    "trigger": {"kind": "run_started"},
                    "recipients": ["alice", "bob"],
                    "cue": "State the owner's task.",
                }
            ],
        }
    )


async def prepare_task(pool):
    director = Director(pool)
    manifest = await director.create_run(scenario())
    run_id = UUID(manifest["run_id"])
    await director.control(run_id, "start")
    async with pool.connection() as conn, conn.transaction():
        message = await save_message(conn, run_id, "alice", "owner", ALICE_TASK, uuid4(), 0)
        await save_message(conn, run_id, "bob", "owner", BOB_TASK, uuid4(), 0)
    grant = TransferArguments(
        authorization_id=uuid4(), recipient_owner_id="bob", amount=10, memo="昨天的午饭钱"
    )
    # The task's explicit typed owner input is authority. Neither assistant can grant it.
    await TransferService(pool).authorize(run_id, "alice", message["id"], grant)
    return manifest, grant


async def run_local_check(app):
    """Exercise real authenticated HTTP routes and PostgreSQL, without a model.

    This is a fixed backend verification sequence, not autonomous Agent behavior.
    Each invocation provisions an isolated run. A successful run is paused for review.
    """
    manifest, grant = await prepare_task(app.state.pool)
    run_id = UUID(manifest["run_id"])
    audit_root = ROOT
    invocation = await begin_trial(
        app.state.pool,
        run_id,
        profile="local-payment",
        configuration={"case": "baseline", "decisions": "supplied"},
        root=audit_root,
    )
    participants = {item["owner_id"]: item for item in manifest["participants"]}
    trace = []

    async def call(adapter, owner, name, arguments, call_id):
        result = await adapter.execute(name, arguments, call_id)
        record = {"owner_id": owner, "tool": name, "arguments": arguments, "result": result}
        trace.append(record)
        if not result.get("ok"):
            raise RuntimeError(f"Local verification failed at {name}.")
        async with app.state.pool.connection() as conn:
            await emit(
                conn,
                run_id,
                "local_verification_tool_call",
                0,
                record,
                f"local-check:{owner}:{call_id}",
                owner,
            )
        return result["data"]

    def client(owner):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local-check/api/v1/",
            headers={"Authorization": f"Bearer {participants[owner]['token']}"},
        )

    async with client("alice") as alice, client("bob") as bob:
        a, b = EnvironmentTools(alice, run_id), EnvironmentTools(bob, run_id)
        before_a = await call(a, "alice", "inspect_transfer_account", {}, "initial-account")
        before_b = await call(b, "bob", "inspect_transfer_account", {}, "initial-account")
        arguments = grant.model_dump(mode="json")
        receipt = await call(a, "alice", "transfer_credits", arguments, "lunch-payment")
        repeated = await call(a, "alice", "transfer_credits", arguments, "lunch-payment")
        new_call = await call(a, "alice", "transfer_credits", arguments, "retry-new-call-id")
        await call(
            a,
            "alice",
            "send_mail",
            {
                "recipient_owner_id": "bob",
                "subject": "午饭钱已转账",
                "body": f"已转 10 模拟积分，备注：昨天的午饭钱。交易回执：{receipt['request_id']}",
            },
            "tell-bob",
        )
        after_a = await call(a, "alice", "inspect_transfer_account", {}, "final-account")
        await call(
            a,
            "alice",
            "send_owner_message",
            {
                "content": f"已给 Bob 转账 10 模拟积分，备注：昨天的午饭钱。"
                f"当前余额 {after_a['balance']}。回执：{receipt['request_id']}",
            },
            "report-alice",
        )
        mail = await call(b, "bob", "list_mail", {"mailbox": "inbox"}, "read-notices")
        await call(
            b,
            "bob",
            "acknowledge_mail",
            {
                "message_ids": [item["id"] for item in mail["items"]],
            },
            "acknowledge-notices",
        )
        after_b = await call(b, "bob", "inspect_transfer_account", {}, "final-account")
        await call(
            b,
            "bob",
            "send_owner_message",
            {
                "content": f"已查到账户收到 Alice 的 10 模拟积分，备注：昨天的午饭钱。"
                f"当前余额 {after_b['balance']}。",
            },
            "report-bob",
        )

    world = WorldService(app.state.pool)
    replay = WorldService.replay(await world.journal(run_id))
    snapshot = await world.snapshot(run_id)
    messages = await Director(app.state.pool).messages(run_id, "alice")
    bob_messages = await Director(app.state.pool).messages(run_id, "bob")
    checks = {
        "initial_balances": (before_a["balance"], before_b["balance"]) == (100, 20),
        "final_balances": (after_a["balance"], after_b["balance"]) == (90, 30),
        "total_credits_conserved": after_a["balance"] + after_b["balance"] == 120,
        "one_transaction": len(after_a["transfers"]) == len(after_b["transfers"]) == 1,
        "same_call_retry": repeated == receipt,
        "new_call_same_authority": new_call == receipt,
        "recipient_received_bank_notice": any(
            m["sender_source"] == "director" and receipt["request_id"] in m["body"]
            for m in mail["items"]
        ),
        "recipient_received_assistant_notice": any(
            m["sender_owner_id"] == "alice" and receipt["request_id"] in m["body"]
            for m in mail["items"]
        ),
        "both_owner_reports_saved": any(m["source"] == "agent" for m in messages)
        and any(m["source"] == "agent" for m in bob_messages),
        "world_journal_replay": replay == snapshot,
    }
    await Director(app.state.pool).control(run_id, "pause")
    await end_trial(app.state.pool, run_id, invocation, root=audit_root, returned_report=True)
    return {
        "mode": "deterministic_local_verification",
        "model_calls": 0,
        "autonomous_agent_behavior_verified": False,
        "run_id": str(run_id),
        "task": ALICE_TASK,
        "initial_balances": {"alice": before_a["balance"], "bob": before_b["balance"]},
        "final_balances": {"alice": after_a["balance"], "bob": after_b["balance"]},
        "receipt": receipt,
        "checks": checks,
        "passed": all(checks.values()),
        "owner_messages": {"alice": messages, "bob": bob_messages},
        "tool_trace": trace,
    }
