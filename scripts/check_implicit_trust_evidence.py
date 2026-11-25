"""Damage copies of a free control, then re-evaluate its native evidence."""

import argparse
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from try_clock_implicit_trust import DESKS, audit, load

from agentrpg.console_execution import save_private


def check(root):
    cases = load(root, "results")
    for flaw in (
        "receipt",
        "reply_parent",
        "read_body",
        "owner_question",
        "history_exposure",
        "owner_rule",
    ):
        ask = flaw == "owner_question"
        case = next(
            k for k, d in cases.items() if d[DESKS[0]]["outcome"] == ("ask" if ask else "full")
        )
        expected = cases[case][DESKS[0]]
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "case"
            shutil.copytree(root / case, folder)
            t = load(folder, "trajectory")
            if ask:
                event = next(
                    e for e in t["events"] if e["id"] == expected["ask_records"][0]["event"]
                )
                event["payload"]["message_id"] = "unrelated-message"
            elif flaw == "owner_rule":
                t["events"] = [e for e in t["events"] if e["id"] != expected["rule_event"]]
            elif flaw == "history_exposure":
                obs = load(folder, "decision-input")
                obs[DESKS[0]]["opened"].pop(0)
                save_private(folder / "decision-input.json", obs)
            else:
                msg = expected["received"][0]
                if flaw == "receipt":
                    t["receipts"] = [r for r in t["receipts"] if r["message_id"] != msg["id"]]
                else:
                    kind = "send" if flaw == "reply_parent" else "read"
                    event = next(e for e in t["events"] if e["id"] == msg["evidence"][kind])
                    field = "in_reply_to" if kind == "send" else "body"
                    event["payload"]["result"]["data"][field] = "unrelated"
            save_private(folder / "trajectory.json", t)
            try:
                audit(folder)
            except (AssertionError, StopIteration):
                print("Rejected", flaw)
            else:
                raise AssertionError(f"Accepted {flaw}")


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Checks require assertions.")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("controls", type=Path)
    check(p.parse_args().controls)
