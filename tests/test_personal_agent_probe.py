"""Free end-to-end rehearsal of the exact bounded personal Agent probe."""

import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agentrpg.budget import BudgetError, BudgetTransport, ModelPrice
from agentrpg.budget_recovery import restore_opening_balance

SOURCE = Path(__file__).resolve().parents[1] / "scripts/probe_personal_agent.py"


@pytest.mark.parametrize("repair", [False, True])
async def test_probe_and_process_recreation_keep_three_calls_one_post_and_opening_costs(
    pool, settings, tmp_path, monkeypatch, repair
):
    spec = importlib.util.spec_from_file_location("personal_agent_probe", SOURCE)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.setattr(script, "Settings", lambda: settings)
    monkeypatch.setenv("ADMIN_TOKEN", settings.admin_token.get_secret_value())
    (tmp_path / "src/agentrpg").mkdir(parents=True)
    (tmp_path / "src/agentrpg/example.py").write_text("# Fixed rehearsal source\n")
    (tmp_path / ".env").write_text("ADMIN_TOKEN=" + settings.admin_token.get_secret_value() + "\n")
    (tmp_path / ".env.personal-agent.local").write_text(
        "PERSONAL_AGENT_API_ENABLED=true\nPERSONAL_AGENT_PROVIDER=openrouter\n"
        "PERSONAL_AGENT_BASE_URL=https://openrouter.ai/api/v1\n"
        "PERSONAL_AGENT_MODEL=deepseek/deepseek-v3.2\nPERSONAL_AGENT_API_KEY=fixture-key\n"
    )
    campaign = uuid4()
    data = json.dumps(
        {
            "campaign_budget": {
                "id": str(campaign),
                "limit_usd": "5.5",
                "attempts": 1467,
                "reported_usd": "4.89333461",
                "uncertain_usd": "0.4079752",
                "reserved_usd": "0",
                "committed_usd": "5.30130981",
                "remaining_usd": "0.19869019",
            }
        }
    ).encode()
    await restore_opening_balance(
        pool,
        data,
        expected_sha256=hashlib.sha256(data).hexdigest(),
        campaign_id=campaign,
        approved_limit="5.5",
    )
    unit = ModelPrice("deepseek/deepseek-v3.2", "0.00000026", "0.00000038", 0, 163840, "fixture")

    async def prices(*args):
        return {unit.model: unit}

    monkeypatch.setattr(script, "fetch_model_prices", prices)
    folder = tmp_path / ".local/probe"
    seen = []

    def supplied_provider(request):
        payload = json.loads(request.content)
        assert payload["reasoning"] == {"effort": "none"}
        assert request.headers["authorization"] == "Bearer fixture-key"
        seen.append(payload)
        manifest = json.loads((folder / "credentials.json").read_text())
        reference = manifest["reference"]
        message = {
            "role": "assistant",
            "content": None,
            "reasoning": "Supplied historical text.",
            "reasoning_details": [
                {
                    "type": "reasoning.text",
                    "format": "unknown",
                    "index": 0,
                    "text": "Supplied historical text.",
                }
            ],
        }
        if len(seen) == 1:
            name = "post_thread"
            args = {
                "title": f"DeepSeek check {reference}",
                "body": f"Verification code: {reference}",
            }
        else:
            post = next(
                json.loads(m["content"])["data"]
                for m in payload["messages"]
                if m["role"] == "tool" and "id" in json.loads(m["content"]).get("data", {})
            )
            name, args = "read_thread", {"thread_id": post["id"]}
        if len(seen) <= 2:
            message["tool_calls"] = [
                {
                    "id": f"fixture-{len(seen)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ]
        else:
            message["content"] = f"Saved and read thread {post['id']}; code {reference}."
        return httpx.Response(
            200,
            json={
                "id": f"fixture-generation-{len(seen)}",
                "model": unit.model,
                "created": 1,
                "object": "chat.completion",
                "system_fingerprint": None,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(seen) <= 2 else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
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
    import agentrpg.budget as budget

    original = budget.prepare_request
    preflights = 0

    def preflight(body, prices):
        nonlocal preflights
        preflights += 1
        if repair and preflights == 2:
            raise BudgetError("Supplied unsent preflight failure.")
        return original(body, prices)

    monkeypatch.setattr(budget, "prepare_request", preflight)
    first = await script.probe(campaign, folder)
    if repair:
        assert not first["passed"] and len(seen) == 1
        previous = json.loads((folder / "credentials.json").read_text())
        (tmp_path / "src/agentrpg/example.py").write_text("# Repaired rehearsal source\n")
        with pytest.raises(ValueError, match="explicit recorded repair"):
            await script.probe(campaign, folder)
        first = await script.probe(campaign, folder, accept_repair=True)
        current = json.loads((folder / "credentials.json").read_text())
        for field in ("task_id", "session_id", "cluster_id", "agent_id", "campaign_id"):
            assert current[field] == previous[field]
        assert current["configuration"]["max_model_calls"] == 4
        assert current["source_repairs"][-1]["unsent_preflight_attempts"] == 1
    assert first["passed"] and len(seen) == 3
    second = await script.probe(campaign, folder)
    assert second["passed"] and len(seen) == 3
    assert first["posts"] == second["posts"]
    assert str(second["campaign_after"]["uncertain_usd"]) == "0.4079752"
    assert str(second["campaign_after"]["remaining_usd"]) == "0.19866019"
