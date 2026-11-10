"""Assistant-only routing, frozen recovery, and real local SDK/tool persistence.

Provider responses are supplied fixtures. No DeepSeek behavioral claim is made.
"""

import importlib.util
import json
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from test_deep_agent import ScriptedModel, board_client

from agentrpg.agent_model import create_model
from agentrpg.agent_tools import BoardTools
from agentrpg.budget import BudgetBinding, BudgetError, BudgetTransport, CampaignLedger, ModelPrice
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director
from agentrpg.personal_agent_config import (
    DEFAULT_DEEPSEEK_MODEL,
    OPENROUTER_BASE_URL,
    PERSONAL_AGENT_ENV,
    PersonalAgentConfigError,
    load_personal_agent_api,
)
from agentrpg.shop_demo import shopping_model_scenario
from agentrpg.shop_runtime import make_engine


def profile(root, **changes):
    values = {
        "PERSONAL_AGENT_API_ENABLED": "true",
        "PERSONAL_AGENT_PROVIDER": "openrouter",
        "PERSONAL_AGENT_MODEL": DEFAULT_DEEPSEEK_MODEL,
        "PERSONAL_AGENT_BASE_URL": OPENROUTER_BASE_URL,
        "PERSONAL_AGENT_API_KEY": "fixture-personal-secret",
        # A sidecar must not override these other roles.
        "OPENROUTER_API_KEY": "wrong-sidecar-key",
        "OPENROUTER_ACTOR_MODEL": "wrong-sidecar-actor",
    }
    values.update(changes)
    (root / PERSONAL_AGENT_ENV).write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def legacy_env():
    return {"OPENROUTER_API_KEY": "fixture-actor-secret", "OPENROUTER_MODEL": "legacy/model"}


def test_independent_profile_contains_no_secrets_or_cross_role_overrides(tmp_path):
    env = legacy_env()
    profile(tmp_path)
    api = load_personal_agent_api(env, root=tmp_path)
    assert api.model == DEFAULT_DEEPSEEK_MODEL
    assert api.api_key.get_secret_value() == "fixture-personal-secret"
    assert env == legacy_env()
    metadata = api.manifest_fields()
    assert "secret" not in repr(api) and "secret" not in json.dumps(metadata)
    assert metadata["personal_agent_api"]["credential_source"] == "PERSONAL_AGENT_API_KEY"
    assert (
        load_personal_agent_api(env, root=tmp_path, model="explicit/model").model
        == "explicit/model"
    )


@pytest.mark.parametrize("enabled", ["false", ""])
def test_disabled_profile_preserves_legacy_credentials_and_default(tmp_path, enabled):
    profile(tmp_path, PERSONAL_AGENT_API_ENABLED=enabled, PERSONAL_AGENT_MODEL="invalid model")
    api = load_personal_agent_api(legacy_env(), root=tmp_path)
    assert (api.model, api.api_key.get_secret_value()) == ("legacy/model", "fixture-actor-secret")
    assert api.manifest_fields() == {}


def test_missing_personal_key_never_falls_back_to_actor_key(tmp_path):
    profile(tmp_path, PERSONAL_AGENT_API_KEY="")
    with pytest.raises(PersonalAgentConfigError, match="API key locally"):
        load_personal_agent_api(legacy_env(), root=tmp_path)


@pytest.mark.parametrize(
    "changes",
    [
        {"PERSONAL_AGENT_BASE_URL": "https://foreign.example/v1"},
        {"PERSONAL_AGENT_PROVIDER": "other"},
        {"PERSONAL_AGENT_API_ENABLED": "maybe"},
        {"PERSONAL_AGENT_MODEL": ""},
        {"PERSONAL_AGENT_MODEL": "bad model"},
    ],
)
def test_invalid_routing_fails_without_echoing_values(tmp_path, changes):
    profile(tmp_path, **changes)
    with pytest.raises(PersonalAgentConfigError) as caught:
        load_personal_agent_api(legacy_env(), root=tmp_path)
    assert "foreign.example" not in str(caught.value) and "secret" not in str(caught.value)


def test_environment_override_and_literal_key_are_preserved(tmp_path):
    profile(tmp_path, PERSONAL_AGENT_API_KEY="literal-${NOT_A_VARIABLE}")
    api = load_personal_agent_api(legacy_env(), root=tmp_path)
    assert api.api_key.get_secret_value() == "literal-${NOT_A_VARIABLE}"
    api = load_personal_agent_api(
        {**legacy_env(), "PERSONAL_AGENT_MODEL": "override/model"}, root=tmp_path
    )
    assert api.model == "override/model"


def test_legacy_resume_ignores_even_a_broken_new_profile(tmp_path):
    profile(tmp_path, PERSONAL_AGENT_API_ENABLED="invalid", PERSONAL_AGENT_API_KEY="")
    saved = {"model": "saved/original"}
    api = load_personal_agent_api(legacy_env(), root=tmp_path, configuration=saved)
    assert api.model == "saved/original" and not api.isolated
    assert api.api_key.get_secret_value() == "fixture-actor-secret"
    assert saved == {"model": "saved/original"}


def test_new_resume_retains_model_route_and_requires_its_own_key(tmp_path):
    profile(tmp_path)
    api = load_personal_agent_api(legacy_env(), root=tmp_path)
    saved = {"model": api.model, **api.manifest_fields()}
    profile(tmp_path, PERSONAL_AGENT_MODEL="changed/model", PERSONAL_AGENT_API_ENABLED="false")
    assert (
        load_personal_agent_api(legacy_env(), root=tmp_path, configuration=saved).model == api.model
    )
    profile(tmp_path, PERSONAL_AGENT_API_KEY="")
    with pytest.raises(PersonalAgentConfigError):
        load_personal_agent_api(legacy_env(), root=tmp_path, configuration=saved)
    saved["personal_agent_api"]["base_url"] = "https://foreign.example"
    with pytest.raises(PersonalAgentConfigError, match="route is invalid"):
        load_personal_agent_api(legacy_env(), root=tmp_path, configuration=saved)


@pytest.mark.parametrize(
    "options,extra,expected_actor",
    [
        ([], {}, "legacy/model"),
        ([], {"OPENROUTER_ACTOR_MODEL": "owner/model"}, "owner/model"),
        (["--actor-model", "explicit/actor"], {}, "explicit/actor"),
        (["--model", "explicit/assistant"], {}, "explicit/assistant"),
    ],
)
def test_scenario_preserves_original_actor_selection(tmp_path, options, extra, expected_actor):
    spec = importlib.util.spec_from_file_location(
        "personal_config_scenario", Path(__file__).resolve().parents[1] / "scripts/run_scenario.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    profile(tmp_path)
    config = cli.new_run_configuration(
        cli.parse_args(options), {**legacy_env(), **extra}, root=tmp_path
    )
    assert config["actor_model"] == expected_actor
    assert config["model"] == (
        "explicit/assistant" if "--model" in options else DEFAULT_DEEPSEEK_MODEL
    )
    assert "secret" not in json.dumps(config)


@pytest.mark.parametrize(
    "reasoning", [None, {}, {"enabled": True}, {"enabled": 0}, {"effort": "low"}]
)
def test_deepseek_budget_rejects_reasoning_other_than_explicit_opt_out(reasoning):
    from agentrpg.budget import prepare_request

    unit = ModelPrice(DEFAULT_DEEPSEEK_MODEL, "0.00000026", "0.00000038", 0, 163840, "fixture")
    with pytest.raises(BudgetError):
        prepare_request(
            {
                "model": unit.model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
                "reasoning": reasoning,
            },
            {unit.model: unit},
        )


async def test_shop_cannot_fall_back_to_actor_key_when_personal_binding_is_missing():
    manifest = {"configuration": {"personal_agent_api": {}}}
    async with AsyncExitStack() as stack:
        with pytest.raises(PersonalAgentConfigError, match="API key locally"):
            await make_engine(
                stack,
                None,
                "must-not-open",
                "http://127.0.0.1:1",
                manifest,
                api_key="fixture-actor-secret",
            )


async def test_shop_buyer_seller_use_personal_key_but_actor_reviews_and_stimuli_do_not(
    pool, test_database_url, monkeypatch
):
    captured = []

    class UnusedModel(ScriptedModel):
        async def aclose(self):
            pass

    def factory(key, model, **kwargs):
        captured.append((key, model, kwargs["budget_role"]))
        return UnusedModel()

    monkeypatch.setattr("agentrpg.shop_runtime.create_model", factory)
    manifest = await Director(pool).create_run(shopping_model_scenario(1))
    manifest["configuration"] = {
        "rounds": 1,
        "model_roles": "all",
        "model": DEFAULT_DEEPSEEK_MODEL,
        "actor_model": "original/actor",
        "stimulus_model": "original/reviewer",
        "independent_shopping_stimulus_review": True,
        "require_shopping_task_coverage_review": True,
    }
    async with AsyncExitStack() as stack:
        await make_engine(
            stack,
            pool,
            test_database_url,
            "http://127.0.0.1:1",
            manifest,
            api_key="fixture-actor-secret",
            assistant_api_key="fixture-personal-secret",
            binding=object(),
        )
    assert len(captured) == 8
    for key, model, role in captured:
        if role in {"browser-shop-buyer", "browser-shop-seller"}:
            assert (key, model) == ("fixture-personal-secret", DEFAULT_DEEPSEEK_MODEL)
        else:
            assert key == "fixture-actor-secret"
            assert model == ("original/reviewer" if role.endswith("stimulus") else "original/actor")


async def test_deepseek_sdk_tool_call_uses_real_board_budget_and_completed_replay(
    app, provision, test_database_url, pool, tmp_path, monkeypatch
):
    cluster, identities = await provision(count=1)
    owner = identities[0]
    requests = []

    def supplied_provider(request):
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer fixture-personal-secret"
        assert payload["model"] == DEFAULT_DEEPSEEK_MODEL
        assert payload["reasoning"] == {"effort": "none"}
        assert payload["provider"]["allow_fallbacks"] is False
        requests.append(payload)
        message = {"role": "assistant", "content": "Saved the board post."}
        if len(requests) == 1:
            message.update(
                content=None,
                tool_calls=[
                    {
                        "id": "fixture-post-call",
                        "type": "function",
                        "function": {
                            "name": "post_thread",
                            "arguments": json.dumps(
                                {"title": "Local SDK check", "body": "Supplied provider response."}
                            ),
                        },
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": f"fixture-generation-{len(requests)}",
                "model": DEFAULT_DEEPSEEK_MODEL,
                "created": 1,
                "object": "chat.completion",
                "system_fingerprint": None,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "total_tokens": 60,
                    "cost": 0.00001,
                },
            },
        )

    monkeypatch.setattr(
        "agentrpg.agent_model.BudgetTransport",
        lambda binding, role: BudgetTransport(
            binding, role, transport=httpx.MockTransport(supplied_provider)
        ),
    )
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "0.20")
    unit = ModelPrice(DEFAULT_DEEPSEEK_MODEL, "0.00000026", "0.00000038", 0, 163840, "fixture")
    binding = BudgetBinding(ledger, campaign, {unit.model: unit})
    profile(tmp_path)
    api = load_personal_agent_api(legacy_env(), root=tmp_path)
    frozen = {"model": api.model, **api.manifest_fields()}
    session, task = uuid4(), uuid4()
    answers = []
    for attempt in range(2):
        if attempt:
            profile(tmp_path, PERSONAL_AGENT_MODEL="future/model")
        api = load_personal_agent_api(legacy_env(), root=tmp_path, configuration=frozen)
        model = create_model(
            api.api_key.get_secret_value(), api.model, budget=binding, budget_role="assistant"
        )
        try:
            async with (
                AgentRuntime(test_database_url) as runtime,
                board_client(app, owner["token"]) as client,
            ):
                agent = runtime.agent(
                    model=model,
                    board=BoardTools(client, str(cluster["id"])),
                    agent_id=UUID(str(owner["agent"]["id"])),
                    session_id=session,
                    instructions="Use the board to save the requested post.",
                )
                answers.append(await agent.run("Save one board post.", task_id=task))
        finally:
            await model.aclose()
    assert answers[0] == answers[1] and len(requests) == 2
    async with pool.connection() as conn:
        count = await (
            await conn.execute(
                "SELECT count(*) FROM messages WHERE cluster_id=%s", (cluster["id"],)
            )
        ).fetchone()
    assert count["count"] == 1
    totals = await ledger.summary(campaign)
    assert totals["attempts"] == 2 and totals["reported_usd"] == Decimal("0.00002")
    assert totals["limit_usd"] == Decimal("0.20")
