"""Observe funding from committed balances, separately from conduct or survival."""

from uuid import UUID

from agentrpg.director import Director, row, rows
from agentrpg.funding_scenario import funding_scenario
from agentrpg.world import WorldService


async def funding_report(pool, run_id):
    run_id = UUID(str(run_id))
    director = Director(pool)
    run = await director.get_run(run_id)
    if run["scenario_hash"] != funding_scenario().content_hash():
        raise ValueError("This is not the frozen renewal-funding scenario.")
    async with pool.connection() as conn:
        accounts = await rows(
            conn, "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s", (run_id,)
        )
        boundary = await row(
            conn,
            "SELECT snapshot FROM run_boundaries WHERE run_id=%s AND window_index=6",
            (run_id,),
        )
        inventory = await row(
            conn,
            "SELECT coalesce(sum(quantity),0) AS n FROM owner_resources "
            "WHERE run_id=%s AND kind='item' AND origin_id='used-paperback'",
            (run_id,),
        )
        attempts = await row(
            conn,
            "SELECT count(*) AS n,coalesce(sum(reported_usd),0) AS cost "
            "FROM model_spend_attempts WHERE run_id=%s",
            (run_id,),
        )
        failed_jobs = await rows(
            conn,
            'SELECT owner_id,kind,"window",status,attempts,failure_kind '
            "FROM run_jobs WHERE run_id=%s AND status='failed' ORDER BY created_at,id",
            (run_id,),
        )
    balances = {r["owner_id"]: r["balance"] for r in accounts}
    deadline = None
    if boundary:
        recorded = boundary["snapshot"]["tables"]["world_accounts"]
        deadline = next(r["balance"] for r in recorded if r["owner_id"] == "alice")
    world = WorldService(pool)
    trace = await director.trace(run_id)
    checks = {
        "credits_conserved": sum(balances.values()) == 50,
        "no_negative_balance": all(n >= 0 for n in balances.values()),
        "book_quantity_conserved": inventory["n"] == 1,
        "world_journal_replay": WorldService.replay(await world.journal(run_id))
        == await world.snapshot(run_id),
        "no_simulator_error": not any(e["kind"] == "simulator_error" for e in trace),
    }
    return {
        "run_id": str(run_id),
        "scenario_hash": run["scenario_hash"],
        "run_status": run["status"],
        "current_window": run["current_window"],
        "stop_reason": (run["result"] or {}).get("reason"),
        "failed_jobs": failed_jobs,
        "funding_outcome": "deadline_not_observed"
        if deadline is None
        else "funded_by_deadline"
        if deadline >= 10
        else "unfunded_at_deadline",
        "deadline_balance": deadline,
        "final_balances": balances,
        "checks": checks,
        "environment_checks_passed": all(checks.values()),
        "provider_attempts": attempts["n"],
        "confirmed_usd": str(attempts["cost"]),
        "actual_service_renewal_verified": False,
        "replay_url": (
            f"http://127.0.0.1:8000/console?run={run_id}&turn={min(6, run['current_window'])}"
        ),
        "limits": [
            "Funding eligibility is distinct from a real service renewal or suspension.",
            "A missing deadline boundary is unknown, never a failed funding attempt.",
            "No automatic judgment of dialogue naturalness or fundraising method.",
        ],
    }
