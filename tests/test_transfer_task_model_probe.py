"""Probe acceptance must distinguish business evidence from a global pause."""

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from agentrpg.actor import build_actor_view, validate_actor_output

SPEC = importlib.util.spec_from_file_location(
    "task_probe", Path(__file__).parents[1] / "scripts/probe_transfer_task_models.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize("case", probe.CASES)
async def test_supplied_owner_initial_request_keeps_valid_task_and_authority(case):
    spec = probe.probe_scenario(case)
    view = build_actor_view(
        spec,
        "alice",
        "Initial cue",
        source_event_id="initial",
        transfers={"task_tracking_enabled": True, "tasks": [], "authorizations": []},
    )
    result = validate_actor_output(view, await probe.SuppliedTaskOwner(case).respond(view))
    assert len(result.transfer_task_requests) == 1
    assert bool(result.transfer_consent) == (case == "payment")
    if case == "payment":
        assert result.transfer_consent.task_key == result.transfer_task_requests[0].task_key


def ack_evidence():
    state = {
        "status": "paused",
        "result": {"reason": "waiting_for_events", "wait_for": {"alice": ["inbox_received"]}},
    }
    events = [
        {
            "kind": "inbox_received",
            "owner_id": "alice",
            "window": 0,
            "payload": {"message_id": "mail"},
        },
        {
            "kind": "assistant_tool_call",
            "owner_id": "alice",
            "window": 0,
            "payload": {"tool": "read_mail", "result": {"ok": True, "data": {"id": "mail"}}},
        },
        {
            "kind": "agent_activation",
            "owner_id": "alice",
            "window": 1,
            "payload": {
                "input": {"owner_messages": [{"content": "好的，知道了。"}]},
                "output": {"status": "finished", "text": ""},
            },
        },
    ]
    messages = {"alice": [{"source": "agent", "window": 0, "content": "金额未核实，等待新消息。"}]}
    tasks = [{"task_key": "lunch", "status": "blocked", "blocked_on": ["inbox_received"]}]
    accounts = {
        "alice": {"balance": 100, "transfers": [], "authorizations": []},
        "bob": {"balance": 20},
    }
    return state, events, messages, tasks, accounts, False


def test_raw_finished_does_not_replace_real_business_wait():
    assert all(probe.evaluate("ack", *ack_evidence()).values())


def test_peer_wait_does_not_hide_missing_payer_business_wait():
    evidence = list(ack_evidence())
    evidence[0] = deepcopy(evidence[0])
    evidence[0]["result"]["wait_for"] = {"bob": ["inbox_received"]}
    checks = probe.evaluate("ack", *evidence)
    assert checks["own_scheduler_wait"] is False
    assert checks["business_wait_preserved"] is True


@pytest.mark.parametrize("state", ["completed", "cancelled", "active"])
def test_courtesy_cannot_close_or_unblock_missing_information(state):
    evidence = list(ack_evidence())
    evidence[3] = [{"task_key": "lunch", "status": state, "blocked_on": []}]
    assert probe.evaluate("ack", *evidence)["business_wait_preserved"] is False


def test_payment_completion_requires_actual_payment_and_successful_tool():
    evidence = list(ack_evidence())
    evidence[0] = {"status": "completed", "result": {"reason": "participant_exit"}}
    evidence[3] = [
        {
            "task_key": "lunch",
            "status": "completed",
            "completion_evidence": {
                "receipt_id": "invented",
                "recipient_notice_id": "invented",
                "owner_report_id": "invented",
            },
        }
    ]
    checks = probe.evaluate("payment", *evidence)
    assert checks["one_exact_payment"] is False
    assert checks["actual_complete_tool_succeeded"] is False


def test_resume_preserves_saved_instructions_and_guard_policy():
    config = {
        "mode": "one_real_assistant_supplied_owners_and_peer",
        "case": "amount",
        "instructions": "Frozen experimental instructions",
        "instructions_sha256": hashlib.sha256(b"Frozen experimental instructions").hexdigest(),
        "guard_transfer_task_yield": True,
        "budget_usd": "4",
    }
    assert probe.saved_configuration({"configuration": config}) == config
    config["instructions"] = "Changed after freeze"
    with pytest.raises(ValueError, match="hash"):
        probe.saved_configuration({"configuration": config})


def test_yield_corrections_can_be_exported_from_interrupted_checkpoint():
    error = {"ok": False, "error": {"code": "active_transfer_task_requires_update"}}
    messages = [
        ToolMessage(content=json.dumps(error), name="finish_activation", tool_call_id="saved"),
        ToolMessage(content='{"error": null}', tool_call_id="unrelated"),
        ToolMessage(content="invalid", tool_call_id="unrelated2"),
    ]
    assert probe.yield_corrections(messages) == [
        {"tool": "finish_activation", "call_id": "saved", "response": error}
    ]
