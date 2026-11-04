"""The drama draw is seeded, measured and never exceeds its level."""

import hashlib

import pytest

from agentrpg.drama import FIRE_EARLIEST, FIRE_LATEST, LEVELS, draw, fire_s

KEYS = [f"action-{index}" for index in range(120_000)]


def measure(level, seed=7):
    draws = [draw(seed, key, level) for key in KEYS]
    fired = [item for item in draws if item.outcome != "none"]
    extreme = [item for item in fired if item.outcome == "extreme"]
    return draws, fired, extreme


@pytest.mark.parametrize(
    ("level", "rate", "share"),
    [("low", 0.03, 0.0), ("medium", 0.08, 0.10), ("high", 0.13, 0.20)],
)
def test_measured_rates_and_severity_splits_are_close(level, rate, share):
    draws, fired, extreme = measure(level)
    assert LEVELS[level].rate == rate
    assert LEVELS[level].extreme_share == share
    assert abs(len(fired) / len(draws) - rate) < 0.003
    assert abs(len(extreme) / len(fired) - share) < 0.015


def test_low_is_always_mild_and_off_never_fires():
    _, fired, extreme = measure("low")
    assert fired
    assert not extreme
    assert {item.outcome for item in fired} == {"mild"}
    draws, fired, _ = measure("off")
    assert not fired
    # The values exist even when nothing can happen, so the rate stays measurable.
    assert all(0 <= item.u < 1 and 0 <= item.v < 1 for item in draws[:100])


def test_values_follow_the_contract_hash_and_order_is_irrelevant():
    first = draw(11, "dan:errand:3", "high")
    digest = hashlib.sha256(b"11|dan:errand:3|perturb").digest()
    assert first.u == (int.from_bytes(digest[:8], "big") >> 11) / 2**53
    digest = hashlib.sha256(b"11|dan:errand:3|severity").digest()
    assert first.v == (int.from_bytes(digest[:8], "big") >> 11) / 2**53
    draw(11, "another", "high")
    assert draw(11, "dan:errand:3", "high") == first
    assert draw(12, "dan:errand:3", "high") != first
    # One action keeps the same u under each level, so higher levels only add events.
    assert draw(11, "dan:errand:3", "low").u == first.u


def test_a_higher_level_fires_whenever_a_lower_one_does():
    for key in KEYS[:20_000]:
        order = {"none": 0, "mild": 1, "extreme": 2}
        low, medium, high = (draw(3, key, level).outcome for level in ("low", "medium", "high"))
        assert (order[low] > 0) <= (order[medium] > 0) <= (order[high] > 0)


def test_fire_fraction_is_seeded_and_inside_the_action():
    fractions = [draw(5, key, "medium").fire_fraction for key in KEYS[:20_000]]
    assert min(fractions) >= FIRE_EARLIEST
    assert max(fractions) < FIRE_LATEST
    assert min(fractions) < 0.32
    assert max(fractions) > 0.68
    assert abs(sum(fractions) / len(fractions) - 0.5) < 0.005
    assert fire_s(1000, 3600, 0.5) == 2800


def test_an_unknown_level_is_refused():
    with pytest.raises(ValueError):
        draw(1, "key", "severe")
