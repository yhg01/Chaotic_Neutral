"""Structured calls enforce JSON mode through the real SDK and spend transport."""

import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from agentrpg.actor import ActorFidelityReview, generate_json
from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.budget import BudgetBinding, CampaignLedger, ModelPrice


@pytest.mark.parametrize(
    "content,validation_error",
    [
        ('{"accepted":true,"issues":[]}', None),
        ('{"issues":[]}', "missing"),
        ("The owner's reply follows the role.", "json_invalid"),
    ],
)
async def test_structured_sdk_sends_json_mode_and_keeps_cost_on_invalid_output(
    pool, content, validation_error
):
    ledger, campaign = CampaignLedger(pool), uuid4()
    await ledger.ensure_campaign(campaign, "1", name="Mocked structured provider test")
    price = ModelPrice(
        "openai/gpt-4.1",
        Decimal("0.000002"),
        Decimal("0.000008"),
        Decimal(0),
        1047576,
        "2026-10-28T00:00:00Z",
    )
    binding = BudgetBinding(ledger, campaign, {price.model: price})
    model = create_model(
        "test-provider-credential",
        model=price.model,
        budget=binding,
        budget_role="reviewer",
        max_tokens=128,
        max_retries=0,
    )
    requests = []

    async def provider(request):
        # This is the final serialized HTTP request, after SDK binding and the
        # budget transport. No real network or provider call occurs in this test.
        requests.append(json.loads(request.content))
        reserved = await ledger.summary(campaign)
        assert reserved["attempts"] == 1 and reserved["reserved_usd"] > 0
        return httpx.Response(
            200,
            json={
                "id": "gen-structured-test",
                "object": "chat.completion",
                "created": 1,
                "system_fingerprint": None,
                "model": price.model,
                "provider": "OpenAI",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "cost": 0.00036,
                },
            },
        )

    model._budget_clients[1]._transport.transport = httpx.MockTransport(provider)
    usage, candidates = UsageTracker(), []
    try:
        if validation_error is None:
            result = await generate_json(
                model,
                ActorFidelityReview,
                "Review the owner's reply.",
                {"reply": "Please ask my assistant to arrange transport."},
                usage=usage,
                on_candidate=candidates.append,
            )
            assert result.accepted is True and result.issues == ()
        else:
            with pytest.raises(ValidationError) as captured:
                await generate_json(
                    model,
                    ActorFidelityReview,
                    "Review the owner's reply.",
                    {"reply": "Please ask my assistant to arrange transport."},
                    usage=usage,
                    on_candidate=candidates.append,
                )
            assert captured.value.errors(include_input=False)[0]["type"] == validation_error
    finally:
        await model.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == 128
    assert request["stream"] is False
    assert request["provider"]["max_price"]["prompt"] == 2.0
    assert request["provider"]["allow_fallbacks"] is False
    assert "Return only a JSON object" in request["messages"][0]["content"]
    assert bool(candidates) is (validation_error != "json_invalid")
    assert usage.summary()["completed_requests"] == 1
    assert usage.summary()["failed_requests"] == 0
    assert usage.summary()["total_tokens"] == 120
    assert usage.summary()["reported_cost_usd"] == 0.00036
    final = await ledger.summary(campaign)
    assert final["attempts"] == 1
    assert final["reported_usd"] == Decimal("0.00036")
    assert final["reserved_usd"] == final["uncertain_usd"] == 0
    assert final["recent_attempts"][0]["role"] == "reviewer"
