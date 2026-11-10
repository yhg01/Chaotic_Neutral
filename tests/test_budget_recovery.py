"""Real PostgreSQL opening balances, immutable history and per-role call fences."""

import asyncio
import hashlib
import json
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from agentrpg.budget import BudgetError, BudgetExceeded, CampaignLedger, ModelPrice
from agentrpg.budget_recovery import opening_snapshot, restore_opening_balance
from agentrpg.console_execution import campaign_totals


def report(campaign, **changes):
    snapshot = {
        "id": str(campaign),
        "limit_usd": "5.5",
        "attempts": 1467,
        "halted": False,
        "reported_usd": "4.89333461",
        "reserved_usd": "0",
        "uncertain_usd": "0.4079752",
        "committed_usd": "5.30130981",
        "remaining_usd": "0.19869019",
        **changes,
    }
    return json.dumps({"campaign_budget": snapshot}).encode()


def options(data, campaign):
    return {
        "expected_sha256": hashlib.sha256(data).hexdigest(),
        "campaign_id": campaign,
        "approved_limit": "5.5",
    }


def unit():
    return ModelPrice("openai/gpt-4.1-mini", "0.000001", "0.000001", "0", 100000, "fixture")


@pytest.mark.parametrize(
    "change",
    [
        {"limit_usd": "6"},
        {"remaining_usd": "5.5"},
        {"uncertain_usd": "0"},
        {"attempts": True},
        {"attempts": 0},
        {"reported_usd": "NaN"},
        {"halted": "false"},
    ],
)
def test_snapshot_cannot_increase_headroom_or_change_contract(change):
    campaign = uuid4()
    data = report(campaign, **change)
    with pytest.raises(BudgetError):
        opening_snapshot(data, **options(data, campaign))


def test_changed_bytes_or_wrong_campaign_are_rejected():
    campaign = uuid4()
    data = report(campaign)
    with pytest.raises(BudgetError):
        opening_snapshot(data + b" ", **options(data, campaign))
    with pytest.raises(BudgetError):
        opening_snapshot(data, **options(data, uuid4()))


async def test_opening_balance_is_not_a_fake_attempt_and_new_spend_accumulates(pool):
    campaign = uuid4()
    data = report(campaign)
    initial = await restore_opening_balance(pool, data, **options(data, campaign))
    assert initial["attempts"] == 1467 and initial["local_attempts"] == 0
    assert initial["remaining_usd"] == Decimal("0.19869019")
    ledger = CampaignLedger(pool)
    attempt = await ledger.reserve(campaign, unit(), 1000, 100, role="probe", max_role_attempts=3)
    await ledger.finish(attempt, reported="0.0001", generation_id="fixture-generation")
    after = await restore_opening_balance(pool, data, **options(data, campaign))
    assert after["attempts"] == 1468 and after["local_attempts"] == 1
    assert after["reported_usd"] == Decimal("4.89343461")
    assert after["uncertain_usd"] == Decimal("0.4079752")
    assert after["remaining_usd"] == Decimal("0.19859019")
    async with pool.connection() as conn:
        actual = await (
            await conn.execute("SELECT count(*) AS n FROM model_spend_attempts")
        ).fetchone()
        visible = await campaign_totals(conn, campaign)
    assert actual["n"] == 1
    assert visible["spent"] + visible["reserved"] == after["committed_usd"]


async def test_snapshot_import_is_concurrent_and_idempotent_but_cannot_be_replaced(pool):
    campaign = uuid4()
    data = report(campaign)
    results = await asyncio.gather(
        *[restore_opening_balance(pool, data, **options(data, campaign)) for _ in range(5)]
    )
    assert all(x["remaining_usd"] == Decimal("0.19869019") for x in results)
    other = report(campaign, attempts=1466)
    with pytest.raises(BudgetError, match="different opening"):
        await restore_opening_balance(pool, other, **options(other, campaign))
    for command in (
        "UPDATE model_campaign_opening_balances SET uncertain_usd=uncertain_usd",
        "DELETE FROM model_campaign_opening_balances",
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            async with pool.connection() as conn:
                await conn.execute(command)


async def test_carry_forward_cannot_be_applied_to_an_active_ledger(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "5.5")
    await ledger.reserve(campaign, unit(), 100, 100)
    data = report(campaign)
    with pytest.raises(BudgetError, match="empty campaign"):
        await restore_opening_balance(pool, data, **options(data, campaign))


async def test_historic_unknown_cost_cannot_be_released_to_fund_another_request(pool):
    campaign = uuid4()
    data = report(campaign)
    await restore_opening_balance(pool, data, **options(data, campaign))
    ledger = CampaignLedger(pool)
    with pytest.raises(BudgetExceeded):
        await ledger.reserve(campaign, unit(), 200000, 1)
    assert (await ledger.summary(campaign))["local_attempts"] == 0


async def test_only_three_concurrent_attempts_and_uncertain_calls_still_count_after_restart(pool):
    campaign = uuid4()
    data = report(campaign)
    await restore_opening_balance(pool, data, **options(data, campaign))
    ledger = CampaignLedger(pool)
    results = await asyncio.gather(
        *[
            ledger.reserve(campaign, unit(), 1000, 100, role="probe", max_role_attempts=3)
            for _ in range(12)
        ],
        return_exceptions=True,
    )
    accepted = [x for x in results if not isinstance(x, BaseException)]
    assert len(accepted) == 3
    assert sum(isinstance(x, BudgetExceeded) for x in results) == 9
    for attempt in accepted:
        await ledger.finish(attempt)  # Unknown provider outcome retains full reservation.
    with pytest.raises(BudgetExceeded, match="attempt limit"):
        await CampaignLedger(pool).reserve(
            campaign, unit(), 1000, 100, role="probe", max_role_attempts=3
        )
    state = await ledger.summary(campaign)
    assert state["attempts"] == 1470 and state["local_attempts"] == 3
    assert state["uncertain_usd"] == Decimal("0.4112752")


@pytest.mark.parametrize("limit", [0, 101, True, 3.0])
async def test_invalid_request_limits_fail_before_a_reservation(pool, limit):
    with pytest.raises(BudgetError):
        await CampaignLedger(pool).reserve(uuid4(), unit(), 1, 1, max_role_attempts=limit)
