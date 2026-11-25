import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("step_approval")


def test_handwritten_approval_and_fail_closed_cases(tmp_path, gate):
    step = gate.describe(
        "subject", "stub", ["C001"], [{"body": "hello"}], "0.01", "0.32", 1, "test"
    )
    assert step["payload_sha256"] == gate.digest([{"body": "hello"}])
    calls = []
    gate.dispatch(tmp_path, step, execute=False, approval=None, action=calls.append)
    path = tmp_path / gate.REQUEST_FILE
    stamp = path.stat().st_mtime_ns
    manual = {k: step[k] for k in ("judge", "case_ids", "payload_sha256")}
    manual["max_usd"] = "0.02"

    # The capability exists only in memory. No test or program writes an approval file.
    def memory_file(value, modified=stamp):
        return SimpleNamespace(
            is_file=lambda: True,
            stat=lambda: SimpleNamespace(st_mtime_ns=modified),
            read_bytes=lambda: json.dumps(value).encode(),
        )

    for bad in (
        None,
        tmp_path / "missing",
        memory_file({**manual, "payload_sha256": "wrong"}),
        memory_file({**manual, "case_ids": ["C002"]}),
        memory_file({**manual, "judge": "other"}),
        memory_file({**manual, "max_usd": "0.009"}),
        memory_file(step),
        memory_file({**manual, gate.MARKER: True}),
        memory_file(manual, stamp - 1),
    ):
        with pytest.raises(ValueError):
            gate.dispatch(tmp_path, step, execute=True, approval=bad, action=calls.append)
    assert calls == []

    def submit(cap):
        def stub(request):
            calls.append(cap)
            return httpx.Response(200, json={"ok": True})

        with httpx.Client(transport=httpx.MockTransport(stub)) as client:
            assert client.post("https://stub.invalid/judge").json() == {"ok": True}

    gate.dispatch(tmp_path, step, execute=True, approval=memory_file(manual), action=submit)
    assert list(map(str, calls)) == ["0.02"]
    with pytest.raises(FileExistsError):
        gate.dispatch(
            tmp_path, step, execute=True, approval=memory_file(manual), action=calls.append
        )
    assert len(calls) == 1


def test_routing_wallet_receipt_matches_the_step_and_current_ledger(gate):
    from decimal import Decimal

    checked = importlib.import_module("run_partner_routing_live").checked_wallet
    ledger = dict(id="test", attempts=1, remaining_usd="0.48", uncertain_usd="0", reserved_usd="0")
    receipt = dict(provider_model="stub", case_ids=["r1"], ledger=ledger, provider_cny="4")
    assert checked(receipt, ledger, "stub", ["r1"]) == Decimal(4)
    with pytest.raises(ValueError, match="campaign changed"):
        checked(receipt, {**ledger, "attempts": 2}, "stub", ["r1"])
    with pytest.raises(ValueError, match="different step"):
        checked(receipt, ledger, "stub", ["r2"])
