"""Compare two saved judge outputs for exactly the same blind packets, offline."""

import argparse
from collections import Counter
from pathlib import Path

from favoritism_judge import DIMS, check_answers, load
from favoritism_subset import registered, selected_ids


def agreement(study, first, second, ids=None):
    selected_ids(study, ids)
    strata = registered(study)
    meta = load(study, "packet-metadata")
    output = {}
    for name, group, tag in (
        ("favoritism_estimand", "sealed_bid", None),
        ("judge_calibration_only", "routing_v1", "stated_tie_and_labels"),
    ):
        cases = strata[group]
        if any(meta[c]["confound"] != tag for c in cases):
            raise ValueError("Packet confound does not match the registered stratum.")
        output[name] = _agreement(study, first, second, cases)
    return output


def _agreement(study, first, second, ids):
    if not __debug__:
        raise RuntimeError("Evidence checks require assertions.")
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Select distinct matched case IDs.")
    match, comparable, invalid = Counter(), Counter(), Counter()
    for cid in ids:
        outputs = []
        for name, folder in (("first", first), ("second", second)):
            labels = {}
            try:
                data = load(folder, cid)
                if data["case_id"] != cid:
                    raise ValueError("Wrong case ID.")
                for tag in ("X", "Y"):
                    for dim in DIMS:
                        labels[tag, dim] = data["episodes"][tag][dim]["label"]
                labels["pair", "favoritism"] = data["favoritism"]["label"]
                check_answers(study, folder, [cid])
            except (AssertionError, KeyError, TypeError, ValueError, FileNotFoundError):
                invalid[name] += 1
            outputs.append(labels)
        a, b = outputs
        for key in a.keys() & b.keys():
            dim = key[1]
            allowed = (
                {"owner_message", "peer_message", "timer", "unclear"}
                if dim == "agency"
                else {"observed", "none_observed", "unclear"}
            )
            if all(isinstance(label, str) and label in allowed for label in (a[key], b[key])):
                comparable[dim] += 1
                match[dim] += a[key] == b[key]
    complete = all(comparable[d] == 2 * len(ids) for d in DIMS) and comparable["favoritism"] == len(
        ids
    )
    return {
        "complete": complete,
        "matching_labels": dict(match) if complete else None,
        "comparable_labels": dict(comparable),
        "scheduled_episode_denominator": 2 * len(ids),
        "scheduled_pair_denominator": len(ids),
        "invalid_or_key_disagreeing_packets": dict(invalid),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("study", type=Path)
    p.add_argument("first", type=Path)
    p.add_argument("second", type=Path)
    p.add_argument("cases", nargs="*")
    a = p.parse_args()
    print(agreement(a.study, a.first, a.second, a.cases or None))
