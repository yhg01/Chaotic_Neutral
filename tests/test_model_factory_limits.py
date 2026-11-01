"""A larger author output allowance does not change personal-agent defaults."""

import pytest

from agentrpg.agent_model import create_model


@pytest.mark.parametrize("limit", [4095, 16385, True, 8192.0])
def test_invalid_explicit_output_limits_rejected(limit):
    with pytest.raises(ValueError):
        create_model("test-key", max_output_limit=limit)


def test_larger_output_still_requires_explicit_bounded_allowance():
    with pytest.raises(ValueError):
        create_model("test-key", max_tokens=4097)
    with pytest.raises(ValueError):
        create_model("test-key", max_tokens=8193, max_output_limit=8192)


async def test_explicit_author_allowance_retains_other_factory_limits():
    model = create_model("test-key", max_tokens=14080, max_output_limit=16384)
    try:
        assert model.max_tokens == 14080
        assert model.max_retries == 2
        assert model.request_timeout == 45000
    finally:
        await model.aclose()
