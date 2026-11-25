"""Replay routing outcomes and reconcile the pilot to its original campaign ledger."""

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from try_clock_partner_routing import (
    DESKS,
    TASKS,
    USE_HISTORY,
    audit,
    desks_of,
    history_check_schedule,
    load,
    schedule,
)

from agentrpg.console_execution import save_private


def check_provider(folder, evidence, charges):
    desks = desks_of(evidence)
    visible = load(folder, "decision-input")
    batches = load(folder, "decision-batches")["batches"]
    paths = sorted(folder.glob("provider-*.json"))
    assert len(paths) == len(batches) == len(charges) > 0
    by_generation = {entry["generation_id"]: entry for entry in charges}
    ids = set()
    saw_contacts = False
    for path, batch in zip(paths, batches, strict=True):
        response = json.loads(path.read_text())
        assert response["http_status"] == 200 and response["id"] not in ids
        ids.add(response["id"])
        assert by_generation[response["id"]]["status"] == "reported"
        wire = response["choices"][0]["message"].get("tool_calls", [])
        assert [(c["id"], c["name"], c["args"]) for c in batch] == [
            (c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in wire
        ]
        for m in response["request_messages"]:
            if m["role"] == "tool":
                data = json.loads(m["content"]).get("data")
                if isinstance(data, dict) and "contacts" in data:
                    roster = [v["owner_id"] for v in data["contacts"] if v["owner_id"] in desks]
                    assert len(roster) == 2 and roster[0] == evidence.get(
                        "directory_first", desks[0]
                    )
                    saw_contacts = True
        if path == paths[0]:
            request = response["request_messages"]
            assert any(
                m["role"] == "user" and visible["assignment"] in m["content"] for m in request
            )
            bodies = []
            for message in request:
                if message["role"] == "tool":
                    data = json.loads(message["content"]).get("data")
                    if isinstance(data, dict) and data.get("recipient_owner_id") == "lead":
                        bodies.append((data.get("id"), data.get("body")))
            assert bodies == [(m["id"], m["body"]) for m in visible["mail"]]
    assert ids == set(by_generation)
    trace = load(folder, "trajectory")
    assert trace["provider_attempts"] == 0  # Paid attempts reside only in the shared ledger.
    for dest in evidence["outgoing"]:
        event = next(e for e in trace["events"] if e["id"] == dest["send_event"])
        assert event["payload"]["call_id"] in {c["id"] for c in batches[dest["batch"]]}
    return saw_contacts


def searched_history(folder):
    """True when the assistant searched the booking correspondence before its last batch."""
    batches = load(folder, "decision-batches")["batches"]
    for n, batch in enumerate(batches[:-1], start=1):
        if any(c["name"] == "list_mail" and c["args"].get("query") == "Booking R-" for c in batch):
            request = load(folder, f"provider-{n + 1:02}")["request_messages"]
            outcome = json.loads(request[-1]["content"])
            assert outcome["ok"] and len(outcome["data"]["items"]) == 20
            return True
    return False


def report(root):
    protocol, budget = load(root, "protocol"), load(root, "budget")
    rows = load(root, "results")
    arm = protocol["schedule"][0]
    plan = list(
        history_check_schedule()
        if arm.get("use_history")
        else schedule(arm.get("directory_first", DESKS[0]))
    )
    assert protocol["schedule"] == plan and len(rows) == len(plan)
    ledger = budget["attempts"]
    all_charges, lookups, booking_searches = [], [], []
    for condition, row in zip(plan, rows, strict=True):
        assert all(row[k] == v for k, v in condition.items())
        folder = root / row["case"]
        assert (
            audit(folder) == row
            and row["scenario_hash"] == protocol["scenario_hashes"][row["case"]]
        )
        charges = [c for c in ledger if c["role"] == f"{root.name}-{row['case']}"]
        if check_provider(folder, row, charges):
            lookups.append(row["case"])
        if searched_history(folder):
            booking_searches.append(row["case"])
        all_charges.extend(c["id"] for c in charges)
    assert len(set(all_charges)) == len(all_charges) == len(ledger)
    before, after = budget["before"], budget["after"]
    spent = sum(Decimal(c["reported_usd"]) for c in ledger)
    assert Decimal(before["remaining_usd"]) - Decimal(after["remaining_usd"]) == spent
    assert before["uncertain_usd"] == after["uncertain_usd"] and Decimal(after["reserved_usd"]) == 0

    lookup = {(r["block"], r["task"], r["history"]): r["first"] for r in rows}
    cells = {}
    for task in TASKS:
        subset = [r for r in rows if r["task"] == task]
        asymmetric = [r for r in subset if r["history"] in DESKS]
        paired = [
            (lookup[i, task, "desk-a"], lookup[i, task, "desk-b"])
            for i in sorted({r["block"] for r in subset})
        ]
        cells[task] = {
            "by_history": {
                h: dict(Counter(r["first"] for r in subset if r["history"] == h))
                for h in (*DESKS, "equal")
            },
            "fast_chosen": sum(r["history"] == r["first"] for r in asymmetric),
            "changed_with_fast_desk": sum(pair == DESKS for pair in paired),
            "changed_against_fast_desk": sum(pair == DESKS[::-1] for pair in paired),
            "same_first_desk_after_swap": sum(a == b and a in DESKS for a, b in paired),
            "listed_first_chosen": sum(r["listed_first"] == r["first"] for r in subset),
            "last_historical_contact_chosen": sum(
                r["first"] in DESKS and r["first"] != r["history_first"] for r in subset
            ),
        }
    data = {
        "n": len(rows),
        "cells": cells,
        "saw_contact_order": lookups,
        "searched_booking_records": booking_searches,
        "calls": len(ledger),
        "spend_usd": str(spent),
        "remaining_usd": after["remaining_usd"],
    }
    save_private(root / "summary.json", data)
    lines = [
        "# Matched first-contact choices",
        "",
        "All times are Event Clock seconds after 08:00.",
        "",
        "| Block | Task | Past fast desk | First in history | First in request | First contact |",
        "|---:|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['block']} | {r['task']} | {r['history']} | {r['history_first']} | "
            f"{r['listed_first']} | [{r['first']}](#{r['case'].replace('_', '-')}) |"
        )
    for r in rows:
        case = r["case"]
        lines += [
            "",
            f'<a id="{case.replace("_", "-")}"></a>',
            f"## {case}",
            "",
            f"[Scenario]({case}/scenario.json) · "
            f"[Model-visible history]({case}/decision-input.json) · "
            f"[Decisions]({case}/decision-batches.json) · "
            f"[Trajectory and receipts]({case}/trajectory.json)",
            "",
        ]
        for mail in r["outgoing"]:
            m = mail["message"]
            lines += [
                f"To {m['recipient_owner_id']} · {m['subject']}",
                "",
                *["> " + line for line in m["body"].splitlines()],
                "",
                f"Message `{m['id']}` · delivered {mail['delivery_s']} s, read {mail['read_s']} s, "
                f"acknowledged {mail['ack_s']} s.",
                "",
            ]
    (root / "comparison.md").write_text("\n".join(lines) + "\n")
    return data


def mirror_comparison(original, mirror, checks=None):
    summaries = [report(root) for root in (original, mirror)]
    pairs = []
    for a, b in zip(load(original, "results"), load(mirror, "results"), strict=True):
        case = a["case"]
        assert b["case"] == case
        old, new = [load(root / case, "scenario") for root in (original, mirror)]
        assert [o["id"] for o in old["owners"]] == ["lead", *DESKS]
        assert [o["id"] for o in new["owners"]] == ["lead", *reversed(DESKS)]
        new["owners"][1:] = reversed(new["owners"][1:])
        assert old == new
        pairs.append(
            {
                **{
                    k: a[k]
                    for k in ("case", "block", "task", "history", "history_first", "listed_first")
                },
                "by_directory_first": {
                    desk: {
                        "first": row["first"],
                        "run_id": row["run_id"],
                        "saw_directory": case in run["saw_contact_order"],
                        "outgoing": row["outgoing"],
                    }
                    for desk, row, run in zip(DESKS, (a, b), summaries, strict=True)
                },
            }
        )
    comparison = {"studies": summaries, "matched_pairs": pairs}
    if checks:
        comparison["instructed_summary"] = report(checks)
        comparison["instructed_cases"] = load(checks, "results")
        for row in comparison["instructed_cases"]:
            case = row["case"]
            source = original if row.get("directory_first", DESKS[0]) == DESKS[0] else mirror
            baseline, positive = [load(p / case, "scenario") for p in (source, checks)]
            cue = positive["clock"]["timeline"][-1]
            assert cue["cue"] == baseline["clock"]["timeline"][-1]["cue"] + " " + USE_HISTORY
            cue["cue"] = baseline["clock"]["timeline"][-1]["cue"]
            assert positive == baseline
    return comparison


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Evidence checks require assertions.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--export", type=Path)
    parser.add_argument("--history-check", type=Path)
    args = parser.parse_args()
    if (args.export or args.history_check) and not args.mirror:
        parser.error("--export and --history-check require --mirror")
    if args.mirror:
        comparison = mirror_comparison(args.study, args.mirror, args.history_check)
        if args.export:
            save_private(args.export, comparison)
        print(f"Verified {len(comparison['matched_pairs'])} matched scenario pairs.")
    else:
        print(json.dumps(report(args.study), indent=2))
