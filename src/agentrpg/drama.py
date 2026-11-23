"""Seeded chance of something going wrong during an owner's action (contract rule R10).

The draw is a pure function of the run seed, the action key and the drama level, so a
rerun repeats it exactly. Code decides whether something happens and how serious it is.
The model only writes what it was.
"""

import hashlib
from typing import Literal, NamedTuple

Outcome = Literal["none", "mild", "extreme"]


class Level(NamedTuple):
    rate: float
    extreme_share: float


LEVELS: dict[str, Level] = {
    "off": Level(rate=0.0, extreme_share=0.0),
    "low": Level(rate=0.03, extreme_share=0.0),
    "medium": Level(rate=0.08, extreme_share=0.10),
    "high": Level(rate=0.13, extreme_share=0.20),
}
FIRE_EARLIEST = 0.30
FIRE_LATEST = 0.70


class Draw(NamedTuple):
    u: float
    v: float
    outcome: Outcome
    fire_fraction: float


def _uniform(seed, action_key: str, purpose: str) -> float:
    """A uniform value in [0, 1). 53 bits divide exactly, so the result can never round to 1."""
    digest = hashlib.sha256(f"{seed}|{action_key}|{purpose}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / 2**53


def draw(seed, action_key: str, level: str) -> Draw:
    """Draw for one action. u and v are returned even when nothing happens, for measurement."""
    if level not in LEVELS:
        raise ValueError(f"Unknown drama level: {level!r}.")
    rate, extreme_share = LEVELS[level]
    u = _uniform(seed, action_key, "perturb")
    v = _uniform(seed, action_key, "severity")
    outcome: Outcome = "none"
    if u < rate:
        outcome = "extreme" if v < extreme_share else "mild"
    fraction = FIRE_EARLIEST + (FIRE_LATEST - FIRE_EARLIEST) * _uniform(seed, action_key, "fire")
    return Draw(u=u, v=v, outcome=outcome, fire_fraction=fraction)


def fire_s(start_s: int, duration_s: int, fire_fraction: float) -> int:
    """The second at which a perturbation lands inside its action."""
    return start_s + int(duration_s * fire_fraction)
