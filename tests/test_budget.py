"""Real PostgreSQL spend fences plus provider HTTP contract checks."""

import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.budget import (
    BudgetBinding,
    BudgetError,
    BudgetExceeded,
    BudgetTransport,
    CampaignLedger,
    ModelPrice,
    amount,
    budget_context,
    fetch_model_prices,
    prepare_request,
)


def price(**overrides):
    values = dict(
        model="openai/gpt-4.1",
        prompt=Decimal("0.000002"),
        completion=Decimal("0.000008"),
        request=Decimal(0),
        context_length=1047576,
        fetched_at="2026-10-28T00:00:00Z",
    )
    return ModelPrice(**(values | overrides))


def body(**overrides):
    return {
        "model": "openai/gpt-4.1",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
        "stream": False,
    } | overrides


@pytest.mark.parametrize("value", [True, -1, "NaN", "Infinity", "-Infinity", None, [], "bad"])
def test_costs_fail_closed(value):
    with pytest.raises(BudgetError):
        amount(value)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "unknown/model"},
        {"model": "openai/gpt-4.1:online"},
        {"plugins": [{"id": "web"}]},
        {"stream": True},
        {"max_tokens": None},
        {"max_tokens": True},
        {"max_tokens": 0},
        {"n": 2},
        {"service_tier": "priority"},
        {"tools": [{"type": "web_search"}]},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}]},
    ],
)
def test_unsupported_requests_fail_before_spend(change):
    with pytest.raises(BudgetError):
        prepare_request(body(**change), {"openai/gpt-4.1": price()})


def test_price_ceiling_and_serialized_tool_bound():
    request = body(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "local_search",
                    "description": "Find " * 1000,
                    "parameters": {"type": "object"},
                },
            }
        ]
    )
    raw = len(json.dumps(request, ensure_ascii=False).encode())
    _, bound, output = prepare_request(request, {"openai/gpt-4.1": price()})
    assert bound > raw and output == 100
    assert request["provider"] == {
        "require_parameters": True,
        "allow_fallbacks": False,
        "max_price": {"prompt": 2.0, "completion": 8.0, "request": 0.0, "image": 0},
    }
    assert request["usage"] == {"include": True}


async def test_price_fetch_uses_current_metadata_and_rejects_unknown():
    entry = {
        "id": "openai/gpt-4.1",
        "pricing": {"prompt": "0.000002", "completion": "0.000008"},
        "architecture": {"tokenizer": "GPT", "output_modalities": ["text"]},
        "supported_parameters": ["max_tokens"],
        "context_length": 1047576,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [entry]}))
    ) as client:
        result = await fetch_model_prices(["openai/gpt-4.1"], client=client)
        assert result["openai/gpt-4.1"].prompt == Decimal("0.000002")
        entry["pricing"]["prompt"] = "NaN"
        with pytest.raises(BudgetError):
            await fetch_model_prices(["openai/gpt-4.1"], client=client)


async def test_parallel_reservations_are_atomic_and_survive_restart(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1.00")
    unit = price(prompt=Decimal("0.01"), completion=Decimal(0))
    results = await asyncio.gather(
        *(ledger.reserve(campaign, unit, 10, 1) for _ in range(30)), return_exceptions=True
    )
    assert sum(not isinstance(result, Exception) for result in results) == 10
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 20
    reopened = CampaignLedger(pool)
    summary = await reopened.ensure_campaign(campaign, "1.00")
    assert summary["reserved_usd"] == Decimal("1")
    assert summary["remaining_usd"] == 0
    with pytest.raises(BudgetError, match="differs"):
        await reopened.ensure_campaign(campaign, "2.00")


async def test_exact_report_releases_only_unused_reserve_and_keeps_uncertain(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    unit = price(prompt=Decimal("0.01"), completion=Decimal(0))
    first = await ledger.reserve(campaign, unit, 10, 1)
    second = await ledger.reserve(campaign, unit, 10, 1)
    await ledger.finish(first, reported="0.031", generation_id="gen-test")
    await ledger.finish(second, reason="Unknown charge")
    await ledger.finish(first, reported="0.0001")  # Already final; cannot lower history.
    report = await ledger.summary(campaign)
    assert report["reported_usd"] == Decimal("0.031")
    assert report["uncertain_usd"] == Decimal("0.1")
    assert report["remaining_usd"] == Decimal("0.869")


async def test_cost_contract_breach_halts_campaign(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    attempt = await ledger.reserve(campaign, price(), 10, 1)
    with pytest.raises(BudgetError, match="halted"):
        await ledger.finish(attempt, reported="0.5")
    with pytest.raises(BudgetExceeded):
        await ledger.reserve(campaign, price(), 10, 1)
    assert (await ledger.summary(campaign))["halted"] is True


async def test_provider_decimal_precision_is_preserved(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1.00000000000001")
    attempt = await ledger.reserve(campaign, price(), 10, 1)
    exact = Decimal("0.00000000000001234567")
    await ledger.finish(attempt, reported=exact)
    summary = await ledger.summary(campaign)
    assert summary["reported_usd"] == exact
    assert summary["limit_usd"] == Decimal("1.00000000000001")
    assert summary["remaining_usd"] == Decimal("1.00000000000001") - exact


@pytest.mark.parametrize("outcome", ["success", "missing", "failure", "cancel", "timeout"])
async def test_transport_reserves_before_call_and_retains_unknown_charge(pool, outcome):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})
    calls = []

    async def provider(request):
        report = await ledger.summary(campaign)
        assert report["reserved_usd"] > 0 and report["attempts"] == 1
        calls.append(json.loads(request.content))
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "timeout":
            raise httpx.ReadTimeout("secret provider response")
        if outcome == "failure":
            return httpx.Response(503, json={"error": "provider failure"})
        data = {"id": "gen-test", "usage": {}}
        if outcome == "success":
            data["usage"]["cost"] = 0.00123
        return httpx.Response(200, json=data)

    transport = BudgetTransport(binding, "assistant", transport=httpx.MockTransport(provider))
    async with httpx.AsyncClient(transport=transport) as client:
        with budget_context(owner_id="owner-1", role="actor"):
            try:
                await client.post("https://openrouter.ai/api/v1/chat/completions", json=body())
            except (asyncio.CancelledError, httpx.ReadTimeout):
                pass
    report = await ledger.summary(campaign)
    assert len(calls) == 1
    assert calls[0]["provider"]["max_price"]["prompt"] == 2
    assert report["recent_attempts"][0]["role"] == "assistant"
    assert report["recent_attempts"][0]["owner_id"] == "owner-1"
    if outcome == "success":
        assert report["reported_usd"] == Decimal("0.00123")
        assert report["uncertain_usd"] == 0
    else:
        assert report["uncertain_usd"] > 0
    assert "secret" not in json.dumps(report, default=str)


async def test_factory_counts_each_actual_retry_and_stops_at_cap(pool, monkeypatch):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})
    model = create_model(
        "test-secret",
        model="openai/gpt-4.1",
        budget=binding,
        budget_role="assistant",
        max_retries=2,
    )
    calls = []

    def response(request):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "test-secret", "code": 503}})

    model._budget_clients[1]._transport.transport = httpx.MockTransport(response)
    try:
        with pytest.raises(Exception, match="HTTP 503"):
            await model.ainvoke("Hello")
    finally:
        await model.aclose()
    report = await ledger.summary(campaign)
    assert len(calls) == report["attempts"] == 3
    assert report["uncertain_usd"] > 0
    assert "test-secret" not in json.dumps(report, default=str)


async def test_empty_budget_never_sends_http(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "0.00001")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})
    transport = BudgetTransport(
        binding,
        "actor",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Unfunded request reached the provider")
        ),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BudgetExceeded):
            await client.post("https://openrouter.ai/api/v1/chat/completions", json=body())
    assert (await ledger.summary(campaign))["attempts"] == 0


async def test_factory_preserves_budget_exhaustion_without_http(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "0.00001")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})
    model = create_model("test-secret", model="openai/gpt-4.1", budget=binding)
    model._budget_clients[1]._transport.transport = httpx.MockTransport(
        lambda request: pytest.fail("Unfunded request reached the provider")
    )
    try:
        with pytest.raises(BudgetExceeded):
            await model.ainvoke("Hello")
    finally:
        await model.aclose()
    assert (await ledger.summary(campaign))["attempts"] == 0


async def test_factory_success_keeps_existing_usage_callbacks_and_exact_cost(pool):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})
    model = create_model("test-secret", model="openai/gpt-4.1", budget=binding)
    model._budget_clients[1]._transport.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "id": "gen-budget-test",
                "object": "chat.completion",
                "created": 1,
                "system_fingerprint": None,
                "model": "openai/gpt-4.1",
                "provider": "OpenAI",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "total_tokens": 11,
                    "cost": 0.000028,
                },
            },
        )
    )
    usage = UsageTracker()
    try:
        result = await model.ainvoke("Hello", config={"callbacks": [usage]})
    finally:
        await model.aclose()
    assert result.content == "Done"
    assert usage.summary()["completed_requests"] == 1
    assert usage.summary()["reported_cost_usd"] == 0.000028
    assert (await ledger.summary(campaign))["reported_usd"] == Decimal("0.000028")
