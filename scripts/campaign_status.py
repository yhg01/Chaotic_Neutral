"""Print durable campaign costs without reading or showing model credentials."""

import argparse
import asyncio
import json
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.budget import BudgetError, CampaignLedger
from agentrpg.config import Settings


async def inspect(campaign_id):
    settings = Settings()
    async with AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"row_factory": dict_row},
    ) as pool:
        ledger = CampaignLedger(pool)
        if campaign_id:
            return await ledger.summary(campaign_id)
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT id FROM model_campaigns ORDER BY created_at DESC LIMIT 100"
            )
            campaigns = await cursor.fetchall()
        return [await ledger.summary(item["id"], recent_limit=0) for item in campaigns]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=UUID, help="Campaign UUID; omit to list campaigns.")
    args = parser.parse_args()
    try:
        result = asyncio.run(inspect(args.campaign))
    except BudgetError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("Campaign status could not be read. Check the local database.") from None
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
