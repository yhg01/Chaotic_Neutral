"""Run the local browser-shopping pipeline with saved credentials and checkpoints."""

import argparse
import asyncio
import hashlib
import json
import math
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.director import Director
from agentrpg.migrate import migrate
from agentrpg.shop import ShopService, plain
from agentrpg.shop_demo import shopping_model_scenario, shopping_scenario
from agentrpg.shop_model_roles import (
    QUIET_BUYER_INSTRUCTIONS,
    SELLER_INSTRUCTIONS,
    SHOP_ACTOR_PROMPT,
    SHOP_ACTOR_REVIEW_PROMPT,
)
from agentrpg.shop_runtime import INSTRUCTIONS, run_shop
from agentrpg.supervision import RuntimeSupervisor, SupervisionPolicy

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    if not math.isfinite(args.step_interval_seconds) or not 0 <= args.step_interval_seconds <= 3600:
        raise SystemExit("Use a window interval from zero through 3600 seconds.")
    settings = Settings()
    database_url = settings.database_url.get_secret_value()
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    manifest = json.loads(args.resume.read_text()) if args.resume else None
    if manifest and args.supervise and not manifest["configuration"].get("supervision"):
        raise SystemExit("A historical run cannot enable a different recovery policy on resume.")
    mode = manifest["configuration"]["mode"] if manifest else args.mode
    uses_models = mode in {"model", "model-actors"}
    api_key = env.get("OPENROUTER_API_KEY") if uses_models else None
    if uses_models and (not api_key or not args.campaign):
        raise SystemExit(
            "Model runs need local OPENROUTER_API_KEY and the existing --campaign UUID."
        )
    await migrate(database_url)
    async with AsyncConnectionPool(
        database_url, min_size=1, max_size=8, kwargs={"row_factory": dict_row}
    ) as pool:
        director = Director(pool)
        binding, ledger = None, CampaignLedger(pool)
        if uses_models:
            # Never create a campaign or change its ceiling in this runner.
            budget = await ledger.summary(args.campaign)
            if Decimal(str(budget["remaining_usd"])) <= 0:
                raise SystemExit("The existing campaign has no available budget.")
            actor_model = (
                manifest["configuration"].get("actor_model", "openai/gpt-4.1")
                if manifest
                else "openai/gpt-4.1"
            )
            models = {"openai/gpt-4.1-mini"}
            if mode == "model-actors":
                models.add(actor_model)
            prices = await fetch_model_prices(models)
            binding = BudgetBinding(ledger, args.campaign, prices)
        if manifest is None:
            scenario = shopping_model_scenario if mode == "model-actors" else shopping_scenario
            manifest = await director.create_run(scenario(args.rounds))
            manifest["configuration"] = {
                "mode": mode,
                "rounds": args.rounds,
                "instructions": INSTRUCTIONS,
                "runtime_revision": "local-browser-shop-v1",
                "require_explicit_finish": True,
                "include_previous_activation_context": True,
                "browser_shop_owners": ["alice"],
                "campaign_id": str(args.campaign) if uses_models else None,
                "step_interval_seconds": args.step_interval_seconds,
            }
            if args.supervise:
                manifest["configuration"]["supervision"] = SupervisionPolicy(
                    base_delay_seconds=args.retry_delay_seconds
                ).model_dump()
            if mode == "model-actors":
                manifest["configuration"].update(
                    model_roles="all",
                    model="openai/gpt-4.1-mini",
                    actor_model="openai/gpt-4.1",
                    actor_prompt=SHOP_ACTOR_PROMPT,
                    actor_review_prompt=SHOP_ACTOR_REVIEW_PROMPT,
                    instructions_by_owner={
                        "alice": INSTRUCTIONS + QUIET_BUYER_INSTRUCTIONS,
                        "bob": SELLER_INSTRUCTIONS,
                    },
                    require_shopping_task_coverage_review=True,
                    preserve_shopping_consent=True,
                    independent_shopping_stimulus_review=True,
                    shopping_initial_setup_only=True,
                    require_initial_shopping_consent=True,
                    stimulus_model="openai/gpt-4.1",
                    runtime_revision="typed-setup-model-browser-shop-v11",
                )
            await ShopService(pool).enable(UUID(manifest["run_id"]))
        elif uses_models and str(args.campaign) != manifest["configuration"]["campaign_id"]:
            raise SystemExit("Resume must use the original campaign.")
        if policy := manifest["configuration"].get("supervision"):
            await RuntimeSupervisor(pool).bind(
                UUID(manifest["run_id"]), SupervisionPolicy.model_validate(policy)
            )
        folder = ROOT / ".local/browser-shop-runs" / manifest["run_id"]
        save_private(folder / "credentials.json", manifest)
        run = await director.get_run(UUID(manifest["run_id"]))
        retry_wait = (
            run["status"] == "paused"
            and policy
            and (run["result"] or {}).get("reason") == "worker_failure"
        )
        if run["status"] in {"pending", "paused"} and not retry_wait:
            await director.control(
                UUID(manifest["run_id"]), "start" if run["status"] == "pending" else "resume"
            )
        print(f"Run: {manifest['run_id']}\nMode: {mode}", flush=True)
        report = await run_shop(
            pool,
            database_url,
            args.origin,
            manifest,
            api_key=api_key,
            binding=binding,
            restart_every=args.restart_every,
            inject=args.inject_checkout_interruption,
            stop_after_windows=args.stop_after_windows,
            progress=lambda r: print(f"Window {r['current_window']}: {r['status']}", flush=True),
        )
        if uses_models:
            report["campaign_budget"] = await ledger.summary(args.campaign)
        # Preserve each invocation, including partial and failed reports, before
        # updating the latest report. Resuming never erases earlier evidence.
        latest = folder / "report.json"
        if latest.exists():
            digest = hashlib.sha256(latest.read_bytes()).hexdigest()[:16]
            save_private(folder / f"invocation-{digest}.json", json.loads(latest.read_text()))
        report["invocation_id"] = str(uuid4())
        report["process_id"] = os.getpid()
        save_private(folder / f"invocation-{report['invocation_id']}.json", report)
        save_private(folder / "report.json", report)
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO run_reports(run_id,report) VALUES(%s,%s) "
                "ON CONFLICT(run_id) DO UPDATE SET report=EXCLUDED.report",
                (manifest["run_id"], Jsonb(plain(report))),
            )
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "run_id",
                        "mode",
                        "run_status",
                        "rounds",
                        "checks",
                        "passed",
                        "elapsed_seconds",
                        "runtime_recreations",
                        "committed_checkout_recoveries",
                        "model_usage",
                    )
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"Report: {folder / 'report.json'}")
        if not report["passed"] and report.get("stop_reason") != "invocation_window_limit":
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=["control", "model", "model-actors"], default="control")
    parser.add_argument("--rounds", type=int, choices=range(1, 31), default=3)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--campaign", type=UUID)
    parser.add_argument("--restart-every", type=int, choices=range(0, 101), default=0)
    parser.add_argument("--inject-checkout-interruption", action="store_true")
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="Freeze safe retries and progress checks before execution.",
    )
    parser.add_argument("--retry-delay-seconds", type=float, default=1.0)
    parser.add_argument("--step-interval-seconds", type=float, default=0)
    parser.add_argument("--stop-after-windows", type=int, choices=range(0, 101), default=0)
    asyncio.run(main(parser.parse_args()))
