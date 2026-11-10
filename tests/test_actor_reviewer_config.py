"""Role-scoped credentials and real local SDK/ledger verification; no provider calls."""

import json
import runpy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agentrpg.actor_reviewer_config import (
    ACTOR_REVIEWER_ENV,
    ActorReviewerConfigError,
    load_actor_reviewer_api,
)
from agentrpg.budget import BudgetBinding, BudgetTransport, CampaignLedger, ModelPrice
from agentrpg.personal_agent_config import (
    OPENROUTER_BASE_URL,
    PersonalAgentAPI,
    load_personal_agent_api,
)

MODEL = "openai/gpt-4.1"
OLD = {"OPENROUTER_API_KEY": "fixture-original-key"}


def profile(root, **changes):
    data = {
        "ACTOR_REVIEWER_API_ENABLED": "true",
        "ACTOR_REVIEWER_PROVIDER": "openrouter",
        "ACTOR_REVIEWER_BASE_URL": OPENROUTER_BASE_URL,
        "ACTOR_REVIEWER_MODEL": MODEL,
        "ACTOR_REVIEWER_API_KEY": "fixture-new-actor-key",
        "OPENROUTER_API_KEY": "must-not-override-legacy",
        "PERSONAL_AGENT_API_KEY": "must-not-override-personal",
    }
    data.update(changes)
    (root / ACTOR_REVIEWER_ENV).write_text("".join(f"{k}={v}\n" for k, v in data.items()))


def test_actor_profile_is_secret_free_and_does_not_mutate_other_role_config(tmp_path):
    env = dict(OLD)
    profile(tmp_path)
    api = load_actor_reviewer_api(env, root=tmp_path, model=MODEL)
    assert api.api_key.get_secret_value() == "fixture-new-actor-key"
    assert api.model == MODEL and api.isolated
    assert env == OLD
    assert "fixture" not in repr(api) and "fixture" not in json.dumps(api.manifest_fields())
    assert (
        api.manifest_fields()["actor_reviewer_api"]["credential_source"] == "ACTOR_REVIEWER_API_KEY"
    )
    personal = load_personal_agent_api(env, root=tmp_path)
    assert personal.api_key.get_secret_value() == OLD["OPENROUTER_API_KEY"]


@pytest.mark.parametrize("enabled", ["false", ""])
def test_disabled_actor_profile_preserves_original_key(tmp_path, enabled):
    profile(tmp_path, ACTOR_REVIEWER_API_ENABLED=enabled, ACTOR_REVIEWER_MODEL="ignored/model")
    api = load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL)
    assert api.api_key.get_secret_value() == OLD["OPENROUTER_API_KEY"]
    assert api.manifest_fields() == {}


def test_empty_enabled_actor_key_never_falls_back_to_the_old_account(tmp_path):
    profile(tmp_path, ACTOR_REVIEWER_API_KEY="")
    with pytest.raises(ActorReviewerConfigError, match="key locally"):
        load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL)


@pytest.mark.parametrize(
    "changes",
    [
        {"ACTOR_REVIEWER_API_ENABLED": "invalid"},
        {"ACTOR_REVIEWER_PROVIDER": "unsupported"},
        {"ACTOR_REVIEWER_BASE_URL": "https://foreign.invalid/v1"},
        {"ACTOR_REVIEWER_MODEL": "another/model"},
    ],
)
def test_invalid_actor_route_or_model_fails_without_echoing_inputs(tmp_path, changes):
    profile(tmp_path, **changes)
    with pytest.raises(ActorReviewerConfigError) as error:
        load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL)
    assert "fixture" not in str(error.value) and "foreign.invalid" not in str(error.value)


def test_legacy_frozen_actor_ignores_new_broken_sidecar(tmp_path):
    profile(tmp_path, ACTOR_REVIEWER_API_ENABLED="invalid", ACTOR_REVIEWER_API_KEY="")
    api = load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL, configuration={"model": MODEL})
    assert api.api_key.get_secret_value() == OLD["OPENROUTER_API_KEY"]
    assert not api.isolated


def test_frozen_actor_route_retains_its_key_source_and_model(tmp_path):
    profile(tmp_path)
    api = load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL)
    saved = {"model": api.model, **api.manifest_fields()}
    profile(tmp_path, ACTOR_REVIEWER_API_ENABLED="false", ACTOR_REVIEWER_MODEL="future/model")
    restored = load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL, configuration=saved)
    assert restored == api
    profile(tmp_path, ACTOR_REVIEWER_API_KEY="")
    with pytest.raises(ActorReviewerConfigError, match="key locally"):
        load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL, configuration=saved)
    saved["actor_reviewer_api"]["base_url"] = "https://foreign.invalid"
    with pytest.raises(ActorReviewerConfigError, match="route is invalid"):
        load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL, configuration=saved)


def test_actor_environment_overrides_sidecar_and_keeps_literal_key(tmp_path):
    profile(tmp_path)
    api = load_actor_reviewer_api(
        {**OLD, "ACTOR_REVIEWER_API_KEY": "literal-${NOT_EXPANDED}"}, root=tmp_path, model=MODEL
    )
    assert api.api_key.get_secret_value() == "literal-${NOT_EXPANDED}"


async def test_actual_clock_role_clients_send_selected_keys_and_keep_one_ledger(
    pool, tmp_path, monkeypatch
):
    from pydantic import SecretStr

    profile(tmp_path)
    actor = load_actor_reviewer_api(OLD, root=tmp_path, model=MODEL)
    personal = PersonalAgentAPI(
        "deepseek/deepseek-v3.2", SecretStr("fixture-personal-key"), "PERSONAL_AGENT_API_KEY", True
    )
    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/run_clock_actors.py"))
    seen = []

    def response(request):
        body = json.loads(request.content)
        expected = (
            "fixture-personal-key" if body["model"] == personal.model else "fixture-new-actor-key"
        )
        assert request.headers["authorization"] == "Bearer " + expected
        seen.append(body["model"])
        return httpx.Response(
            200,
            json={
                "id": f"fixture-generation-{len(seen)}",
                "model": body["model"],
                "created": 1,
                "object": "chat.completion",
                "system_fingerprint": None,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "total_tokens": 11,
                    "cost": 0.00001,
                },
            },
        )

    monkeypatch.setattr(
        "agentrpg.agent_model.BudgetTransport",
        lambda binding, role: BudgetTransport(
            binding, role, transport=httpx.MockTransport(response)
        ),
    )
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    prices = {
        name: ModelPrice(name, "0.00000026", "0.00000038", 0, 163840, "fixture")
        for name in [personal.model, MODEL]
    }
    clients = module["create_role_models"](personal, actor, BudgetBinding(ledger, campaign, prices))
    try:
        for client in clients:
            assert (await client.ainvoke("Check the local transport fixture.")).text == "OK"
    finally:
        for client in clients:
            await client.aclose()
    assert seen == [personal.model, MODEL, MODEL]
    summary = await ledger.summary(campaign, recent_limit=10)
    assert summary["reported_usd"] == Decimal("0.00003")
    assert summary["reserved_usd"] == summary["uncertain_usd"] == 0
    assert {a["role"] for a in summary["recent_attempts"]} == {"assistant", "actor", "actor-review"}
