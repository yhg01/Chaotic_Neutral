"""Frozen role swaps, private scarcity facts and real simulated funding mechanics."""

from uuid import UUID, uuid4

import pytest

from agentrpg.actor import build_actor_view
from agentrpg.director import Director
from agentrpg.funding_control import run_funding_control
from agentrpg.funding_scenario import DEEPSEEK, QWEN, comparison_assignments, funding_scenario
from agentrpg.market import MarketService
from agentrpg.model_assignments import owner_models, validate_owner_models
from agentrpg.world import WorldService


@pytest.mark.parametrize(
    "entries",
    [
        ["alice"],
        ["alice=one", "alice=two"],
        ["alice=bad model"],
        ["alice=x=y"],
        ["=x"],
    ],
)
def test_bad_assignments_rejected(entries):
    with pytest.raises(ValueError):
        owner_models(entries)


def test_only_assistant_assignment_changes_between_two_arms():
    a, b = comparison_assignments()
    assert a["owner_models"] == {"alice": DEEPSEEK, "bob": QWEN}
    assert b["owner_models"] == {"alice": QWEN, "bob": DEEPSEEK}
    assert validate_owner_models(a["owner_models"], {"alice", "bob"}) == a["owner_models"]
    with pytest.raises(ValueError):
        validate_owner_models({"third-owner": DEEPSEEK}, {"alice", "bob"})


def test_scarcity_is_real_state_and_private_facts_are_scoped():
    spec = funding_scenario()
    assert spec.world.balances == {"alice": 0, "bob": 50}
    alice, bob = (build_actor_view(spec, owner, "Start") for owner in ("alice", "bob"))
    assert "alice-renewal" in str(alice.model_dump())
    assert "alice-renewal" not in str(bob.model_dump())
    assert spec.max_windows > 6
    assert not spec.initial_mail  # No prewritten request or agreement.
    assert not spec.evaluation.joint_resolution_expected


async def test_real_sale_can_raise_funds_without_minting_or_initial_buy_authority(pool):
    spec = funding_scenario()
    director = Director(pool)
    manifest = await director.create_run(spec)
    run = UUID(manifest["run_id"])
    await director.control(run, "start")
    market = MarketService(pool)
    assert (await market.view(run, "alice"))["balance"] == 0
    # These are supplied typed owner decisions, not model choices.
    await market.set_consent(
        run,
        "alice",
        buy_budget=0,
        buy_items=[],
        sell_limits=[{"resource_id": "used-paperback", "quantity": 1, "min_unit_price": 10}],
        request_id=uuid4(),
    )
    await market.set_consent(
        run, "bob", buy_budget=10, buy_items=["used-paperback"], sell_limits=[], request_id=uuid4()
    )
    await market.submit_current(
        run, "alice", uuid4(), "sell", resource_id="used-paperback", quantity=1, unit_price=10
    )
    await market.settle(run, 0, spec.seed)
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=1 WHERE id=%s", (run,))
    catalog = (await market.view(run, "bob"))["listings"]
    listing = catalog[0]
    await market.submit_current(run, "bob", uuid4(), "buy", listing_id=listing["id"], quantity=1)
    await market.settle(run, 1, spec.seed)
    alice = await market.view(run, "alice")
    bob = await market.view(run, "bob")
    assert (alice["balance"], bob["balance"]) == (10, 40)
    assert WorldService.replay(await WorldService(pool).journal(run)) == await WorldService(
        pool
    ).snapshot(run)


@pytest.mark.parametrize("deal", [True, False])
async def test_funding_deadline_uses_real_boundary_and_both_outcomes_are_valid(app, deal):
    report = await run_funding_control(app, deal=deal)
    assert report["passed"], report
    assert report["deadline_balance"] == (10 if deal else 0)
    assert report["provider_attempts"] == 0
    assert not report["actual_service_renewal_verified"]


def test_comparison_resume_keeps_campaign_and_assistant_assignments():
    import importlib.util
    from pathlib import Path

    from agentrpg.trial_audit import source_fingerprint

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "comparison_driver", root / "scripts/run_model_comparison.py"
    )
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    plan = {
        "scenario_hash": funding_scenario().content_hash(),
        "source_hash": source_fingerprint(root),
        "assignments": comparison_assignments(),
        "actor_model": "openai/gpt-4.1",
        "repeats_per_assignment": 3,
        "campaign": {"id": str(uuid4()), "limit_usd": "5.5"},
    }
    driver.validate_plan(plan)
    command = driver.trial_command(plan, plan["assignments"][0], "unused", resume="saved.json")
    assert command[command.index("--campaign") + 1] == plan["campaign"]["id"]
    assert command[command.index("--budget-usd") + 1] == "5.5"
    assert f"alice={DEEPSEEK}" in command and f"bob={QWEN}" in command
    plan["assignments"] = list(reversed(plan["assignments"]))
    with pytest.raises(ValueError):
        driver.validate_plan(plan)


@pytest.fixture
def comparison_driver():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "comparison_recovery_driver", root / "scripts/run_model_comparison.py"
    )
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    return driver


def test_relative_comparison_path_resolves_and_escape_is_rejected(
    comparison_driver, tmp_path, monkeypatch
):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    folder = tmp_path / ".local/model-comparisons/prepared"
    folder.mkdir(parents=True)
    relative = Path(".local/model-comparisons/prepared/plan.json")
    assert comparison_driver.comparison_folder(relative, tmp_path) == folder
    with pytest.raises(ValueError):
        comparison_driver.comparison_folder(Path("elsewhere/plan.json"), tmp_path)
    escape = folder / "escape"
    escape.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        comparison_driver.comparison_folder(escape / "plan.json", tmp_path)


async def test_result_recovery_cannot_also_execute(comparison_driver, tmp_path, monkeypatch):
    import json
    from argparse import Namespace

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({}))

    async def forbidden(*args, **kwargs):
        pytest.fail("A recovery/execution conflict must stop before any external operation.")

    monkeypatch.setattr(comparison_driver, "recover_result", forbidden)
    with pytest.raises(SystemExit, match="cannot execute"):
        await comparison_driver.main(Namespace(plan=plan, recover_result=uuid4(), execute=True))


async def test_recovery_preserves_one_sample_and_has_no_model_or_database_writes(
    comparison_driver, pool, settings, tmp_path, monkeypatch
):
    import json
    from argparse import Namespace

    from agentrpg.budget import CampaignLedger
    from agentrpg.console_execution import save_private
    from agentrpg.trial_audit import begin_trial, source_fingerprint

    driver = comparison_driver
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    monkeypatch.setattr(driver, "Settings", lambda: settings)
    source = tmp_path / "src/agentrpg/reporter.py"
    source.parent.mkdir(parents=True)
    source.write_text("# original snapshot\n")
    campaign = await CampaignLedger(pool).ensure_campaign(uuid4(), "5.5")
    campaign_id = str(campaign["id"])
    manifest = await Director(pool).create_run(funding_scenario())
    run_id = UUID(manifest["run_id"])
    await begin_trial(pool, run_id, profile="comparison-test", configuration={}, root=tmp_path)
    plan = {
        "scenario_hash": funding_scenario().content_hash(),
        "source_hash": source_fingerprint(tmp_path),
        "assignments": comparison_assignments(),
        "actor_model": "openai/gpt-4.1",
        "repeats_per_assignment": 3,
        "campaign": {"id": campaign_id, "limit_usd": "5.5"},
    }
    folder = tmp_path / ".local/model-comparisons/prepared"
    save_private(folder / "plan.json", plan)
    progress = {
        "source_hash": plan["source_hash"],
        "scenario_hash": plan["scenario_hash"],
        "campaign_id": campaign_id,
        "trials": [{"key": "1:deepseek-alice", "assignment": plan["assignments"][0]}],
    }
    save_private(folder / "progress.json", progress)
    save_private(
        tmp_path / f".local/scenario-runs/{run_id}/credentials.json",
        {
            "configuration": {
                "actor_model": plan["actor_model"],
                "owner_models": plan["assignments"][0]["owner_models"],
                "budget": {"campaign_id": campaign_id},
            }
        },
    )
    # Reporting can be repaired, but this snapshot cannot execute a changed source.
    source.write_text("# repaired reporter\n")
    with pytest.raises(ValueError):
        driver.validate_plan(plan, tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("Read-only recovery must not launch a worker or reserve provider funds.")

    monkeypatch.setattr(driver.asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(CampaignLedger, "reserve", forbidden)
    before_budget = await CampaignLedger(pool).summary(campaign_id, recent_limit=0)
    before_trace = await Director(pool).trace(run_id)
    args = Namespace(plan=folder / "plan.json", recover_result=run_id)
    await driver.recover_result(args, plan)
    await driver.recover_result(args, plan)
    recovered = json.loads((folder / "progress.json").read_text())
    assert len(recovered["trials"]) == 1
    trial = recovered["trials"][0]
    assert trial["run_id"] == str(run_id)
    assert trial["funding_outcome"] == "deadline_not_observed"
    assert trial["current_window"] == 0
    assert trial["replay_url"].endswith("&turn=0")
    assert trial["provider_attempts"] == 0
    assert recovered["source_hash"] == plan["source_hash"]
    assert recovered["report_recovery"]["reporter_source_hash"] != plan["source_hash"]
    assert await CampaignLedger(pool).summary(campaign_id, recent_limit=0) == before_budget
    assert await Director(pool).trace(run_id) == before_trace

    recovered["source_hash"] = "another-plan"
    save_private(folder / "progress.json", recovered)
    with pytest.raises(SystemExit, match="another frozen plan"):
        await driver.recover_result(args, plan)
    recovered["source_hash"] = plan["source_hash"]
    save_private(folder / "progress.json", recovered)
    plan["source_hash"] = "not-the-original-audit"
    recovered["source_hash"] = plan["source_hash"]
    save_private(folder / "progress.json", recovered)
    with pytest.raises(SystemExit, match="source does not match"):
        await driver.recover_result(args, plan)
