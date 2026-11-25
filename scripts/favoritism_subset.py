"""Frozen two-stratum selection; preserve the ordered IDs for all judge reports."""

import json
import random
from pathlib import Path

SEED = 25112026
FROZEN = Path(__file__).resolve().parents[1] / "docs/favoritism-subset.json"
BATCHES = (
    "partner-routing-live-01",
    "partner-routing-roster-01",
    "partner-routing-history-check-01",
)


def select(keys):
    rng = random.Random(SEED)
    routing = {name: [] for name in BATCHES}
    sealed = []
    for cid, key in sorted(keys.items()):
        sources = {Path(v["path"]).parent.name for v in key["mapping"].values()}
        if len(sources) != 1:
            raise ValueError(f"Mixed source pair {cid}")
        batch = sources.pop()
        if batch.startswith("sealed-"):
            sealed.append(cid)
        else:
            routing[batch].append(cid)
    if len(sealed) != 69 or [len(routing[b]) for b in BATCHES] != [12, 12, 4]:
        raise ValueError("Expected 69 sealed pairs and all 28 routing pairs.")
    rng.shuffle(sealed)
    calibration = [cid for b in BATCHES for cid in rng.sample(routing[b], 2)]
    return {"seed": SEED, "sealed_bid": sealed, "routing_v1": calibration}


def registered(study):
    fixed = json.loads(FROZEN.read_bytes())
    if select(json.loads((study / "private-key.json").read_bytes())) != fixed:
        raise ValueError("Local cases differ from the pre-registered subset.")
    return fixed


def selected_ids(study, requested=None):
    fixed = registered(study)
    ids = fixed["sealed_bid"] + fixed["routing_v1"]
    if requested is not None and list(requested) != ids:
        raise ValueError("Agreement requires the complete ordered pre-registered subset.")
    return ids
