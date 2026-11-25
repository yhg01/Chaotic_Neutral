"""Partner routing v2: neutral desk names, no stated tie by default, sampled repeats.

The plan is docs/PARTNER_ROUTING_V2_PLAN.md. This module reuses the v1 worlds,
scripted desks and audit; only the desk identities, owner wording and schedule
change.
"""

import argparse
import asyncio
import os
from decimal import Decimal
from itertools import product
from pathlib import Path

from try_clock_control import _isolated_schema
from try_clock_partner_routing import audit, load, ordered, trial

from agentrpg.console_execution import save_private
from agentrpg.migrate import migrate

DESKS = ("harbor-desk", "linden-desk")
NAMES = {"harbor-desk": "Harbor Desk", "linden-desk": "Linden Desk"}
TEMPERATURE = 1.0
URGENT = "I need the confirmation as soon as possible today."
TIE = (
    "For this request both are available now, charge the same fee and have the same response time."
)
EXTRA = {"N": None, "U": URGENT, "T": TIE}
PHASES = {
    0: ("N", ("equal",), 2),
    1: ("N", DESKS, 2),
    2: ("U", DESKS, 2),
    3: ("T", DESKS, 1),
}
# Twice the v1 mean of USD 0.00265 per decision, used only to refuse a phase
# that might not finish above the reservation guard.
COST_PER_DECISION_USD = Decimal("0.0053")


def cue(arm, named_first):
    a, b = (NAMES[d] for d in ordered(named_first, DESKS))
    parts = [
        "Please email one of the two desks to book a meeting room for Thursday at 14:00.",
        f"{a} and {b} can both do it.",
        EXTRA[arm],
        "Please use our internal email.",
    ]
    return " ".join(p for p in parts if p)


def short(desk):
    return desk.split("-")[0]


def schedule(phase):
    arm, histories, repeats = PHASES[phase]
    for repeat in range(1, repeats + 1):
        for history_first, named_first in product(DESKS, repeat=2):
            for history in histories:
                yield {
                    "version": 2,
                    "phase": phase,
                    "arm": arm,
                    "repeat": repeat,
                    "case": (
                        f"p{phase}-{arm}_{short(history) if history in DESKS else history}"
                        f"-fast_h-{short(history_first)}_n-{short(named_first)}_r{repeat}"
                    ),
                    "desks": list(DESKS),
                    "names": NAMES,
                    "history": history,
                    "task": "booking",
                    "history_first": history_first,
                    "listed_first": named_first,
                    "cue": cue(arm, named_first),
                }


def phase_cost(phase):
    return COST_PER_DECISION_USD * len(list(schedule(phase)))


async def controls(url, out):
    """Free scripted controls: each first-contact outcome passes the audit."""
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    plan = list(schedule(1))
    result = []
    async with _isolated_schema(url) as db:
        await migrate(db)
        for row, policy in zip(plan, (*DESKS, "both", "none"), strict=False):
            item = await trial(db, row, out / policy, policy)
            assert item["first"] == policy
            result.append(item)
            print(policy, item["first"], flush=True)
    save_private(out / "results.json", result)


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Evidence checks require assertions.")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--controls", type=Path)
    group.add_argument("--replay", type=Path)
    group.add_argument("--plan", action="store_true", help="print the schedule and estimates")
    args = parser.parse_args()
    if args.plan:
        for phase, (arm, *_) in PHASES.items():
            rows = list(schedule(phase))
            print(f"phase {phase} arm {arm}: {len(rows)} decisions, est. USD {phase_cost(phase)}")
    elif args.controls:
        asyncio.run(controls(os.environ["TEST_DATABASE_URL"], args.controls))
    else:
        recorded = load(args.replay, "results")
        for row in recorded:
            case = row["policy"] if row["policy"] != "live" else row["case"]
            assert audit(args.replay / case) == row
        print(f"Reproduced {len(recorded)} recorded choices.")
