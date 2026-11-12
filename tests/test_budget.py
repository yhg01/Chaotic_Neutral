"""Real PostgreSQL spend fences plus provider HTTP contract checks."""

import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.budget import (
    CONTEXT_BOUND_MODELS,
    NO_REASONING_MODELS,
    NO_TEMPERATURE_MODELS,
    ROUTED_MODELS,
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


@pytest.mark.parametrize("model", list(CONTEXT_BOUND_MODELS))
def test_foreign_tokenizers_reserve_full_context_not_gpt_byte_estimate(model):
    unit = price(model=model, context_length=262144)
    request = body(model=model, messages=[{"role": "user", "content": "你好"}])
    _, bound, output = prepare_request(request, {model: unit})
    assert bound == 262144 and output == 100
    assert unit.snapshot()["input_bound_strategy"] == "full_declared_context"
    assert request["provider"]["allow_fallbacks"] is False
    assert unit.maximum(bound, output) >= unit.maximum(100000, output)
    with pytest.raises(BudgetError):
        prepare_request(body(model=model, reasoning={"enabled": True}), {model: unit})


@pytest.mark.parametrize("model,tokenizer", list(CONTEXT_BOUND_MODELS.items()))
async def test_comparison_metadata_requires_exact_tokenizer_and_tool_contract(model, tokenizer):
    entry = {
        "id": model,
        "context_length": 163840,
        "architecture": {
            "tokenizer": tokenizer,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["max_tokens", "tools", "tool_choice", "response_format"],
        "pricing": {"prompt": "0.0000003", "completion": "0.000001"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [entry]}))
    ) as client:
        fetched = await fetch_model_prices([model], client=client)
        assert fetched[model].context_length == 163840
        entry["architecture"]["tokenizer"] = "unknown"
        with pytest.raises(BudgetError):
            await fetch_model_prices([model], client=client)
        entry["architecture"]["tokenizer"] = tokenizer
        entry["supported_parameters"].remove("tools")
        with pytest.raises(BudgetError):
            await fetch_model_prices([model], client=client)


def _routed_price(model):
    return price(model=model, context_length=1_000_000)


@pytest.mark.parametrize("model", sorted(ROUTED_MODELS))
def test_routed_models_use_the_byte_bound_and_may_fall_back(model):
    prepared = body(model=model, temperature=0)
    _, bound, output = prepare_request(prepared, {model: _routed_price(model)})
    assert bound < 20_000
    assert output == 100
    assert prepared["provider"]["allow_fallbacks"] is True
    assert prepared["provider"]["require_parameters"] is True
    assert prepared["provider"]["max_price"]["prompt"] == pytest.approx(2.0)


def test_only_the_listed_models_lose_reasoning_or_temperature():
    assert NO_REASONING_MODELS == {"deepseek/deepseek-v4.1-flash", "qwen/qwen3.8-flash"}
    for model in sorted(ROUTED_MODELS):
        prepared = body(model=model, temperature=0.8)
        prepare_request(prepared, {model: _routed_price(model)})
        assert ("reasoning" in prepared) == (model in NO_REASONING_MODELS)
        if model in NO_REASONING_MODELS:
            assert prepared["reasoning"] == {"enabled": False}
        assert ("temperature" in prepared) == (model not in NO_TEMPERATURE_MODELS)
    fixed = body()
    prepare_request(fixed, {"openai/gpt-4.1": price()})
    assert "reasoning" not in fixed
    assert fixed["provider"]["allow_fallbacks"] is False


@pytest.mark.parametrize("value", [{"enabled": True}, {"effort": "high"}, {}])
def test_other_reasoning_options_still_fail_closed(value):
    model = "deepseek/deepseek-v4.1-flash"
    with pytest.raises(BudgetError):
        prepare_request(body(model=model, reasoning=value), {model: _routed_price(model)})
    with pytest.raises(BudgetError):
        prepare_request(body(reasoning=value), {"openai/gpt-4.1": price()})


def test_earlier_thinking_is_not_sent_back_and_other_extras_still_fail():
    model = "anthropic/claude-opus-5.5"
    messages = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "Hi",
            "reasoning": "private",
            "reasoning_details": [{"type": "reasoning.text", "text": "private"}],
        },
        {"role": "user", "content": "Again"},
    ]
    prepared = body(model=model, messages=messages)
    prepare_request(prepared, {model: _routed_price(model)})
    assert set(prepared["messages"][1]) == {"role", "content"}
    extra = [{"role": "user", "content": "Hello", "cache_control": {"type": "ephemeral"}}]
    with pytest.raises(BudgetError):
        prepare_request(body(model=model, messages=extra), {model: _routed_price(model)})


@pytest.mark.parametrize("model", sorted(ROUTED_MODELS))
async def test_routed_price_needs_the_exact_tokenizer_and_applies_its_factor(model):
    tokenizer, factor = ROUTED_MODELS[model]
    entry = {
        "id": model,
        "context_length": 1_000_000,
        "architecture": {
            "tokenizer": tokenizer,
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["max_tokens", "tools", "tool_choice", "response_format"],
        "pricing": {"prompt": "0.0000001", "completion": "0.000001"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [entry]}))
    ) as client:
        fetched = await fetch_model_prices([model], client=client)
        assert fetched[model].prompt == Decimal("0.0000001") * factor
        assert fetched[model].completion == Decimal("0.000001") * factor
        assert fetched[model].snapshot()["route_price_factor"] == str(factor)
        entry["architecture"]["tokenizer"] = "GPT"
        with pytest.raises(BudgetError):
            await fetch_model_prices([model], client=client)
        entry["architecture"]["tokenizer"] = tokenizer
        entry["supported_parameters"].remove("tools")
        with pytest.raises(BudgetError):
            await fetch_model_prices([model], client=client)


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


@pytest.mark.parametrize("model", list(CONTEXT_BOUND_MODELS))
async def test_foreign_full_context_reservation_blocks_unfunded_request(pool, model):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "0.01")
    unit = price(model=model)
    binding = BudgetBinding(ledger, campaign, {model: unit})
    transport = BudgetTransport(
        binding,
        "assistant",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Unfunded comparison request reached provider")
        ),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(BudgetExceeded):
            await client.post(
                "https://openrouter.ai/api/v1/chat/completions", json=body(model=model)
            )
    report = await ledger.summary(campaign)
    assert report["attempts"] == 0 and report["remaining_usd"] == Decimal("0.01")


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 429])
async def test_a_refused_request_costs_nothing_because_no_model_ran(pool, status):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})
    transport = BudgetTransport(
        binding,
        "assistant",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {}})),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", json=body())
    assert response.status_code == status
    report = await ledger.summary(campaign)
    assert report["attempts"] == 1
    assert report["committed_usd"] == 0 and report["uncertain_usd"] == 0
    assert report["recent_attempts"][0]["status"] == "reported"
    assert report["recent_attempts"][0]["reported_usd"] == 0


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        (httpx.ConnectError("refused"), False),
        (httpx.ConnectTimeout("timed out"), False),
        (httpx.ReadTimeout("no answer"), True),
        (httpx.RemoteProtocolError("body lost"), True),
    ],
)
async def test_only_a_request_that_never_left_is_released(pool, error, kept):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})

    def fail(request):
        raise error

    transport = BudgetTransport(binding, "assistant", transport=httpx.MockTransport(fail))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(type(error)):
            await client.post("https://openrouter.ai/api/v1/chat/completions", json=body())
    report = await ledger.summary(campaign)
    assert (report["uncertain_usd"] > 0) is kept
    assert (report["committed_usd"] > 0) is kept


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_keeps_its_reservation(pool, status):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1")
    binding = BudgetBinding(ledger, campaign, {"openai/gpt-4.1": price()})
    transport = BudgetTransport(
        binding,
        "assistant",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": {}})),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://openrouter.ai/api/v1/chat/completions", json=body())
    assert (await ledger.summary(campaign))["uncertain_usd"] > 0


async def test_a_refusal_waits_for_attempts_in_flight_before_it_stands(pool, monkeypatch):
    monkeypatch.setattr("agentrpg.budget.RESERVE_POLL_SECONDS", 0.01)
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "0.15")
    unit = price(prompt=Decimal("0.01"), completion=Decimal(0))
    running = await ledger.reserve(campaign, unit, 10, 1)
    waiting = asyncio.create_task(ledger.reserve(campaign, unit, 10, 1, wait_seconds=30))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    # The call in flight cost almost nothing, which frees the room.
    await ledger.finish(running, reported="0.0001")
    await asyncio.wait_for(waiting, 10)
    assert (await ledger.summary(campaign))["attempts"] == 2


async def test_a_refusal_stands_once_nothing_is_in_flight_or_the_wait_ends(pool, monkeypatch):
    monkeypatch.setattr("agentrpg.budget.RESERVE_POLL_SECONDS", 0.01)
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "0.1")
    unit = price(prompt=Decimal("0.01"), completion=Decimal(0))
    first = await ledger.reserve(campaign, unit, 10, 1)
    # No wait was asked for, so a direct caller gets its answer at once.
    with pytest.raises(BudgetExceeded):
        await ledger.reserve(campaign, unit, 10, 1)
    with pytest.raises(BudgetExceeded):
        await ledger.reserve(campaign, unit, 10, 1, wait_seconds=0.05)
    # An attempt with an unknown charge has finished. Waiting for it would never end.
    await ledger.finish(first, reason="unknown")
    with pytest.raises(BudgetExceeded):
        await asyncio.wait_for(ledger.reserve(campaign, unit, 10, 1, wait_seconds=30), 5)
    # A stopped process leaves a reservation that nothing will finish. It is not waited for.
    stale = uuid4()
    other = uuid4()
    await ledger.ensure_campaign(other, "0.1")
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO model_spend_attempts(id,campaign_id,model,reserved_usd,status,
            prompt_token_bound,output_token_bound,price_snapshot,created_at,role)
            VALUES(%s,%s,'openai/gpt-4.1',0.1,'reserved',1,1,'{}',
                   clock_timestamp()-interval '1 hour','model')""",
            (stale, other),
        )
    with pytest.raises(BudgetExceeded):
        await asyncio.wait_for(ledger.reserve(other, unit, 10, 1, wait_seconds=30), 5)


def test_a_long_history_of_short_messages_is_reserved_at_the_context_not_refused():
    model = "deepseek/deepseek-v4.1-flash"
    many = [{"role": "user", "content": "ok"}] * 1200
    _, bound, output = prepare_request(
        body(model=model, messages=many, max_tokens=4000), {model: _routed_price(model)}
    )
    assert bound + output == 1_000_000
    with pytest.raises(BudgetError):
        prepare_request(body(messages=many), {"openai/gpt-4.1": price(context_length=300_000)})


async def test_a_ceiling_only_goes_up_and_keeps_the_spend_history(pool):
    ledger = CampaignLedger(pool)
    campaign = uuid4()
    await ledger.ensure_campaign(campaign, Decimal("1"))
    await ledger.reserve(campaign, price(), 1000, 100)
    await ledger.raise_ceiling(campaign, Decimal("1"), Decimal("2"))
    summary = await ledger.summary(campaign)
    assert summary["limit_usd"] == Decimal("2") and summary["attempts"] == 1
    with pytest.raises(BudgetError):
        await ledger.raise_ceiling(campaign, Decimal("2"), Decimal("1"))
    with pytest.raises(BudgetError):
        await ledger.raise_ceiling(campaign, Decimal("1"), Decimal("3"))
    await ledger.ensure_campaign(campaign, Decimal("2"))
