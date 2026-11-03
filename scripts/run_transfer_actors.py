"""Run model Actors and personal assistants in the lunch repayment scenario."""

import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values
from psycopg.types.json import Jsonb

from agentrpg.actor import ACTOR_PROMPT, ACTOR_REVIEW_PROMPT, ModelActor
from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.agent_tools import BoardTools
from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.main import create_app
from agentrpg.migrate import migrate
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.transfer_model_task import INSTRUCTIONS
from agentrpg.transfer_tasks import TransferTaskService
from agentrpg.transfer_validation import CASES, validation_report
from agentrpg.trial_audit import begin_trial, end_trial

ROOT = Path(__file__).resolve().parents[1]
ASSISTANT_INSTRUCTIONS = INSTRUCTIONS + (
    " Account balances and payment receipts live in inspect_transfer_account. "
    "Incoming and sent messages live in list_mail/read_mail. Scratch files and their search "
    "tools do not contain account or inbox records. Use the authoritative source when checking "
    "a payment or message; a scratch search cannot establish that there are no records. "
    "If the owner asks you to check account records, call inspect_transfer_account before "
    "reporting whether a payment exists, even when the inbox is empty or the owner reports "
    "receipt. A peer's payment notice alone is not account verification. Describe payment "
    "direction using the receipt sender and recipient: the sender repays money previously "
    "advanced by the recipient. Do not describe the recipient as owing that debt. "
    "Read message bodies before acknowledging them. When the requested work is complete, "
    "use finish_activation with status finished only if no requested work remains unresolved. "
    "A courtesy acknowledgement does not resolve an unknown amount or an outstanding reply. "
    "Preserve that task's explicit wait with empty text. If no work remains and the courtesy "
    "acknowledgement needs no useful reply, finish with empty text."
    " If waiting for an unknown amount or another person's reply, include one brief deferral "
    "report in finish_activation.text or send_owner_message for the new owner request. "
    "Use status='waiting', wait_for=['inbox_received']. After the owner knows the deferral, "
    "subsequent waiting activations use empty text. "
    "For a question to your own owner use wait_for=['owner_message']. Do not send repeated "
    "waiting acknowledgements. Reply in your owner's language. Owner-chat message IDs must "
    "never be passed to acknowledge_mail; only received Inbox email IDs can be acknowledged."
    " Prefer wait_for_mail when blocked on another person's email; prefer wait_for_owner "
    "after asking your own paired owner a question. These tools declare the correct event."
    " When the owner already asks you to contact a named peer or confirm information with "
    "that peer, send the permitted internal inquiry rather than asking permission to ask. "
    "Use read_run_view for the directory and authoritative account/mail tools for evidence. "
    "Do not ask again for records that the owner already said are unavailable unless new "
    "evidence appears. If both people lack an amount record, explain that payment must remain "
    "deferred. Withholding an unverified or unauthorized payment does not need another approval. "
    "If the owner already decides to defer and asks whether that is reasonable, answer briefly "
    "without asking them to confirm the same decision again. Avoid generic invitations that "
    "restart a settled deferral. Use wait_for_mail with empty text once the owner is informed; "
    "the missing amount is still unresolved, so do not mark that work finished. Use "
    "wait_for_owner with the question when a real decision from your own owner is still needed. "
    "A completed payment repays the recipient's advance: for example, "
    "say '归还 Bob 替您垫付的午饭钱', "
    "never '归还您借给 Bob 的钱'. Receipt direction is authoritative."
)
TASK_INSTRUCTIONS = (
    " This run tracks persistent owner transfer tasks separately from activation results. "
    "Read transfer_task_context observations or list_transfer_tasks. Ending an activation "
    "does not finish a business task. Use defer_transfer_task for the exact task while blocked. "
    "Peer mail only provides candidate information: inspect the owner's exact authorization "
    "before payment. If permission is missing, ask the owner and defer on owner_message. "
    "After the matching payment, send the recipient notice and an owner report using the "
    "message tools. Complete the task with complete_transfer_task and their real IDs. "
    "Here finished ends only the current activation; business task completion is separate. "
    "A cancelled task must not be restarted or paid."
)


async def main(args):
    if args.guard_transfer_task_yield and not args.track_transfer_tasks:
        raise SystemExit("--guard-transfer-task-yield requires --track-transfer-tasks.")
    if args.review_transfer_task_intent and not args.track_transfer_tasks:
        raise SystemExit("--review-transfer-task-intent requires --track-transfer-tasks.")
    if args.clarify_transfer_authority and not args.track_transfer_tasks:
        raise SystemExit("--clarify-transfer-authority requires --track-transfer-tasks.")
    if args.review_transfer_authority and not args.track_transfer_tasks:
        raise SystemExit("--review-transfer-authority requires --track-transfer-tasks.")
    if args.review_transfer_task_coverage and not args.track_transfer_tasks:
        raise SystemExit("--review-transfer-task-coverage requires --track-transfer-tasks.")
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    if not env.get("OPENROUTER_API_KEY"):
        raise SystemExit("Set OPENROUTER_API_KEY in the ignored .env file.")
    config = {
        "model": env.get("OPENROUTER_MODEL") or "openai/gpt-4.1-mini",
        "actor_model": env.get("OPENROUTER_ACTOR_MODEL") or "openai/gpt-4.1",
        "campaign_id": str(uuid4()),
        "budget_usd": str(args.budget_usd),
        "instructions": ASSISTANT_INSTRUCTIONS,
        "validation_case": args.case,
        "runtime_revision": "explicit-prior-activation-context-v6",
        "require_explicit_finish": True,
        "guard_empty_scratch": True,
        "include_previous_activation_context": True,
        "actor_prompt": ACTOR_PROMPT,
        "actor_review_prompt": ACTOR_REVIEW_PROMPT,
        "require_mail_intent_review": True,
        "track_transfer_tasks": args.track_transfer_tasks,
        "guard_transfer_task_yield": args.guard_transfer_task_yield,
        "require_transfer_task_intent_review": args.review_transfer_task_intent,
        "clarify_transfer_authority": args.clarify_transfer_authority,
        "require_transfer_authority_review": args.review_transfer_authority,
        "require_transfer_task_coverage_review": args.review_transfer_task_coverage,
    }
    if args.track_transfer_tasks:
        config["instructions"] += TASK_INSTRUCTIONS
        config["runtime_revision"] = "persistent-transfer-tasks-v7"
        config["actor_prompt"] += (
            " When task_tracking_enabled is true, open the owner's repayment task once with "
            "a stable task_key in transfer_task_requests. Do not open a task just to monitor "
            "someone else's payment. Preserve it on acknowledgements. If choosing to cancel "
            "your own unpaid task, express that in dialogue and request a typed cancellation. "
            "Link any exact transfer_consent to its live task_key."
        )
    if args.guard_transfer_task_yield:
        config["runtime_revision"] = "persistent-transfer-tasks-v8"
    if args.review_transfer_task_intent:
        config["runtime_revision"] = "reviewed-outgoing-transfer-tasks-v9"
        config["actor_prompt"] += (
            " A transfer_task_request opens only a payment FROM this owner's account TO "
            "recipient_owner_id. The owner can ask to clarify the amount before paying. "
            "Watching an incoming payment owed to this owner is monitoring, not an outgoing "
            "payment task: preserve that request in dialogue and leave transfer_task_requests "
            "empty. Do not turn the recipient's monitoring request into a reverse payment."
        )
        config["actor_review_prompt"] += (
            " When transfer_task_intents is required, classify every opened task by its "
            "actual task_key. Check the owner's request, dialogue and known payment direction. "
            "Only outgoing_owner_payment can open a payment task. An incoming_payment_monitor "
            "or unsupported task must be removed without dropping valid owner dialogue. "
            "A missing amount or authority does not by itself invalidate an outgoing task."
        )
    if args.clarify_transfer_authority:
        config["runtime_revision"] = "explicit-transfer-authority-handoff-v10"
        config["instructions"] += (
            " Distinguish missing payment information from missing owner authority. Inspect "
            "the actual account. If the owner has already specified recipient, amount and "
            "memo but no matching authorization exists, ask YOUR OWN OWNER whether they "
            "authorize those exact terms, and defer the task on owner_message. Do not ask "
            "the peer to reconfirm already known terms or wait for peer mail to grant "
            "authority. Only seek missing information from a peer when it is actually "
            "unknown. If a valid exact authorization already exists, use it without asking "
            "for the same approval again. A payment-monitoring request does not authorize "
            "an outgoing payment. Read and answer substantive received inquiries when your "
            "owner has supplied the answer; an acknowledgement to your owner does not "
            "deliver a reply to the peer."
        )
        config["actor_prompt"] += (
            " Keep payment dialogue and typed authority consistent. If you choose to "
            "instruct your assistant to make an exact payment, explicitly express your "
            "approval and include the matching transfer_consent with recipient, amount, "
            "memo and live task_key. If your assistant asks you for those exact terms and "
            "they match the payment you still want, make an owner decision and supply the "
            "matching typed consent. Do not repeat an existing grant. When the amount is "
            "unknown, do not guess or approve it; choose a clarification or deferral. "
            "Monitoring incoming funds never needs outgoing payment consent."
        )
        config["actor_review_prompt"] += (
            " Check consistency between the current payment decision and typed consent. "
            "An explicit new instruction or approval to pay exact known terms must include "
            "the matching transfer_consent and live task_key, unless that grant already "
            "exists. Do not require a payment decision when the owner chooses to clarify, "
            "defer or cancel, or when the amount is unverified. Preserve valid owner's "
            "decisions and do not infer a new grant from a courtesy acknowledgement."
        )
    if args.review_transfer_authority:
        config["runtime_revision"] = "reviewed-transfer-authority-v11"
        config["actor_prompt"] += (
            " Never open an existing task_key again. The saved view already contains it; "
            "use transfer_task_requests=[] on routine follow-ups and reuse that key only "
            "when supplying consent or a genuine cancellation."
        )
        config["actor_review_prompt"] += (
            " transfer_authority is a required independent classification of the current "
            "owner decision. A new exact payment instruction needs matching typed consent. "
            "An unverified amount, incoming monitoring, courtesy or old grant is not a new "
            "approval. Classify before comparing with the candidate's consent fields."
        )
    if args.review_transfer_task_coverage:
        config["runtime_revision"] = "reviewed-transfer-task-coverage-v12"
        config["actor_prompt"] += (
            " Preserve unresolved repayment work even when the amount is unknown: open "
            "its outgoing business task once, with an Inbox or owner-message wait. "
            "Clarifying an amount is preparation for repayment, not cancellation or task "
            "completion. Opening that task does not grant transfer_consent. Keep the "
            "repayment goal and its constraints in the owner's dialogue."
        )
        config["actor_review_prompt"] += (
            " outgoing_task_coverage must independently cover the full owner's unresolved "
            "repayment request, including payment deferred for missing amount or authority. "
            "Do not reduce repay-after-clarification to a taskless one-off question. Require "
            "a matching existing or newly opened task, while preserving no consent for "
            "unverified terms. Incoming monitoring and resolved/cancelled work adds no task."
        )
    if args.campaign:
        config["campaign_id"] = str(args.campaign)
    manifest = json.loads(args.resume.read_text()) if args.resume else None
    if manifest:
        config = manifest["configuration"]
        config.setdefault("instructions", INSTRUCTIONS)
    prices = await fetch_model_prices({config["model"], config["actor_model"]})
    settings = Settings()
    database_url = settings.database_url.get_secret_value()
    await migrate(database_url)
    app = create_app(settings)
    usage = UsageTracker()
    async with app.router.lifespan_context(app), AsyncExitStack() as stack:
        director = Director(app.state.pool)
        if manifest is None:
            suffix = "actors" if args.case == "baseline" else args.case
            scenario_path = args.scenario or ROOT / f"scenarios/friends_lunch_{suffix}.json"
            spec = ScenarioSpec.model_validate_json(scenario_path.read_text())
            manifest = await director.create_run(spec)
            manifest["configuration"] = config
        run_id = UUID(manifest["run_id"])
        if config.get("track_transfer_tasks", False):
            await TransferTaskService(app.state.pool).enable(run_id)
        folder = ROOT / ".local/transfer-actor-runs" / str(run_id)
        save_private(folder / "credentials.json", manifest)
        print(f"Run: {run_id}\nResume file: {folder / 'credentials.json'}", flush=True)
        ledger = CampaignLedger(app.state.pool)
        campaign_id = UUID(config["campaign_id"])
        await ledger.ensure_campaign(
            campaign_id, Decimal(config["budget_usd"]), name="Actor–Agent lunch repayment"
        )
        binding = BudgetBinding(ledger, campaign_id, prices)
        runtime = await stack.enter_async_context(AgentRuntime(database_url))
        agents, actors = {}, {}
        for member in manifest["participants"]:
            owner = member["owner_id"]
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://actors/api/v1/",
                    headers={"Authorization": "Bearer " + member["token"]},
                )
            )
            model = create_model(
                env["OPENROUTER_API_KEY"],
                config["model"],
                budget=binding,
                budget_role="assistant",
                max_tokens=1500,
            )
            stack.push_async_callback(model.aclose)
            agents[owner] = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(
                    client, run_id, track_transfer_tasks=config.get("track_transfer_tasks", False)
                ),
                agent_id=member["agent_id"],
                session_id=member["session_id"],
                instructions=config["instructions"],
                max_model_calls=16,
                timeout_seconds=240,
                callbacks=[usage],
                require_explicit_finish=config.get("require_explicit_finish", False),
                guard_empty_scratch=config.get("guard_empty_scratch", False),
                include_previous_activation_context=config.get(
                    "include_previous_activation_context", False
                ),
                guard_transfer_task_yield=config.get("guard_transfer_task_yield", False),
            )
            await agents[owner]._verify_identity()
            actor_model = create_model(
                env["OPENROUTER_API_KEY"],
                config["actor_model"],
                budget=binding,
                budget_role="actor",
                max_tokens=1500,
            )
            reviewer = create_model(
                env["OPENROUTER_API_KEY"],
                config["actor_model"],
                budget=binding,
                budget_role="actor-review",
                max_tokens=1000,
            )
            stack.push_async_callback(actor_model.aclose)
            stack.push_async_callback(reviewer.aclose)
            actors[owner] = ModelActor(
                actor_model,
                reviewer=reviewer,
                max_repairs=1,
                prompt=config.get("actor_prompt", ACTOR_PROMPT),
                review_prompt=config.get("actor_review_prompt", ACTOR_REVIEW_PROMPT),
                require_mail_intent_review=config.get("require_mail_intent_review", False),
                require_transfer_task_intent_review=config.get(
                    "require_transfer_task_intent_review", False
                ),
                require_transfer_authority_review=config.get(
                    "require_transfer_authority_review", False
                ),
                require_transfer_task_coverage_review=config.get(
                    "require_transfer_task_coverage_review", False
                ),
            )
        run = await director.get_run(run_id)
        if run["status"] in {"pending", "paused"}:
            await director.control(run_id, "start" if run["status"] == "pending" else "resume")
        engine = RunEngine(app.state.pool, actors=actors, agents=agents)
        invocation = await begin_trial(
            app.state.pool, run_id, profile="transfer", configuration=config, root=ROOT
        )
        returned_report = False
        try:
            while (await director.get_run(run_id))["status"] == "running":
                run = await engine.advance(run_id)
                print(f"Window {run['current_window']}: {run['status']}", flush=True)
            returned_report = True
        finally:
            await end_trial(
                app.state.pool, run_id, invocation, root=ROOT, returned_report=returned_report
            )
        report = await validation_report(
            app.state.pool, run_id, config.get("validation_case", "baseline")
        )
        report["assistant_usage"] = usage.summary()
        report["actor_usage"] = {owner: actor.usage.summary() for owner, actor in actors.items()}
        report["campaign_budget"] = await ledger.summary(campaign_id)
        save_private(folder / "report.json", report)
        save_private(folder / f"report.{uuid4()}.json", report)
        async with app.state.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO run_reports(run_id,report) VALUES(%s,%s) "
                "ON CONFLICT(run_id) DO UPDATE SET report=EXCLUDED.report",
                (run_id, Jsonb(json.loads(json.dumps(report, default=str)))),
            )
        print(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "mode",
                        "run_status",
                        "final_balances",
                        "checks",
                        "passed",
                        "campaign_budget",
                    )
                    if key != "campaign_budget"
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        print(f"Report: {folder / 'report.json'}", flush=True)
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--guard-transfer-task-yield", action="store_true")
    parser.add_argument("--review-transfer-task-intent", action="store_true")
    parser.add_argument("--clarify-transfer-authority", action="store_true")
    parser.add_argument("--review-transfer-authority", action="store_true")
    parser.add_argument("--review-transfer-task-coverage", action="store_true")
    parser.add_argument(
        "--track-transfer-tasks",
        action="store_true",
        help="Opt in a new run to independent persistent transfer tasks.",
    )
    parser.add_argument("--case", choices=CASES, default="baseline")
    parser.add_argument(
        "--budget-usd",
        type=Decimal,
        default=Decimal("1"),
        help="Requested ceiling; must match the existing campaign. This does not raise its limit.",
    )
    parser.add_argument(
        "--campaign", type=UUID, help="Reuse the saved campaign and its persistent ceiling."
    )
    parser.add_argument(
        "--scenario", type=Path, help="Override the selected case's frozen scenario file."
    )
    asyncio.run(main(parser.parse_args()))
