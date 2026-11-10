"""Free Clock controls in a fresh schema of a separate PostgreSQL test database.

No Actor/Agent model or business effect is run. A child process checks persisted
pause/replay state. The script removes only its own generated schema.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.migrate import migrate
from agentrpg.simulation_clock import (
    ClockContract,
    Control,
    ProcessSpec,
    Schedule,
    SimulationClock,
    Sleep,
    Work,
)
from agentrpg.trial_audit import source_fingerprint

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]


def test_dsn():
    value = os.environ.get("TEST_DATABASE_URL", "")
    if not value or "test" not in conninfo_to_dict(value).get("dbname", "").lower():
        raise ValueError("TEST_DATABASE_URL must name a separate database containing 'test'.")
    return value


async def child_check(args):
    if not re.fullmatch(r"clock_validation_[0-9a-f]{32}", args.schema or ""):
        raise ValueError("Only an owned Clock validation schema is accepted.")
    dsn = make_conninfo(test_dsn(), options=f"-c search_path={args.schema}")
    async with AsyncConnectionPool(dsn, kwargs={"row_factory": dict_row}) as pool:
        service = SimulationClock(pool)
        clock = UUID(args.check_paused_clock)
        before = await service.snapshot(clock)
        assert before["status"] == "paused"
        replay = await service.execute(clock, UUID(args.replay_request), Control(operation="step"))
        assert replay["reason"] == "event_delivered"
        assert before == await service.snapshot(clock)
        print(
            json.dumps({"paused_state_preserved": True, "prior_step_replayed_without_change": True})
        )


async def control(pool, schema, seconds, pairs):
    start = time.monotonic()
    service = SimulationClock(pool)
    spec = ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=seconds,
        processes=tuple(
            ProcessSpec(id=f"{role}-{i}", owner_id=f"owner-{i}", role=role)
            for i in range(pairs)
            for role in ["actor", "agent"]
        ),
        output_tokens_per_minute=600,
        tool_seconds={"synthetic_tool": 7},
    )
    clock = await service.create(spec)

    async def execute(command, request=None):
        return await service.execute(clock, request or uuid4(), command)

    async def sleep(process):
        await execute(Sleep(process_id=process, until_seconds=seconds, interrupt_on=("reminder",)))

    for p in spec.processes:
        if p.id == "actor-0":
            await execute(Work(process_id=p.id, kind="generation", output_tokens=600))
        elif p.id == "agent-0":
            await execute(Work(process_id=p.id, kind="tool", tool="synthetic_tool"))
        else:
            await sleep(p.id)
    for hour in range(1, seconds // 3600):
        await execute(
            Schedule(
                event_key=f"hour-{hour:04}",
                at_seconds=hour * 3600,
                kind="reminder",
                recipients=(f"agent-{hour % pairs}",),
                payload={"synthetic_control": True},
            )
        )
    for key, at in [("before-end", seconds - 1), ("at-end", seconds), ("after-end", seconds + 1)]:
        await execute(
            Schedule(event_key=key, at_seconds=at, kind="reminder", recipients=("agent-0",))
        )
    delivered = []
    subprocess_verified = False
    while True:
        request = uuid4()
        result = await execute(Control(operation="step"), request)
        if result["reason"] == "horizon_cutoff":
            break
        assert result["reason"] == "event_delivered", result["reason"]
        delivered.append(result)
        if len(delivered) == 2:
            await execute(Control(operation="pause"))
            before = await service.snapshot(clock)
            # Child receives a test DSN in its environment, never on the command line.
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(SCRIPT),
                "--check-paused-clock",
                str(clock),
                "--schema",
                schema,
                "--replay-request",
                str(request),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await child.communicate()
            if child.returncode:
                diagnostic = ROOT / ".local/clock-v2/child-replay-error.log"
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_bytes(stderr)
                diagnostic.chmod(0o600)
                raise RuntimeError(
                    "Independent Clock replay check failed; inspect local diagnostics."
                )
            assert json.loads(stdout)["prior_step_replayed_without_change"]
            assert before == await service.snapshot(clock)
            subprocess_verified = True
            await execute(Control(operation="resume"))
        for process in result["woken_process_ids"]:
            await sleep(process)
    snapshot = await service.snapshot(clock)
    assert [e["now_seconds"] for e in delivered[:2]] == [7, 60]
    assert [e["now_seconds"] for e in delivered] == sorted(e["now_seconds"] for e in delivered)
    assert all(e["event"]["at_seconds"] == e["now_seconds"] for e in delivered)
    assert delivered[-1]["now_seconds"] == seconds - 1
    assert snapshot["now_seconds"] == seconds and snapshot["status"] == "horizon"
    assert snapshot["event_counts"]["pending"] == 2 + len(spec.processes)
    assert {e["event_key"] for e in snapshot["pending_events"]} >= {"at-end", "after-end"}
    assert len(delivered) == seconds // 3600 + 2
    return {
        "clock_id": str(clock),
        "kind": "supplied_clock_component_control_no_business_actions",
        "simulated_seconds": seconds,
        "simulated_days": seconds / 86400,
        "owner_pairs": pairs,
        "lane_count": len(spec.processes),
        "delivered_events": len(delivered),
        "event_delivery_lateness_seconds": 0,
        "parallel_work_completion_seconds": [7, 60],
        "simulated_start": spec.epoch.isoformat(),
        "simulated_end": snapshot["simulated_at"],
        "elapsed_wall_seconds": round(time.monotonic() - start, 3),
        "same_original_step_replayed_in_independent_process": subprocess_verified,
        "pause_and_resume_invocations": 1,
        "pending_event_count_at_cutoff": snapshot["event_counts"]["pending"],
        "queue_drained": snapshot["queue_drained"],
        "task_success": None,
        "contract_hash": snapshot["contract_hash"],
        "contract": snapshot["contract"],
        "provider_calls": 0,
        "provider_cost_usd": "0",
        "passed": True,
    }


async def main(args):
    if args.check_paused_clock:
        return await child_check(args)
    configured = test_dsn()
    schema = "clock_validation_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(configured, autocommit=True) as owner:
        await owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(configured, options=f"-c search_path={schema}")
            await migrate(dsn)
            async with AsyncConnectionPool(
                dsn, min_size=1, max_size=4, kwargs={"row_factory": dict_row}
            ) as pool:
                controls = [
                    await control(pool, schema, seconds, args.pairs)
                    for seconds in [3600, 86400, 604800]
                ]
        finally:
            await owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    result = {
        "date": datetime.now(UTC).date().isoformat(),
        "source_hash": source_fingerprint(ROOT),
        "clock_contract_version": "elapsed-seconds-v1",
        "controls": controls,
        "passed": all(x["passed"] for x in controls),
        "owned_schema_removed": True,
        "development_database_touched": False,
        "new_provider_calls": 0,
        "new_cost_usd": "0",
        "legacy_window_runtime_modified": False,
        "model_or_world_adapter_integrated": False,
        "medium_chaos_experiment_started": False,
        "limits": [
            "Supplied Clock component decisions only; no model autonomy evidence.",
            "No payment, shopping, mail service, shared object or BOLD OS integration.",
            "Hour events are explicit control inputs, not a calibrated medium-chaos profile.",
            "Clock IDs belong to a removed test schema; they are not console run URLs.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "controls": [
                    {
                        k: x[k]
                        for k in [
                            "simulated_seconds",
                            "lane_count",
                            "delivered_events",
                            "elapsed_wall_seconds",
                        ]
                    }
                    for x in controls
                ],
                "provider_calls": 0,
                "output": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/simulation-clock-control-results.json"
    )
    parser.add_argument("--pairs", type=int, choices=range(1, 16), default=15)
    parser.add_argument("--check-paused-clock", help=argparse.SUPPRESS)
    parser.add_argument("--schema", help=argparse.SUPPRESS)
    parser.add_argument("--replay-request", help=argparse.SUPPRESS)
    asyncio.run(main(parser.parse_args()))
