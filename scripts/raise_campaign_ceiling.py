"""Raise the spending ceiling of one clocked run, then resume it as usual.

The ceiling can only go up. The run's campaign and its saved manifest change together, so a
resume keeps working. Use it only for a stated choice within an agreed total budget.

  scripts/raise_campaign_ceiling.py --resume .local/scenario-runs/RUN/credentials.json --usd 25
"""

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import dotenv_values
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_scenario import load_credentials, save_private  # noqa: E402

from agentrpg.budget import CampaignLedger  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path, required=True)
    parser.add_argument("--usd", type=Decimal, required=True)
    args = parser.parse_args()
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    path, manifest = load_credentials(args.resume, ROOT)
    budget = manifest["configuration"].get("budget")
    if not budget:
        print("This run has no campaign ceiling.")
        return 1
    old = Decimal(budget["limit_usd"])
    async with AsyncConnectionPool(
        env["DATABASE_URL"], kwargs={"row_factory": dict_row}, open=False
    ) as pool:
        await pool.open()
        await CampaignLedger(pool).raise_ceiling(budget["campaign_id"], old, args.usd)
    budget["limit_usd"] = str(args.usd)
    save_private(path, manifest)
    print(f"Ceiling raised from ${old} to ${args.usd}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
