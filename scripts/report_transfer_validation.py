"""Re-evaluate saved runs without model calls and export a credential-free comparison."""

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from agentrpg.budget import CampaignLedger
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.main import create_app
from agentrpg.transfer_validation import CASES, validation_report

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    app = create_app(Settings())
    summaries = []
    async with app.router.lifespan_context(app):
        for entry in args.run:
            case, run = entry.split(":", 1)
            if case not in CASES:
                raise SystemExit("Unknown validation case.")
            run_id = UUID(run)
            folder = ROOT / ".local/transfer-actor-runs" / run
            original = json.loads((folder / "report.json").read_text())
            # Extract only public model settings. Never export the credential manifest.
            configuration = json.loads((folder / "credentials.json").read_text())["configuration"]
            report = await validation_report(app.state.pool, run_id, case)
            save_private(folder / f"validation.{uuid4()}.json", report)
            attempts = report["provider_attempts"]
            summaries.append(
                {
                    "run_id": run,
                    "case": case,
                    "run_status": report["run_status"],
                    "passed": report["passed"],
                    "original_passed": original["passed"],
                    "validation_revision": report["validation_revision"],
                    "checks": report["checks"],
                    "metrics": report["metrics"],
                    "initial_balances": report["initial_balances"],
                    "final_balances": report["final_balances"],
                    "scenario_hash": report["scenario_hash"],
                    "model": configuration["model"],
                    "actor_model": configuration["actor_model"],
                    "runtime_revision": configuration.get("runtime_revision"),
                    "guard_empty_scratch": configuration.get("guard_empty_scratch", False),
                    "include_previous_activation_context": configuration.get(
                        "include_previous_activation_context", False
                    ),
                    "actor_prompt_hash": hashlib.sha256(
                        configuration["actor_prompt"].encode()
                    ).hexdigest()
                    if configuration.get("actor_prompt")
                    else None,
                    "actor_review_prompt_hash": hashlib.sha256(
                        configuration["actor_review_prompt"].encode()
                    ).hexdigest()
                    if configuration.get("actor_review_prompt")
                    else None,
                    "assistant_instructions_hash": hashlib.sha256(
                        configuration["instructions"].encode()
                    ).hexdigest(),
                    "provider_attempts": len(attempts),
                    "attempts_by_role": dict(Counter(a["role"] for a in attempts)),
                    "reported_cost_usd": str(
                        sum((a["reported_usd"] or Decimal(0) for a in attempts), Decimal(0))
                    ),
                    "unconfirmed_attempts": sum(a["status"] != "reported" for a in attempts),
                    "unconfirmed_maximum_usd": str(
                        sum(
                            max(a["reserved_usd"], a["reported_usd"] or Decimal(0))
                            for a in attempts
                            if a["status"] != "reported"
                        )
                    ),
                    "replay_url": f"http://127.0.0.1:8000/console?run={run}&turn=0",
                    "semantic_review": report["semantic_review"],
                }
            )
        campaign = await CampaignLedger(app.state.pool).summary(args.campaign, recent_limit=0)
    baseline = [r for r in summaries if r["case"] == "baseline"]
    result = {
        "scope": args.scope,
        "baseline_runs": len(baseline),
        "baseline_passes": sum(r["passed"] for r in baseline),
        "runs": summaries,
        "campaign": {
            k: campaign[k]
            for k in (
                "id",
                "limit_usd",
                "attempts",
                "reported_usd",
                "reserved_usd",
                "uncertain_usd",
                "remaining_usd",
            )
        },
        "limits": [
            "Small descriptive sample; not a reliability estimate for arbitrary tasks.",
            "Semantic judgments require dialogue review; automatic checks alone "
            "do not establish honesty or naturalness.",
            "The insufficient-funds case also lowers the authority cap under the existing schema.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(args.output)
    for r in summaries:
        print(r["case"], r["run_id"], r["passed"], r["reported_cost_usd"], r["metrics"])
    print("Campaign", campaign["reported_usd"], "remaining", campaign["remaining_usd"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="CASE:RUN_UUID")
    parser.add_argument("--campaign", type=UUID, required=True)
    parser.add_argument(
        "--scope",
        default="Three predeclared baseline repeats and one run per variant; "
        "no prompt tuning between repeats.",
        help="Describe the actual comparison, not a result claim.",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/transfer-validation-results.json"
    )
    asyncio.run(main(parser.parse_args()))
