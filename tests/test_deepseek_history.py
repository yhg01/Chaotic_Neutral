"""Returned reasoning text is bounded history, never another billing option."""

import copy

import pytest

from agentrpg.budget import BudgetError, ModelPrice, prepare_request


def request():
    return {
        "model": "deepseek/deepseek-v3.2",
        "max_tokens": 600,
        "reasoning": {"effort": "none"},
        "messages": [
            {"role": "user", "content": "Use the board."},
            {
                "role": "assistant",
                "content": "",
                "reasoning": "Supplied historical text.",
                "reasoning_details": [
                    {
                        "type": "reasoning.text",
                        "format": "unknown",
                        "index": 0,
                        "text": "Supplied historical text.",
                    }
                ],
            },
        ],
    }


def prices():
    unit = ModelPrice("deepseek/deepseek-v3.2", "0.00000026", "0.00000038", 0, 163840, "fixture")
    return {unit.model: unit}


def test_saved_reasoning_round_trip_keeps_full_context_reservation():
    body = request()
    messages = copy.deepcopy(body["messages"])
    _, bound, output = prepare_request(body, prices())
    assert (bound, output) == (163840, 600)
    assert body["messages"] == messages
    assert body["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize(
    "details",
    [
        "not a list",
        [{"type": "image_url", "text": "x"}],
        [{"type": "reasoning.text", "text": {"url": "x"}}],
        [{"type": "reasoning.text", "text": "x", "index": True}],
        [{"type": "reasoning.text", "text": "x", "unknown_billing_option": True}],
    ],
)
def test_media_unknown_options_and_invalid_fields_remain_rejected(details):
    body = request()
    body["messages"][1]["reasoning_details"] = details
    with pytest.raises(BudgetError):
        prepare_request(body, prices())


def test_user_message_cannot_supply_reasoning_fields():
    body = request()
    body["messages"][1]["role"] = "user"
    with pytest.raises(BudgetError):
        prepare_request(body, prices())
