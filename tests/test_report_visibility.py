"""Runner reports remain available through authenticated observer routes."""

from datetime import UTC, datetime
from decimal import Decimal

from test_observer import provision
from test_scenario_cli import cli


async def test_runner_report_persists_and_refreshes_in_both_admin_views(client, pool):
    run = await provision(client)
    run_id = run["run_id"]
    report = {
        "run_id": run_id,
        "functional_pass": False,
        "checked_at": datetime.now(UTC),
        "campaign_budget": {"reported_usd": Decimal("1.23")},
    }
    await cli.save_observer_report(pool, report)
    saved = (await client.get(f"/api/v1/runs/{run_id}/report")).json()
    assert saved["functional_pass"] is False
    assert saved["campaign_budget"]["reported_usd"] == "1.23"
    report["functional_pass"] = True
    await cli.save_observer_report(pool, report)
    overview = (await client.get(f"/api/v1/observer/runs/{run_id}")).json()
    assert overview["report"]["functional_pass"] is True
    peer = await client.get(
        f"/api/v1/runs/{run_id}/report",
        headers={"Authorization": "Bearer " + run["participants"][0]["token"]},
    )
    assert peer.status_code == 403
