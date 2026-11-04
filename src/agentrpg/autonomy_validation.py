"""Read-only, fail-closed evidence for controls and bounded model repeat trials.

Functional completion, model choice, human operation and real elapsed duration
are separate measurements. Missing audit data never means zero interventions.
"""

import math
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from agentrpg.director import Director, rows


@dataclass(frozen=True)
class AcceptancePolicy:
    min_repeats_per_case: int = 3
    min_model_trials: int = 20
    target_success_rate: float = 0.95
    min_completed_tasks: int = 20
    duration_stages_seconds: tuple = (1800, 7200, 28800)
    required_model_cases: tuple = (
        "transfer/baseline",
        "transfer/insufficient",
        "transfer/ambiguous",
        "transfer/duplicate",
        "shopping/3",
    )

    def __post_init__(self):
        if (
            any(
                type(n) is not int or n < 1
                for n in (
                    self.min_repeats_per_case,
                    self.min_model_trials,
                    self.min_completed_tasks,
                    *self.duration_stages_seconds,
                )
            )
            or not 0 < self.target_success_rate <= 1
        ):
            raise ValueError("Use positive sample sizes, durations and a rate at most one.")


def wilson_interval(successes, total):
    """Descriptive 95% Wilson interval; independent, comparable trials are assumed."""
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    scale = 1 + z * z / total
    center = (p + z * z / (2 * total)) / scale
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return [max(0.0, center - radius), min(1.0, center + radius)]


def audit_summary(events, *, interventions=0, pauses=0, executions=(), branches=0):
    starts = [e for e in events if e["kind"] == "trial_started"]
    finishes = [e for e in events if e["kind"] == "trial_finished"]
    controls = [e for e in events if e["kind"] == "operator_control"]
    initial = [
        e for e in controls if e["payload"] == {"action": "start", "previous_status": "pending"}
    ]
    manual_controls = len(controls) - min(1, len(initial))
    # A first Continue may start a run. Step and later launches require operation.
    execution_operations = max(0, len(executions) - 1) + sum(
        e["mode"] == "step" for e in executions[:1]
    )
    known_operations = manual_controls + interventions + pauses + execution_operations + branches
    marker = any(e["kind"] == "control_audit_enabled" for e in events)
    complete = marker and len(starts) == len(finishes) == 1
    begin, end = (
        starts[0] if len(starts) == 1 else None,
        finishes[0] if len(finishes) == 1 else None,
    )
    stable = bool(
        complete
        and begin["payload"]["invocation_id"] == end["payload"]["invocation_id"]
        and begin["payload"]["source_hash"] == end["payload"]["source_hash"]
        and end["payload"].get("returned_report") is True
        and end["created_at"] >= begin["created_at"]
    )
    seconds = (end["created_at"] - begin["created_at"]).total_seconds() if stable else None
    return {
        "coverage": "instrumented_invocation" if stable else "incomplete_or_legacy",
        "observed_operator_actions": known_operations,
        "operator_actions": known_operations if stable else None,
        "invocations": len(starts),
        "unattended": stable and known_operations == 0,
        "invocation_elapsed_seconds": seconds,
        "source_hash": begin["payload"].get("source_hash") if stable else None,
        "configuration_hash": begin["payload"].get("configuration_hash") if stable else None,
        "automatic_recoveries": sum(e["kind"] == "recovery_resumed" for e in events),
        "stalls": sum(e["kind"] == "task_stalled" for e in events),
        "job_failures": sum(e["kind"] == "job_failure" for e in events),
        "simulator_errors": sum(e["kind"] == "simulator_error" for e in events),
    }


class _SnapshotPool:
    def __init__(self, connection):
        self._connection = connection

    @asynccontextmanager
    async def connection(self):
        yield self._connection


async def collect_run(pool, run_id, *, profile, case, local_checks=None):
    async with pool.connection() as conn, conn.transaction():
        # Existing account inspectors take a row lock. They write no records,
        # but PostgreSQL does not permit that lock in a READ ONLY transaction.
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        return await _collect_run(
            _SnapshotPool(conn), run_id, profile=profile, case=case, local_checks=local_checks
        )


async def _collect_run(pool, run_id, *, profile, case, local_checks=None):
    """Re-evaluate model runs from current DB truth; local checks are fresh driver output."""
    run_id = UUID(str(run_id))
    director = Director(pool)
    run = await director.get_run(run_id)
    if run["status"] in {"pending", "running"}:
        raise ValueError("Pause the run before taking a final acceptance snapshot.")
    report = {}
    if profile == "transfer":
        from agentrpg.transfer_validation import validation_report, validation_scenario

        # A caller cannot relabel another scenario to select an easier rubric.
        if run["scenario_hash"] != validation_scenario(case).content_hash():
            raise ValueError("The saved scenario does not match this transfer case.")
        report = await validation_report(pool, run_id, case)
    elif profile == "shopping":
        from agentrpg.shop_demo import shopping_model_scenario, shopping_scenario
        from agentrpg.shop_runtime import shop_report

        rounds = int(case)
        if run["scenario_hash"] not in {
            shopping_scenario(rounds).content_hash(),
            shopping_model_scenario(rounds).content_hash(),
        }:
            raise ValueError("The saved scenario does not match this purchase count.")
        report = await shop_report(pool, run_id, rounds=rounds)
    elif profile in {"local-payment", "local-lifecycle"} and local_checks:
        from agentrpg.transfer_demo import scenario
        from agentrpg.transfer_task_demo import lifecycle_scenario

        expected = scenario() if profile == "local-payment" else lifecycle_scenario(case)
        if (profile == "local-payment" and case != "baseline") or run[
            "scenario_hash"
        ] != expected.content_hash():
            raise ValueError("The saved scenario does not match this local case.")
        report = {"checks": local_checks}
    else:
        raise ValueError("Unknown profile or missing fresh local checks.")

    events = await director.trace(run_id)
    participants = await director.participants(run_id)
    async with pool.connection() as conn:
        spending = await rows(
            conn,
            "SELECT owner_id,role,status,reported_usd,reserved_usd "
            "FROM model_spend_attempts WHERE run_id=%s",
            (run_id,),
        )
        interventions = await rows(
            conn, "SELECT id FROM console_interventions WHERE run_id=%s", (run_id,)
        )
        pauses = await rows(
            conn, "SELECT request_id FROM console_pause_requests WHERE run_id=%s", (run_id,)
        )
        executions = await rows(
            conn,
            "SELECT mode FROM console_executions WHERE run_id=%s ORDER BY created_at,id",
            (run_id,),
        )
        branches = await rows(
            conn, "SELECT child_run_id FROM run_branches WHERE child_run_id=%s", (run_id,)
        )
        jobs = await rows(
            conn, "SELECT owner_id,kind,output_meta FROM run_jobs WHERE run_id=%s", (run_id,)
        )

    audit = audit_summary(
        events,
        interventions=len(interventions),
        pauses=len(pauses),
        executions=executions,
        branches=len(branches),
    )
    actor_roles = {"actor", "browser-shop-actor"}
    agent_roles = {"assistant", "browser-shop-buyer", "browser-shop-seller"}
    successful = [s for s in spending if s["status"] == "reported"]
    actors = {s["owner_id"] for s in successful if s["role"] in actor_roles}
    agents = {s["owner_id"] for s in successful if s["role"] in agent_roles}
    owners = {p["owner_id"] for p in participants}
    model_actors = bool(jobs) and all(
        j["output_meta"].get("attempts") for j in jobs if j["kind"] == "actor"
    )
    mode = (
        "full_model"
        if owners <= actors and owners <= agents and model_actors
        else "partial_model"
        if spending
        else "supplied_control"
        if profile.startswith("local-")
        or any(
            e["kind"] == "trial_started" and e["payload"].get("decision_mode") == "control"
            for e in events
        )
        else "unknown"
    )
    checks = report["checks"]
    metrics = report.get("metrics", {})
    money_checks = {
        "one_order_per_task",
        "correct_total",
        "wallets_match_and_conserve_credits",
        "credits_conserved_for_case",
        "one_transaction",
        "final_balances",
        "total_credits_conserved",
        "no_payment",
        "no_transfer_authority",
        "no_payment_or_grant",
        "funds_unchanged",
        "one_exact_payment",
    }
    hard_failures = sorted(k for k in money_checks if k in checks and checks[k] is not True)
    completed_tasks = (
        sum(t["status"] == "completed" for t in report.get("tasks", []))
        if profile == "shopping"
        else len(report.get("transfers", []))
    )
    if profile == "local-payment" and report["checks"].get("one_transaction") is True:
        completed_tasks = 1
    return {
        "run_id": str(run_id),
        "case": f"{profile}/{case}",
        "mode": mode,
        "scenario_hash": run["scenario_hash"],
        "run_status": run["status"],
        "stop_reason": (run["result"] or {}).get("reason"),
        "checks": checks,
        "functional_passed": bool(checks) and all(v is True for v in checks.values()),
        "hard_failure_checks": hard_failures,
        "completed_business_tasks": completed_tasks,
        "audit": audit,
        "behaviour_signals": {
            "exact_repeated_messages": metrics.get("exact_repeated_messages"),
            "failed_tool_calls": sum(
                e["kind"] == "assistant_tool_call" and not e["payload"].get("result", {}).get("ok")
                for e in events
            ),
            "active_information_gathering": "not_semantically_evaluated",
        },
        "provider_attempts": len(spending),
        "confirmed_usd": str(sum((s["reported_usd"] or Decimal(0) for s in spending), Decimal(0))),
        "unconfirmed_exposure_usd": str(
            sum(
                (
                    max(s["reserved_usd"], s["reported_usd"] or Decimal(0))
                    for s in spending
                    if s["status"] != "reported"
                ),
                Decimal(0),
            )
        ),
        "replay_url": f"http://127.0.0.1:8000/console?run={run_id}&turn=0",
    }


def acceptance_report(runs, policy=None, *, expected_control_cases=()):
    policy = policy or AcceptancePolicy()
    ids = [r["run_id"] for r in runs]
    if len(set(ids)) != len(ids):
        raise ValueError("A run can be counted only once, including across resumes.")
    groups = defaultdict(list)
    for r in runs:
        key = (
            r["case"],
            r["mode"],
            r["scenario_hash"],
            r["audit"]["source_hash"],
            r["audit"]["configuration_hash"],
        )
        groups[key].append(r)
    cohorts = []
    for key, members in sorted(groups.items(), key=lambda item: str(item[0])):
        passed = sum(r["functional_passed"] for r in members)
        autonomous = sum(
            r["functional_passed"] and r["audit"]["unattended"] and not r["hard_failure_checks"]
            for r in members
        )
        cohorts.append(
            {
                "case": key[0],
                "mode": key[1],
                "scenario_hash": key[2],
                "source_hash": key[3],
                "configuration_hash": key[4],
                "runs": len(members),
                "functional_passes": passed,
                "unattended_passes": autonomous,
                "hard_failures": sum(bool(r["hard_failure_checks"]) for r in members),
                "functional_success_rate": passed / len(members),
                "wilson_95_interval": wilson_interval(passed, len(members)),
                "repeat_gate_passed": key[1] == "full_model"
                and bool(key[3] and key[4])
                and len(members) >= policy.min_repeats_per_case
                and all(
                    not r["hard_failure_checks"]
                    and r["audit"]["operator_actions"] == 0
                    and r["audit"]["simulator_errors"] == 0
                    for r in members
                )
                and autonomous / len(members) >= policy.target_success_rate,
            }
        )
    model_cohorts = [c for c in cohorts if c["mode"] == "full_model"]
    model_runs = [r for r in runs if r["mode"] == "full_model"]
    model_gate = (
        bool(model_cohorts)
        and all(c["repeat_gate_passed"] for c in model_cohorts)
        and len(model_runs) >= policy.min_model_trials
        and set(policy.required_model_cases) <= {c["case"] for c in model_cohorts}
        and len({c["source_hash"] for c in model_cohorts}) == 1
    )
    controls = [c for c in cohorts if c["mode"] == "supplied_control"]
    control_gate = (
        bool(expected_control_cases)
        and set(expected_control_cases) == {c["case"] for c in controls}
        and all(
            c["runs"] >= policy.min_repeats_per_case
            and c["functional_success_rate"] == 1
            and c["source_hash"]
            and c["configuration_hash"]
            for c in controls
        )
        and len({c["source_hash"] for c in controls}) == 1
        and all(
            not r["hard_failure_checks"] and r["provider_attempts"] == 0
            for r in runs
            if r["mode"] == "supplied_control"
        )
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": asdict(policy),
        "unique_runs": len(runs),
        "local_control_repeats_passed": bool(control_gate),
        "model_repeat_gate_passed": bool(model_gate),
        "duration_gates": [
            {
                "seconds": seconds,
                "passed": bool(
                    model_gate
                    and any(
                        r["functional_passed"]
                        and r["audit"]["unattended"]
                        and (r["audit"]["invocation_elapsed_seconds"] or 0) >= seconds
                        and r["completed_business_tasks"] >= policy.min_completed_tasks
                        for r in model_runs
                    )
                ),
            }
            for seconds in policy.duration_stages_seconds
        ],
        "long_term_autonomy_verified": False,
        "cohorts": cohorts,
        "runs": runs,
        "remaining_acceptance": [
            "Model repeat and duration gates must pass on a predeclared matrix.",
            "Model fault recovery needs its own predeclared fault matrix.",
            "Memory/resource trends and human calibration of dialogue remain unmeasured.",
        ],
        "limits": [
            "Controls supply decisions and never establish model autonomy.",
            "A safe wait is assessed by its case rubric, not counted as a completed payment.",
            "Missing historical audits are unknown, not zero operator interventions.",
            "Invocation time is real elapsed time, not simulated time or cumulative uptime.",
            "Operator counts cover application controls, not unobserved external DB or host edits.",
            "Intervals are descriptive; selected or correlated trials "
            "are not population guarantees.",
            "Automatic receipt checks do not establish semantic honesty or naturalness.",
        ],
    }


def markdown_report(report):
    lines = [
        "# Autonomy acceptance",
        "",
        f"Unique runs: {report['unique_runs']}.",
        f"Local control repeats: {report['local_control_repeats_passed']}.",
        f"Model repeat gate: {report['model_repeat_gate_passed']}.",
        "Long-term autonomy: not verified.",
        "",
        "| Case | Scope | Runs | Functional passes | Unattended passes |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for c in report["cohorts"]:
        lines.append(
            f"| {c['case']} | {c['mode']} | {c['runs']} | "
            f"{c['functional_passes']} | {c['unattended_passes']} |"
        )
    lines += ["", "## Trajectories", ""]
    for r in report["runs"]:
        lines.append(f"- [{r['case']} · {r['run_id']}]({r['replay_url']})")
    lines += ["", "## Limits", "", *[f"- {s}" for s in report["limits"]], ""]
    return "\n".join(lines)
