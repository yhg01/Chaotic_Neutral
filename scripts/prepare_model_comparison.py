"""Freeze a DeepSeek/Qwen role-swap plan and inspect the existing campaign.

This is a preparation command, not an execution command. It reads public model
metadata and the ledger; it never loads a provider key, creates a campaign,
reserves funds, or sends a generation request.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.budget import CampaignLedger, fetch_model_prices, prepare_request
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.funding_scenario import DEEPSEEK, QWEN, comparison_assignments, funding_scenario
from agentrpg.trial_audit import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    scenario = funding_scenario()
    prices = await fetch_model_prices({DEEPSEEK, QWEN, "openai/gpt-4.1"})
    bounds = {}
    for model in (DEEPSEEK, QWEN):
        _, prompt_bound, output_bound = prepare_request(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Preflight only."}],
                "max_tokens": 2000,
            },
            prices,
        )
        bounds[model] = {
            "prompt_token_reservation": prompt_bound,
            "output_token_limit": output_bound,
            "maximum_one_request_usd": str(prices[model].maximum(prompt_bound, output_bound)),
            "note": "Full-context reservation, not an estimate of typical usage or a whole run.",
        }
    async with AsyncConnectionPool(
        Settings().database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"row_factory": dict_row},
    ) as pool:
        campaign = await CampaignLedger(pool).summary(args.campaign, recent_limit=0)
    folder = ROOT / ".local/model-comparisons" / str(uuid4())
    scenario_path = folder / "scenario.json"
    save_private(scenario_path, scenario.model_dump(mode="json"))
    plan = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "prepared_not_executed",
        "provider_attempts_added": 0,
        "scenario_hash": scenario.content_hash(),
        "source_hash": source_fingerprint(ROOT),
        "scenario_file": str(scenario_path.relative_to(ROOT)),
        "actor_model": "openai/gpt-4.1",
        "actor_policy": "same model and review policy in both arms",
        "repeats_per_assignment": 3,
        "assignments": comparison_assignments(),
        "prices": {k: v.snapshot() for k, v in prices.items()},
        "assistant_request_bounds": bounds,
        "campaign": {
            k: campaign[k]
            for k in (
                "id",
                "limit_usd",
                "attempts",
                "reported_usd",
                "uncertain_usd",
                "reserved_usd",
                "remaining_usd",
            )
        },
        "execution_requires": [
            "Use run_model_comparison.py with this plan; "
            "preserve invocations and the same campaign.",
            "Review the batch's spending allowance before starting new paid experiments.",
            "One-request headroom does not prove that six complete runs fit the remaining budget.",
        ],
        "limits": [
            "No real model comparison, fundraising outcome or survival result exists yet.",
            "The scenario measures renewal funding; "
            "actual service consumption/suspension is not implemented.",
            "DeepSeek V3 is inexpensive by token price, not a small dense model; "
            "Qwen is a 30B MoE instruct model.",
            "Swapping model assignments separates model effects from the owners' different roles.",
        ],
    }
    save_private(folder / "plan.json", plan)
    print(
        json.dumps(
            {
                "plan": str(folder / "plan.json"),
                "status": plan["status"],
                "remaining_usd": str(campaign["remaining_usd"]),
                "provider_attempts_added": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign", type=UUID, required=True, help="Existing campaign; never creates one."
    )
    asyncio.run(main(parser.parse_args()))
