"""Negative acceptance cases plus real PostgreSQL audit and snapshot evidence."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentrpg.autonomy_validation import (
    AcceptancePolicy,
    acceptance_report,
    audit_summary,
    collect_run,
    wilson_interval,
)
from agentrpg.director import Director, row
from agentrpg.shop import ShopService
from agentrpg.shop_demo import shopping_scenario
from agentrpg.transfer_demo import run_local_check
from agentrpg.transfer_task_demo import run_lifecycle_case
from agentrpg.transfer_validation import validation_scenario
from agentrpg.trial_audit import begin_trial, end_trial

ROOT = Path(__file__).resolve().parents[1]


def events(seconds=10):
    now = datetime.now(UTC)
    invocation = str(uuid4())
    return [
        {"kind": "control_audit_enabled", "payload": {"revision": 1}, "created_at": now},
        {
            "kind": "operator_control",
            "created_at": now,
            "payload": {"action": "start", "previous_status": "pending"},
        },
        {
            "kind": "trial_started",
            "created_at": now,
            "payload": {
                "invocation_id": invocation,
                "source_hash": "source-1",
                "configuration_hash": "policy-1",
            },
        },
        {
            "kind": "trial_finished",
            "created_at": now + timedelta(seconds=seconds),
            "payload": {
                "invocation_id": invocation,
                "source_hash": "source-1",
                "returned_report": True,
            },
        },
    ]


def evidence(*, mode="full_model", passed=True, case="transfer/baseline", seconds=10):
    return {
        "run_id": str(uuid4()),
        "case": case,
        "mode": mode,
        "scenario_hash": "scenario-1",
        "functional_passed": passed,
        "hard_failure_checks": [],
        "completed_business_tasks": 1,
        "audit": audit_summary(events(seconds)),
        "provider_attempts": 10 if mode == "full_model" else 0,
        "replay_url": "http://127.0.0.1:8000/console",
    }


def small_policy():
    return AcceptancePolicy(
        min_repeats_per_case=3, min_model_trials=3, required_model_cases=("transfer/baseline",)
    )


def test_missing_audit_never_means_zero_intervention():
    report = audit_summary([])
    assert report["operator_actions"] is None
    assert not report["unattended"]
    assert report["invocation_elapsed_seconds"] is None


@pytest.mark.parametrize("change", ["resume", "source", "invocation", "unfinished", "legacy"])
def test_partial_or_changed_history_cannot_pass_as_unattended(change):
    trace = events()
    if change == "resume":
        trace.append({"kind": "operator_control", "payload": {"action": "resume"}})
    elif change == "source":
        trace[-1]["payload"]["source_hash"] = "source-2"
    elif change == "invocation":
        trace.extend(events()[2:])
    elif change == "unfinished":
        trace.pop()
    else:
        trace.pop(0)
    assert not audit_summary(trace)["unattended"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interventions": 1},
        {"pauses": 1},
        {"branches": 1},
        {"executions": [{"mode": "step"}]},
        {"executions": [{"mode": "continue"}, {"mode": "continue"}]},
    ],
)
def test_console_operations_are_not_autonomy(kwargs):
    assert not audit_summary(events(), **kwargs)["unattended"]


def test_automatic_recovery_and_first_continue_are_distinct_from_manual_resume():
    trace = events()
    trace.append({"kind": "recovery_resumed"})
    audit = audit_summary(trace, executions=[{"mode": "continue"}])
    assert audit["unattended"] and audit["automatic_recoveries"] == 1
    assert audit["invocation_elapsed_seconds"] == 10


def test_controls_do_not_count_toward_model_or_duration_acceptance():
    runs = [evidence(mode="supplied_control", seconds=90000) for _ in range(20)]
    result = acceptance_report(runs, expected_control_cases=("transfer/baseline",))
    assert result["local_control_repeats_passed"]
    assert not result["model_repeat_gate_passed"]
    assert not any(g["passed"] for g in result["duration_gates"])


def test_resume_or_duplicate_id_cannot_inflate_sample_count():
    r = evidence()
    with pytest.raises(ValueError, match="only once"):
        acceptance_report([r, deepcopy(r)])


@pytest.mark.parametrize("field", ["source_hash", "configuration_hash"])
def test_changed_policies_do_not_pool_into_three_repeats(field):
    runs = [evidence() for _ in range(3)]
    runs[-1]["audit"][field] = "changed"
    assert not acceptance_report(runs, small_policy())["model_repeat_gate_passed"]


def test_failures_stay_in_denominator_and_money_error_has_zero_tolerance():
    runs = [evidence() for _ in range(20)]
    runs[-1]["functional_passed"] = False
    result = acceptance_report(runs, small_policy())
    assert result["cohorts"][0]["functional_success_rate"] == 0.95
    assert result["model_repeat_gate_passed"]
    runs[-1]["hard_failure_checks"] = ["one_exact_payment"]
    assert not acceptance_report(runs, small_policy())["model_repeat_gate_passed"]


def test_unattended_long_elapsed_time_also_requires_completed_tasks():
    runs = [evidence(seconds=3000) for _ in range(3)]
    result = acceptance_report(runs, small_policy())
    assert result["model_repeat_gate_passed"]
    assert not result["duration_gates"][0]["passed"]
    runs[0]["completed_business_tasks"] = 20
    result = acceptance_report(runs, small_policy())
    assert result["duration_gates"][0]["passed"]
    assert not result["duration_gates"][1]["passed"]
    assert not result["long_term_autonomy_verified"]


def test_small_all_pass_sample_is_not_95_percent_reliability_proof():
    low, high = wilson_interval(3, 3)
    assert 0 < low < 0.95 and high == pytest.approx(1)
    assert wilson_interval(0, 0) is None
    assert not acceptance_report([])["model_repeat_gate_passed"]


async def test_durable_operator_control_and_no_model_local_evidence(app):
    original = await run_local_check(app)
    run_id = UUID(original["run_id"])
    report = await collect_run(
        app.state.pool,
        run_id,
        profile="local-payment",
        case="baseline",
        local_checks=original["checks"],
    )
    assert report["functional_passed"]
    assert report["mode"] == "supplied_control" and report["provider_attempts"] == 0
    assert report["audit"]["coverage"] == "instrumented_invocation"
    # The local fixture deliberately pauses for review; never claim zero operation.
    assert report["audit"]["operator_actions"] == 1
    trace = await Director(app.state.pool).trace(run_id)
    assert [e["payload"]["action"] for e in trace if e["kind"] == "operator_control"] == [
        "start",
        "pause",
    ]


@pytest.mark.parametrize("case", ["ack", "amount", "cancel"])
async def test_local_wait_and_cancellation_keep_case_rubric_and_no_payment(app, case):
    original = await run_lifecycle_case(app, case)
    report = await collect_run(
        app.state.pool,
        original["run_id"],
        profile="local-lifecycle",
        case=case,
        local_checks=original["checks"],
    )
    assert report["functional_passed"] and report["completed_business_tasks"] == 0
    assert report["audit"]["unattended"]
    assert report["mode"] == "supplied_control"


async def test_relabelled_case_and_active_final_snapshot_rejected(app):
    director = Director(app.state.pool)
    manifest = await director.create_run(validation_scenario("ambiguous"))
    run_id = UUID(manifest["run_id"])
    with pytest.raises(ValueError, match="Pause"):
        await collect_run(app.state.pool, run_id, profile="transfer", case="ambiguous")
    await director.control(run_id, "start")
    await director.control(run_id, "pause")
    with pytest.raises(ValueError, match="does not match"):
        await collect_run(app.state.pool, run_id, profile="transfer", case="baseline")


async def test_collector_uses_one_snapshot_and_writes_no_evidence_records(app):
    original = await run_lifecycle_case(app, "ack")
    run_id = UUID(original["run_id"])
    pool = app.state.pool
    async with pool.connection() as conn:
        before = await row(conn, "SELECT count(*) AS n FROM run_events WHERE run_id=%s", (run_id,))
    await collect_run(
        pool, run_id, profile="local-lifecycle", case="ack", local_checks=original["checks"]
    )
    async with pool.connection() as conn:
        after = await row(conn, "SELECT count(*) AS n FROM run_events WHERE run_id=%s", (run_id,))
    assert before == after


async def test_no_provider_records_alone_do_not_prove_supplied_control(app):
    director = Director(app.state.pool)
    manifest = await director.create_run(shopping_scenario(3))
    run_id = UUID(manifest["run_id"])
    await ShopService(app.state.pool).enable(run_id)
    await director.control(run_id, "start")
    await director.control(run_id, "pause")
    report = await collect_run(app.state.pool, run_id, profile="shopping", case="3")
    assert report["provider_attempts"] == 0
    assert report["mode"] == "unknown"


async def test_hash_only_audit_has_no_prompt_or_credential(app):
    director = Director(app.state.pool)
    manifest = await director.create_run(validation_scenario("baseline"))
    run_id = UUID(manifest["run_id"])
    invocation = await begin_trial(
        app.state.pool,
        run_id,
        profile="test",
        root=ROOT,
        configuration={"prompt": "private-prompt-value", "token": "private-secret-value"},
    )
    await end_trial(app.state.pool, run_id, invocation, root=ROOT, returned_report=True)
    text = str(await director.trace(run_id))
    assert "private-prompt-value" not in text and "private-secret-value" not in text
