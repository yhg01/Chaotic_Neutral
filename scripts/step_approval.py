"""Offline approval requests and single-use dispatch for a fixed step."""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / ".local/partner-routing-history-check-01/budget.json"
MARKER = "generated_request_only"
REQUEST_FILE = "approval-request.json"


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def latest_snapshot(reference=LEDGER):
    base = json.loads(reference.read_bytes())["after"]
    root = reference.parent.parent
    candidates = []
    for filename, field in (("budget.json", "after"), ("preflight-result.json", "ledger")):
        for path in root.rglob(filename):
            record = json.loads(path.read_bytes()).get(field, {})
            if record.get("id") == base["id"] and record.get("limit_usd") == base["limit_usd"]:
                candidates.append((record["attempts"], path.stat().st_mtime_ns, path, record))
    _, _, path, record = max(candidates, key=lambda c: c[:3])
    return path, record


def describe(judge, provider, ids, payloads, cost, reserve, count, kind, **details):
    path, balance = latest_snapshot()
    return {
        MARKER: True,
        "judge": judge,
        "provider": provider,
        "kind": kind,
        "case_ids": list(ids),
        "number_of_requests": count,
        "estimated_usd": str(cost),
        "maximum_reservation_usd": str(reserve),
        "campaign_balance_usd": balance["remaining_usd"],
        "saved_ledger": str(path),
        "payload_sha256": digest(payloads),
        "individual_sha256": [digest(p) for p in payloads],
        **details,
    }


def authorize(request, expected, approval):
    if approval is None or not approval.is_file():
        raise ValueError("A separate hand-written approval file is required.")
    if not request.is_file() or json.loads(request.read_bytes()) != expected:
        raise ValueError("Prepare and review the exact current request first.")
    if approval.stat().st_mtime_ns < request.stat().st_mtime_ns:
        raise ValueError("The approval predates the request.")
    data = json.loads(approval.read_bytes())
    if (
        not isinstance(data, dict)
        or MARKER in data
        or set(data)
        != {
            "payload_sha256",
            "judge",
            "case_ids",
            "max_usd",
        }
    ):
        raise ValueError("Use a hand-written approval with the four required fields.")
    if any(data[k] != expected[k] for k in ("payload_sha256", "judge", "case_ids")):
        raise ValueError("The approval does not identify this exact step.")
    try:
        cap = Decimal(str(data["max_usd"]))
        if not cap.is_finite() or cap < 0 or Decimal(expected["estimated_usd"]) > cap:
            raise ValueError("The estimated cost exceeds the approved maximum.")
    except InvalidOperation as exc:
        raise ValueError("Invalid approval maximum.") from exc
    return cap


def dispatch(out, step, *, execute, approval, action):
    path = out / REQUEST_FILE
    if not execute:
        if approval is not None:
            raise ValueError("Preparation takes no approval.")
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            if json.loads(path.read_bytes()) != step:
                raise ValueError("Use a fresh request directory for a different step.")
        else:
            with path.open("x") as stream:
                json.dump(step, stream, indent=2)
                stream.write("\n")
        print(json.dumps(step, indent=2))
        return
    cap = authorize(path, step, approval)
    # A second invocation, including a concurrent one, cannot spend the same step again.
    (out / "execution-started").touch(exist_ok=False)
    return action(cap)
