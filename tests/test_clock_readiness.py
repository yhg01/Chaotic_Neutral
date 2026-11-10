"""Startup checks must fail before provider access when frozen inputs drift."""

import json
import runpy
from argparse import Namespace
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agentrpg.community_experiment import CommunityPlan
from agentrpg.personal_agent_config import OPENROUTER_BASE_URL, load_personal_agent_api
from agentrpg.simulation_clock import digest
from agentrpg.trial_audit import source_fingerprint


@pytest.mark.parametrize(
    "change", ["prompt", "gate", "route", "estimate", "ceiling", "transport", "actor_route"]
)
async def test_readiness_binds_non_source_configuration_before_provider_access(tmp_path, change):
    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/run_clock_actors.py"))
    main = module["main"]
    main.__globals__["ROOT"] = tmp_path
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/fixture.py").write_text("pass\n")
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=local-test-only\nOPENROUTER_MODEL=deepseek/deepseek-v3.2\n"
    )
    sidecar = tmp_path / ".env.personal-agent.local"
    sidecar.write_text(
        "PERSONAL_AGENT_API_ENABLED=true\nPERSONAL_AGENT_PROVIDER=openrouter\n"
        f"PERSONAL_AGENT_BASE_URL={OPENROUTER_BASE_URL}\n"
        "PERSONAL_AGENT_MODEL=deepseek/deepseek-v3.2\nPERSONAL_AGENT_API_KEY=local-personal-test-only\n"
    )
    baseline = {
        "actor_model": "openai/gpt-4.1",
        "actor_prompt": "Frozen owner policy.",
        "actor_review_prompt": "Frozen independent review policy.",
        "require_transfer_authority_review": True,
    }
    personal = load_personal_agent_api(dict(dotenv_values(tmp_path / ".env")), root=tmp_path)
    ready = {
        "eligible": True,
        "plan_hash": digest(CommunityPlan(days=7, events_per_day=1).manifest()),
        "source_hash": source_fingerprint(tmp_path),
        "campaign_id": str(module["CAMPAIGN"]),
        "ceiling_usd": str(module["CUMULATIVE_CEILING"]),
        "estimated_remaining_experiment_usd": "1",
        "execution_configuration_hash": module["execution_configuration_hash"](baseline, personal),
    }
    if change == "prompt":
        baseline["actor_prompt"] = "A changed owner policy."
    if change == "gate":
        baseline["require_transfer_authority_review"] = False
    if change == "route":
        sidecar.write_text(sidecar.read_text().replace("ENABLED=true", "ENABLED=false"))
    if change == "estimate":
        ready["estimated_remaining_experiment_usd"] = "-1"
    if change == "ceiling":
        ready["ceiling_usd"] = "15"
    if change == "transport":
        main.__globals__["PERSONAL_TRANSPORT"] = {"timeout_seconds": 45, "max_retries": 2}
    if change == "actor_route":
        (tmp_path / ".env.actor-reviewer.local").write_text(
            "ACTOR_REVIEWER_API_ENABLED=true\nACTOR_REVIEWER_PROVIDER=openrouter\n"
            f"ACTOR_REVIEWER_BASE_URL={OPENROUTER_BASE_URL}\n"
            "ACTOR_REVIEWER_API_KEY=local-new-actor-fixture\n"
        )
    baseline_path, ready_path = tmp_path / "baseline.json", tmp_path / "readiness.json"
    baseline_path.write_text(json.dumps({"configuration": baseline}))
    ready_path.write_text(json.dumps(ready))
    called = False

    async def forbidden(*args):
        nonlocal called
        called = True
        raise AssertionError(
            "No provider/catalog access is allowed after a configuration mismatch."
        )

    main.__globals__["fetch_model_prices"] = forbidden
    args = Namespace(
        pairs=3,
        days=7,
        seed=20261109,
        events_per_day=1,
        prepare=None,
        resume=None,
        scenario=None,
        no_chaos=False,
        preflight=None,
        readiness=ready_path,
        baseline_manifest=baseline_path,
        max_actions=10,
    )
    with pytest.raises(
        SystemExit,
        match=(
            "positive finite"
            if change == "estimate"
            else "campaign or cumulative ceiling differs"
            if change == "ceiling"
            else "execution configuration differs"
        ),
    ):
        await main(args)
    assert not called
