"""The author and critique commands share the same durable campaign."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from agentrpg.budget import CampaignLedger, ModelPrice
from agentrpg.scenario_schema import vehicle_scenario

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/author_scenario.py"
SPEC = importlib.util.spec_from_file_location("budget_author_cli", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.mark.parametrize(
    "options",
    [
        ["--budget-usd", "0"],
        ["--budget-usd", "-1"],
        ["--budget-usd", "NaN"],
        ["--budget-usd", "Infinity"],
        ["--campaign", str(uuid4())],
    ],
)
def test_author_budget_arguments_fail_before_model_calls(options):
    with pytest.raises(SystemExit):
        cli.parse_args(["--output", "scenario.json", *options])


async def test_author_and_critic_share_persistent_campaign(
    tmp_path, monkeypatch, test_database_url, pool, capsys
):
    campaign = uuid4()
    roles = []
    model_price = ModelPrice(
        "openai/gpt-4.1",
        Decimal("0.000002"),
        Decimal("0.000008"),
        Decimal(0),
        1047576,
        "2026-10-28T00:00:00Z",
    )

    async def prices(models):
        return {"openai/gpt-4.1": model_price}

    class Model:
        def __init__(self, binding, role):
            self.binding, self.role = binding, role

        async def ainvoke(self, messages, config=None):
            roles.append(self.role)
            attempt = await self.binding.ledger.reserve(
                self.binding.campaign_id, model_price, 100, 10, role=self.role
            )
            await self.binding.ledger.finish(attempt, reported="0.00001")
            value = (
                vehicle_scenario().model_dump(mode="json")
                if self.role == "author"
                else {"accepted": True, "issues": []}
            )
            return AIMessage(content=json.dumps(value))

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "fetch_model_prices", prices)
    monkeypatch.setattr(
        cli,
        "create_model",
        lambda key, model, **kwargs: Model(kwargs["budget"], kwargs["budget_role"]),
    )
    args = cli.parse_args(
        [
            "--brief",
            "A test conflict",
            "--output",
            "scenario.json",
            "--model",
            "openai/gpt-4.1",
            "--campaign",
            str(campaign),
            "--budget-usd",
            "30",
        ]
    )
    report = await cli.execute(
        args,
        root=tmp_path,
        env={
            "OPENROUTER_API_KEY": "author-test-secret",
            "DATABASE_URL": test_database_url,
        },
    )
    assert roles == ["author", "critic"]
    assert report["campaign_budget"]["reported_usd"] == Decimal("0.00002")
    assert (await CampaignLedger(pool).summary(campaign))["attempts"] == 2
    saved = json.loads((tmp_path / "scenario.author-report.json").read_text())
    assert saved["campaign_budget"]["id"] == str(campaign)
    assert "author-test-secret" not in capsys.readouterr().out


async def test_budgeted_author_requires_database_before_model_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "create_model", lambda *args, **kwargs: pytest.fail("Model created"))
    args = cli.parse_args(["--brief", "Test", "--output", "scene.json", "--budget-usd", "30"])
    with pytest.raises(cli.AuthorCommandError, match="DATABASE_URL"):
        await cli.execute(args, root=tmp_path, env={"OPENROUTER_API_KEY": "test-secret"})
    assert not (tmp_path / "scene.json").exists()
