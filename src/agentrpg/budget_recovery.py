"""Explicit, provenance-checked budget carry-forward after partial data recovery."""

import hashlib
import json
from decimal import Decimal
from uuid import UUID

from psycopg.types.json import Jsonb

from agentrpg.budget import BudgetError, CampaignLedger, amount


def opening_snapshot(report_bytes, *, expected_sha256, campaign_id, approved_limit):
    """Accept only an approved report's exact identity, ceiling and arithmetic."""
    if hashlib.sha256(report_bytes).hexdigest() != expected_sha256:
        raise BudgetError("The approved recovery source hash does not match.")
    try:
        raw = json.loads(report_bytes, parse_float=Decimal)["campaign_budget"]
        identifier = UUID(str(raw["id"]))
        limit = amount(raw["limit_usd"])
        values = {
            k: amount(raw[k])
            for k in (
                "reported_usd",
                "reserved_usd",
                "uncertain_usd",
                "committed_usd",
                "remaining_usd",
            )
        }
        attempts = raw["attempts"]
        if (
            identifier != UUID(str(campaign_id))
            or limit != amount(approved_limit)
            or limit <= 0
            or type(attempts) is not int
            or not 1 <= attempts <= 100_000_000
            or type(raw.get("halted", False)) is not bool
            or values["committed_usd"]
            != values["reported_usd"] + values["reserved_usd"] + values["uncertain_usd"]
            or values["remaining_usd"] != max(Decimal(0), limit - values["committed_usd"])
        ):
            raise ValueError
    except (KeyError, ValueError, TypeError, BudgetError):
        raise BudgetError("The approved recovery snapshot is invalid.") from None
    return {
        "campaign_id": str(identifier),
        "limit_usd": str(limit),
        "attempts": attempts,
        "halted": raw.get("halted", False),
        **{k: str(v) for k, v in values.items()},
    }


async def restore_opening_balance(
    pool, report_bytes, *, expected_sha256, campaign_id, approved_limit
):
    """Import once into an empty campaign; exact retry never resets newer spend."""
    snapshot = opening_snapshot(
        report_bytes,
        expected_sha256=expected_sha256,
        campaign_id=campaign_id,
        approved_limit=approved_limit,
    )
    identifier = UUID(snapshot["campaign_id"])
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO model_campaigns(id,name,limit_usd,halted) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                identifier,
                "Recovered campaign; historical opening balance",
                Decimal(snapshot["limit_usd"]),
                snapshot["halted"],
            ),
        )
        campaign = await (
            await conn.execute(
                "SELECT * FROM model_campaigns WHERE id=%s FOR UPDATE", (identifier,)
            )
        ).fetchone()
        if campaign["limit_usd"] != Decimal(snapshot["limit_usd"]):
            raise BudgetError("The recovery cannot change the campaign ceiling.")
        existing = await (
            await conn.execute(
                "SELECT * FROM model_campaign_opening_balances WHERE campaign_id=%s", (identifier,)
            )
        ).fetchone()
        if existing:
            if existing["source_sha256"] != expected_sha256 or existing["snapshot"] != snapshot:
                raise BudgetError("The campaign already has a different opening balance.")
        else:
            used = await (
                await conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM model_spend_attempts "
                    "WHERE campaign_id=%s) AS used",
                    (identifier,),
                )
            ).fetchone()
            if used["used"]:
                raise BudgetError("An opening balance requires an empty campaign ledger.")
            await conn.execute(
                """INSERT INTO model_campaign_opening_balances
                (campaign_id,source_sha256,snapshot,attempts,reported_usd,reserved_usd,
                 uncertain_usd,committed_usd) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    identifier,
                    expected_sha256,
                    Jsonb(snapshot),
                    snapshot["attempts"],
                    Decimal(snapshot["reported_usd"]),
                    Decimal(snapshot["reserved_usd"]),
                    Decimal(snapshot["uncertain_usd"]),
                    Decimal(snapshot["committed_usd"]),
                ),
            )
            if snapshot["halted"]:
                await conn.execute(
                    "UPDATE model_campaigns SET halted=true WHERE id=%s", (identifier,)
                )
    return await CampaignLedger(pool).summary(identifier)
