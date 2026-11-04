"""Run or resume the first transfer task with real models and saved evidence."""

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values

from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.main import create_app
from agentrpg.migrate import migrate
from agentrpg.transfer_demo import prepare_task
from agentrpg.transfer_model_task import execute_model_task

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    api_key = env.get("OPENROUTER_API_KEY")
    if not api_key or not api_key.strip():
        raise SystemExit("Set OPENROUTER_API_KEY in the local .env before running models.")
    model_name = env.get("OPENROUTER_MODEL") or "openai/gpt-4.1-mini"
    manifest = json.loads(args.resume.read_text()) if args.resume else None
    if manifest:
        model_name = manifest["configuration"]["model"]
    prices = await fetch_model_prices({model_name})
    settings = Settings()
    database_url = settings.database_url.get_secret_value()
    await migrate(database_url)
    app = create_app(settings)
    usage = UsageTracker()
    async with app.router.lifespan_context(app):
        if manifest is None:
            manifest, _ = await prepare_task(app.state.pool)
            manifest["configuration"] = {
                "model": model_name,
                "campaign_id": str(uuid4()),
                "budget_usd": "1",
            }
        config = manifest["configuration"]
        folder = ROOT / ".local/transfer-tasks" / manifest["run_id"]
        credentials = folder / "credentials.json"
        save_private(credentials, manifest)
        print(f"Run: {manifest['run_id']}\nResume file: {credentials}", flush=True)
        ledger = CampaignLedger(app.state.pool)
        campaign_id = UUID(config["campaign_id"])
        await ledger.ensure_campaign(
            campaign_id, Decimal(config["budget_usd"]), name="First simulated transfer task"
        )
        binding = BudgetBinding(ledger, campaign_id, prices)
        models = {
            owner: create_model(api_key, model_name, budget=binding, max_retries=1)
            for owner in ("alice", "bob")
        }
        try:
            follow_up = None
            if args.complete_notification:
                follow_up = {
                    "alice": "请继续完成原任务中“转账成功后告诉他”这一项，并把处理结果向我汇报。"
                    "先检查已有记录；不要重复转账。",
                    "bob": "请继续检查账户和新收到的消息，向我报告实际入账和通知情况。",
                }
            report = await execute_model_task(
                app, database_url, manifest, models, callbacks=[usage], follow_up=follow_up
            )
        finally:
            for model in models.values():
                await model.aclose()
        report["usage"] = usage.summary()
        report["operator_follow_up"] = follow_up
        report["campaign_budget"] = await ledger.summary(campaign_id)
        save_private(folder / "report.json", report)
        save_private(folder / f"report.{uuid4()}.json", report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "mode",
                    "run_id",
                    "final_balances",
                    "checks",
                    "passed",
                    "errors",
                    "usage",
                )
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    print(f"Report: {folder / 'report.json'}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path, help="Saved credentials.json from this task.")
    parser.add_argument(
        "--complete-notification",
        action="store_true",
        help="Send a saved owner reminder to finish notification without paying again.",
    )
    args = parser.parse_args()
    if args.complete_notification and not args.resume:
        parser.error("--complete-notification requires --resume.")
    asyncio.run(main(args))
