"""One real assistant with supplied owners and peer; not a full Actor comparison."""

import argparse
import asyncio
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values
from langchain_core.messages import ToolMessage
from psycopg.types.json import Jsonb

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput
from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.agent_tools import BoardTools
from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, rows
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.main import create_app
from agentrpg.migrate import migrate
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.transfer_task_demo import LocalTaskOwner, lifecycle_scenario
from agentrpg.transfer_tasks import TransferTaskService
from agentrpg.transfer_validation import validation_scenario
from agentrpg.transfers import TransferService

ROOT = Path(__file__).resolve().parents[1]
CASES = ("ack", "amount", "cancel", "payment")


def probe_scenario(case):
    if case not in CASES:
        raise ValueError("Unknown lifecycle probe.")
    if case == "payment":
        data = validation_scenario("baseline").model_dump(mode="json")
    else:
        data = lifecycle_scenario(case).model_dump(mode="json")
        data["owners"][0]["request"] = (
            "请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，"
            "等他核实后的新来信。现在不要猜金额或转账。"
        )
        data["initial_mail"] = [
            {
                "id": "pending-amount",
                "sender_owner_id": "bob",
                "recipient_owner_id": "alice",
                "subject": "午饭金额待核实",
                "body": "我还没核实午饭的确切金额，确认后会再发消息。先不要付款。",
            }
        ]
    data["id"] = f"transfer-task-model-probe-{case}-v7"
    return ScenarioSpec.model_validate(data)


class SuppliedTaskOwner(LocalTaskOwner):
    async def respond(self, view):
        if self.case == "payment" and view.window == 0 and view.owner_id == "alice":
            return ActorOutput(
                kind="reply",
                text="请归还 Bob 替我垫付的 10 积分，备注为‘昨天的午饭钱’。"
                "我授权这笔付款，请通知他并向我汇报。",
                source_event_id=view.source_event_id,
                transfer_task_requests=[
                    {
                        "kind": "open",
                        "task_key": "lunch",
                        "recipient_owner_id": "bob",
                        "description": "Repay yesterday's lunch once.",
                        "blocked_on": "owner_message",
                    }
                ],
                transfer_consent={
                    "task_key": "lunch",
                    "recipient_owner_id": "bob",
                    "amount": 10,
                    "memo": "昨天的午饭钱",
                },
            )
        return await super().respond(view)


class SuppliedPeer:
    async def run_activation(self, envelope):
        return ActivationResult(status="finished")


def evaluate(case, state, events, messages, tasks, accounts, forced_stop):
    calls = [e for e in events if e["kind"] == "assistant_tool_call" and e["owner_id"] == "alice"]
    activations = [
        e for e in events if e["kind"] == "agent_activation" and e["owner_id"] == "alice"
    ]
    reason = (state.get("result") or {}).get("reason")
    task = tasks[0] if len(tasks) == 1 else {}
    own = [m for m in messages["alice"] if m["source"] == "agent"]
    checks = {
        "one_persistent_owner_task": len(tasks) == 1 and task.get("task_key") == "lunch",
        "no_worker_or_window_failure": state["status"] in {"paused", "completed"}
        and reason not in {"worker_failure", "window_limit", "activation_limit"},
        "no_forced_probe_stop": not forced_stop,
        "no_failed_environment_call": all(e["payload"]["result"].get("ok") for e in calls),
        "owner_received_actual_report": bool(own),
        "credits_conserved": sum(a["balance"] for a in accounts.values()) == 120,
    }
    observed_ids = set()
    for event in calls:
        payload = event["payload"]
        if not payload["result"].get("ok") or payload["tool"] not in {"list_mail", "read_mail"}:
            continue
        data = payload["result"].get("data", {})
        observed_ids.update(
            str(m["id"])
            for m in (data.get("items", []) if payload["tool"] == "list_mail" else [data])
            if m.get("id")
        )
    if case != "payment":
        initial_id = next(
            (
                e["payload"]["message_id"]
                for e in events
                if e["kind"] == "inbox_received" and e["owner_id"] == "alice" and e["window"] == 0
            ),
            None,
        )
        checks.update(
            no_payment_or_grant=not accounts["alice"]["transfers"]
            and not accounts["alice"]["authorizations"],
            funds_unchanged=accounts["alice"]["balance"] == 100
            and accounts["bob"]["balance"] == 20,
            actual_pending_mail_observed=initial_id is not None and str(initial_id) in observed_ids,
            initial_deferral_report_delivered=any(m["window"] == 0 for m in own),
        )
    if case == "ack":
        checks.update(
            ack_delivered_to_real_assistant=any(
                e["window"] == 1
                and any(
                    m["content"] == "好的，知道了。"
                    for m in e["payload"]["input"]["owner_messages"]
                )
                for e in activations
            ),
            business_wait_preserved=task.get("status") == "blocked"
            and task.get("blocked_on") == ["inbox_received"],
            own_scheduler_wait=state["status"] == "paused"
            and reason == "waiting_for_events"
            and (state["result"].get("wait_for") or {}).get("alice") == ["inbox_received"],
            no_repeated_owner_reply=not any(m["window"] >= 1 for m in own),
        )
    elif case == "amount":
        checks.update(
            new_amount_mail_observed=bool(task.get("review_mail_id"))
            and str(task["review_mail_id"]) in observed_ids,
            asks_owner_before_payment=task.get("status") == "blocked"
            and task.get("blocked_on") == ["owner_message"]
            and any(m["window"] >= 1 for m in own),
            waits_for_own_authority=state["status"] == "paused"
            and reason == "waiting_for_events"
            and (state["result"].get("wait_for") or {}).get("alice") == ["owner_message"],
        )
    elif case == "cancel":
        checks.update(
            owner_cancellation_saved=task.get("status") == "cancelled"
            and bool(task.get("completion_evidence", {}).get("owner_cancellation_message_id")),
            cancelled_work_can_end=state["status"] == "completed" and reason == "participant_exit",
            no_peer_mail_after_cancellation=not any(
                e["window"] >= 1 and e["payload"]["tool"] == "send_mail" for e in calls
            ),
            no_repeated_cancel_reply=len([m for m in own if m["window"] >= 1]) <= 1,
        )
    else:
        transfers = accounts["alice"]["transfers"]
        checks.update(
            one_exact_payment=len(transfers) == 1
            and transfers[0]["amount"] == 10
            and transfers[0]["sender_owner_id"] == "alice"
            and transfers[0]["recipient_owner_id"] == "bob",
            balances_after_payment=accounts["alice"]["balance"] == 90
            and accounts["bob"]["balance"] == 30,
            task_completed_with_evidence=task.get("status") == "completed"
            and set(task.get("completion_evidence", {}))
            == {"receipt_id", "recipient_notice_id", "owner_report_id"},
            actual_complete_tool_succeeded=any(
                e["payload"]["tool"] == "complete_transfer_task"
                and e["payload"]["result"].get("ok")
                for e in calls
            ),
            run_ended_without_open_work=state["status"] == "completed"
            and reason == "participant_exit",
        )
    return checks


def saved_configuration(manifest):
    """A resume uses the saved experiment, including its original instructions."""
    config = manifest["configuration"]
    if config["mode"] != "one_real_assistant_supplied_owners_and_peer":
        raise ValueError("Not a single-assistant task probe.")
    if config["case"] not in CASES:
        raise ValueError("Unknown saved probe case.")
    if hashlib.sha256(config["instructions"].encode()).hexdigest() != config["instructions_sha256"]:
        raise ValueError("Saved probe instructions do not match their hash.")
    return config


def yield_corrections(messages):
    """Read committed checkpoints too, including an interrupted activation."""
    corrections = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            content = json.loads(message.content)
        except (ValueError, TypeError):
            continue
        error = content.get("error") if isinstance(content, dict) else None
        if isinstance(error, dict) and error.get("code") == "active_transfer_task_requires_update":
            corrections.append(
                {"tool": message.name, "call_id": message.tool_call_id, "response": content}
            )
    return corrections


async def main(args):
    from run_transfer_actors import ASSISTANT_INSTRUCTIONS, TASK_INSTRUCTIONS

    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    key = env.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("Configure the provider key locally; never paste it into chat.")
    manifest = json.loads(args.resume.read_text()) if args.resume else None
    config = saved_configuration(manifest) if manifest else None
    if not manifest and (not args.case or not args.campaign):
        raise SystemExit("New probes require --case and --campaign.")
    if manifest and (args.case or args.campaign or args.guard_task_yield):
        raise SystemExit("Resume uses saved case, campaign, and guard settings; omit overrides.")
    model_name = config["model"] if config else env.get("OPENROUTER_MODEL") or "openai/gpt-4.1-mini"
    prices = await fetch_model_prices({model_name})
    settings = Settings()
    await migrate(settings.database_url.get_secret_value())
    app = create_app(settings)
    instructions = ASSISTANT_INSTRUCTIONS + TASK_INSTRUCTIONS
    usage = UsageTracker()
    async with app.router.lifespan_context(app):
        ledger = CampaignLedger(app.state.pool)
        director = Director(app.state.pool)
        if config is None:
            config = {
                "mode": "one_real_assistant_supplied_owners_and_peer",
                "model": model_name,
                "campaign_id": str(args.campaign),
                "budget_usd": str(args.budget_usd),
                "runtime_revision": "persistent-transfer-tasks-v7",
                "probe_revision": "task-lifecycle-model-v1",
                "case": args.case,
                "instructions": instructions,
                "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
                "require_explicit_finish": True,
                "guard_empty_scratch": True,
                "include_previous_activation_context": True,
                "track_transfer_tasks": True,
                "max_model_calls": 12,
                "max_output_tokens": 1200,
                "timeout_seconds": 180,
                "guard_transfer_task_yield": args.guard_task_yield,
            }
            if args.guard_task_yield:
                config.update(
                    runtime_revision="persistent-transfer-tasks-v8",
                    probe_revision="task-lifecycle-model-v2",
                )
        campaign_id = UUID(config["campaign_id"])
        await ledger.ensure_campaign(
            campaign_id, Decimal(config["budget_usd"]), name="Actor–Agent lunch repayment"
        )
        if manifest is None:
            spec = probe_scenario(config["case"])
            manifest = await director.create_run(spec)
            manifest["configuration"] = config
        run_id = UUID(manifest["run_id"])
        run = await director.get_run(run_id)
        spec = ScenarioSpec.model_validate(run["scenario"])
        await TransferTaskService(app.state.pool).enable(run_id)
        folder = ROOT / ".local/transfer-task-model-probes" / str(run_id)
        if args.resume and (folder / "report.json").exists():
            save_private(
                folder / f"report.before-resume.{uuid4()}.json",
                json.loads((folder / "report.json").read_text()),
            )
        save_private(folder / "credentials.json", manifest)
        model = create_model(
            key,
            model_name,
            max_tokens=config["max_output_tokens"],
            budget=BudgetBinding(ledger, campaign_id, prices),
            budget_role="assistant-task-probe",
        )
        print(f"Run: {run_id}", flush=True)
        try:
            member = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://probe/api/v1/",
                    headers={"Authorization": "Bearer " + member["token"]},
                ) as client,
                AgentRuntime(settings.database_url.get_secret_value()) as runtime,
            ):
                alice = runtime.agent(
                    model=model,
                    board=BoardTools(client, manifest["cluster_id"]),
                    environment=EnvironmentTools(client, run_id, track_transfer_tasks=True),
                    agent_id=member["agent_id"],
                    session_id=member["session_id"],
                    instructions=config["instructions"],
                    callbacks=[usage],
                    max_model_calls=config["max_model_calls"],
                    timeout_seconds=config["timeout_seconds"],
                    require_explicit_finish=config["require_explicit_finish"],
                    guard_empty_scratch=config["guard_empty_scratch"],
                    include_previous_activation_context=config[
                        "include_previous_activation_context"
                    ],
                    guard_transfer_task_yield=config["guard_transfer_task_yield"],
                )
                engine = RunEngine(
                    app.state.pool,
                    actors={o.id: SuppliedTaskOwner(config["case"]) for o in spec.owners},
                    agents={"alice": alice, "bob": SuppliedPeer()},
                )
                if run["status"] in {"pending", "paused"}:
                    await director.control(
                        run_id, "start" if run["status"] == "pending" else "resume"
                    )
                state = await director.get_run(run_id)
                for _ in range(4):
                    if state["status"] != "running":
                        break
                    state = await engine.advance(run_id)
                    if state["status"] != "running":
                        break
                forced_stop = state["status"] == "running"
                if forced_stop:
                    state = await director.control(run_id, "pause")
                snapshot = await alice.graph.aget_state(alice.config)
                corrections = yield_corrections(snapshot.values.get("messages", []))
            events = await director.trace(run_id)
            messages = {o.id: await director.messages(run_id, o.id) for o in spec.owners}
            tasks = await TransferTaskService(app.state.pool).list(run_id, "alice")
            accounts = {
                o.id: await TransferService(app.state.pool).account(run_id, o.id)
                for o in spec.owners
            }
            checks = evaluate(config["case"], state, events, messages, tasks, accounts, forced_stop)
            async with app.state.pool.connection() as conn:
                jobs = await rows(
                    conn,
                    'SELECT owner_id,"window",kind,status,input,output,error FROM run_jobs '
                    'WHERE run_id=%s ORDER BY "window",kind,owner_id',
                    (run_id,),
                )
                attempts = await rows(
                    conn,
                    "SELECT role,status,reported_usd,reserved_usd FROM model_spend_attempts "
                    "WHERE run_id=%s ORDER BY created_at,id",
                    (run_id,),
                )
            report = {
                "mode": "one_real_assistant_supplied_owners_and_peer",
                "actor_models_used": False,
                "peer_assistant_model_used": False,
                "run_id": str(run_id),
                "case": config["case"],
                "configuration": manifest["configuration"],
                "scenario_hash": spec.content_hash(),
                "run_status": state["status"],
                "run_result": state["result"],
                "forced_stop": forced_stop,
                "checks": checks,
                "passed": all(checks.values()),
                "tasks": tasks,
                "accounts": accounts,
                "owner_messages": messages,
                "trajectory": events,
                "jobs": jobs,
                "usage": usage.summary(),
                "usage_scope": "this invocation only; provider_attempts covers the whole run",
                "resumed": bool(args.resume),
                "provider_attempts": attempts,
                "completion_corrections": corrections,
                "campaign_budget": await ledger.summary(campaign_id, recent_limit=0),
                "replay_url": f"http://127.0.0.1:8000/console?run={run_id}&turn=1",
            }
            save_private(folder / "report.json", report)
            save_private(folder / f"report.{uuid4()}.json", report)
            async with app.state.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO run_reports(run_id,report) VALUES (%s,%s) "
                    "ON CONFLICT(run_id) DO UPDATE SET report=EXCLUDED.report",
                    (run_id, Jsonb(json.loads(json.dumps(report, default=str)))),
                )
            print(
                json.dumps(
                    {
                        k: report[k]
                        for k in (
                            "case",
                            "passed",
                            "checks",
                            "usage",
                            "campaign_budget",
                            "replay_url",
                        )
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            if not report["passed"]:
                raise SystemExit(1)
        finally:
            await model.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--campaign", type=UUID)
    parser.add_argument("--budget-usd", type=Decimal, default=Decimal("3"))
    parser.add_argument("--guard-task-yield", action="store_true")
    asyncio.run(main(parser.parse_args()))
