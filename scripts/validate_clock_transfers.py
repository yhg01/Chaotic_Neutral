"""Free timed-transfer controls, including process death before/after actual commit.

All credits and owner responses are simulated. Only an owned schema in a separate
PostgreSQL test database is used; no provider configuration or real bank is used.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.clock_transfers import TimedActorOutput, TimedTransfers
from agentrpg.director import Director, row
from agentrpg.director_clock import DirectorClock
from agentrpg.migrate import migrate
from agentrpg.simulation_clock import ClockContract, ProcessSpec, Sleep
from agentrpg.transfer_demo import scenario
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.trial_audit import source_fingerprint
from agentrpg.world import WorldService

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]


def test_dsn(schema):
    value = os.environ.get("TEST_DATABASE_URL", "")
    if not value or "test" not in conninfo_to_dict(value).get("dbname", "").lower():
        raise ValueError("A separate TEST_DATABASE_URL containing test is required.")
    if not re.fullmatch(r"clock_transfer_validation_[0-9a-f]{32}", schema):
        raise ValueError("An owned timed-transfer validation schema is required.")
    return make_conninfo(value, options=f"-c search_path={schema}")


async def raw_state(pool, run):
    # Ordinary MVCC reads remain available while the child holds its uncommitted effect.
    async with pool.connection() as conn:
        balances = await (
            await conn.execute(
                "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s", (run,)
            )
        ).fetchall()
        count = await row(
            conn, "SELECT count(*) AS n FROM credit_transfers WHERE run_id=%s", (run,)
        )
        mail = await row(conn, "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s", (run,))
    return {
        "balances": {r["owner_id"]: r["balance"] for r in balances},
        "payments": count["n"],
        "bank_notices": mail["n"],
    }


async def worker(args):
    async with AsyncConnectionPool(test_dsn(args.schema), kwargs={"row_factory": dict_row}) as pool:
        if args.worker == "before_commit":
            original = TransferService.transfer

            async def uncommitted(self, *values, **kwargs):
                receipt = await original(self, *values, **kwargs)
                print("payment-uncommitted", flush=True)
                await asyncio.Event().wait()
                return receipt

            TransferService.transfer = uncommitted
        await DirectorClock(pool).step(UUID(args.run), UUID(args.step))
        print("payment-committed", flush=True)
        await asyncio.Event().wait()


async def kill_control_child(pool, schema, run, step, mode):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        "--worker",
        mode,
        "--schema",
        schema,
        "--run",
        str(run),
        "--step",
        str(step),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(child.stdout.readline(), 20)
        assert line.strip() == (
            b"payment-uncommitted" if mode == "before_commit" else b"payment-committed"
        )
        visible = await raw_state(pool, run)
        expected = 0 if mode == "before_commit" else 1
        assert visible["payments"] == visible["bank_notices"] == expected
        assert visible["balances"] == (
            {"alice": 100, "bob": 20} if not expected else {"alice": 90, "bob": 30}
        )
    finally:
        if child.returncode is None:
            child.kill()
        stdout, stderr = await child.communicate()
    if stderr:
        folder = ROOT / ".local/clock-transfer"
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"worker-{mode}-stderr.log"
        p.write_bytes(stderr)
        p.chmod(0o600)
    return {
        "phase": mode,
        "owned_child_terminated": True,
        "return_code": child.returncode,
        "externally_visible_before_kill": visible,
    }


async def export_trace(pool, run, clock, report):
    tables = {}
    queries = [
        ("environment_runs", "id", run),
        ("run_participants", "run_id", run),
        ("run_events", "run_id", run),
        ("owner_messages", "run_id", run),
        ("director_clock_jobs", "run_id", run),
        ("simulation_clocks", "id", clock),
        ("simulation_clock_processes", "clock_id", clock),
        ("simulation_clock_events", "clock_id", clock),
        ("simulation_clock_operations", "clock_id", clock),
        ("director_clock_deliveries", "clock_id", clock),
        ("clock_transfer_profiles", "run_id", run),
        ("clock_transfer_operations", "run_id", run),
        ("clock_transfer_calls", "run_id", run),
        ("transfer_grants", "run_id", run),
        ("credit_transfers", "run_id", run),
        ("world_requests", "run_id", run),
        ("world_ledger", "run_id", run),
        ("inbox_messages", "run_id", run),
        ("inbox_receipts", "run_id", run),
    ]
    async with pool.connection() as conn:
        for table, column, value in queries:
            tables[table] = await (
                await conn.execute(
                    sql.SQL("SELECT * FROM {} WHERE {}=%s").format(
                        sql.Identifier(table), sql.Identifier(column)
                    ),
                    (value,),
                )
            ).fetchall()
    folder = ROOT / ".local/clock-transfer/traces" / str(run)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "trace.json"
    p.write_text(json.dumps({"report": report, "tables": tables}, indent=2, default=str) + "\n")
    p.chmod(0o600)


async def control(pool, schema, case):
    data = scenario().model_dump(mode="json")
    data["owners"][0]["request"] = "Repay Bob 10 simulated credits for lunch, once."
    data["owners"][1]["request"] = "Check the actual incoming transfer and bank notice."
    if case == "insufficient_funds":
        data["world"]["balances"]["alice"] = 5
    spec = type(scenario()).model_validate(data)
    manifest = await Director(pool).create_run(spec)
    run = UUID(manifest["run_id"])
    director = DirectorClock(pool)
    transfers = TimedTransfers(pool)
    horizon = 5 if case == "horizon" else 100
    contract = ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=horizon,
        output_tokens_per_minute=600,
        tool_seconds={"transfer_credits": 7},
        processes=tuple(
            ProcessSpec(id=f"{role}-{o.id}", owner_id=o.id, role=role)
            for o in spec.owners
            for role in ["actor", "agent"]
        ),
    )
    clock = await director.bind(run, contract)
    await transfers.enable(run)
    for lane in contract.processes:
        if lane.id != "actor-alice":
            await director.control(
                run,
                uuid4(),
                Sleep(
                    process_id=lane.id,
                    until_seconds=120,
                    interrupt_on=("owner_message", "transfer_completed", "inbox_received"),
                ),
            )
    task = await director.prepare(run, "actor-alice", uuid4())

    async def owner(packet):
        return TimedActorOutput(
            text="I authorize one 10-credit repayment to Bob for lunch.",
            output_tokens=10,
            until_seconds=120,
            transfer_consent={"recipient_owner_id": "bob", "amount": 10, "memo": "Lunch repayment"},
        )

    await director.dispatch(run, task["id"], owner)
    await director.step(run, uuid4())
    await director.step(run, uuid4())
    grant = TransferArguments(
        authorization_id=uuid5(UUID(task["id"]), "clock-transfer-consent"),
        recipient_owner_id="bob",
        amount=10,
        memo="Lunch repayment",
    )
    request = uuid4()
    admission = await transfers.schedule(run, "agent-alice", request, grant)
    assert admission["status"] == "pending" and admission["due_seconds"] == 8
    initial = await raw_state(pool, run)
    assert initial["payments"] == initial["bank_notices"] == 0
    step = uuid4()
    fault = None
    if case in {"before_commit", "after_commit"}:
        fault = await kill_control_child(pool, schema, run, step, case)
    result = await DirectorClock(pool).step(run, step)
    repeated = await asyncio.gather(*(DirectorClock(pool).step(run, step) for _ in range(8)))
    assert all(r == result for r in repeated)
    assert await transfers.schedule(run, "agent-alice", request, grant) == admission
    assert await transfers.schedule(run, "agent-alice", uuid4(), grant) == admission
    actual = await transfers.operation(run, "agent-alice", admission["id"])
    final = await raw_state(pool, run)
    if case in {"before_commit", "after_commit"}:
        assert actual["status"] == "applied" and actual["result"]["request_id"] == admission["id"]
        assert actual["result"]["simulated_seconds"] == 8 and final == {
            "balances": {"alice": 90, "bob": 30},
            "payments": 1,
            "bank_notices": 1,
        }
    elif case == "insufficient_funds":
        assert actual["status"] == "rejected" and final == initial
    else:
        assert (
            actual["status"] == "pending"
            and result["reason"] == "horizon_cutoff"
            and final == initial
        )
    world = WorldService(pool)
    assert world.replay(await world.journal(run)) == await world.snapshot(run)
    report = {
        "case": case,
        "run_id": str(run),
        "clock_id": clock,
        "scenario_hash": spec.content_hash(),
        "due_seconds": 8,
        "final_simulated_seconds": result["now_seconds"],
        "fault": fault,
        "admission": admission,
        "operation": actual,
        "initial": initial,
        "final": final,
        "same_and_new_call_ids_share_original_operation": True,
        "eight_step_replays_preserve_result": True,
        "ledger_replay_matches": True,
        "provider_calls": 0,
        "new_cost_usd": "0",
        "passed": True,
    }
    await export_trace(pool, run, clock, report)
    return report


async def main(args):
    if args.worker:
        return await worker(args)
    schema = "clock_transfer_validation_" + uuid4().hex
    dsn = test_dsn(schema)
    async with await psycopg.AsyncConnection.connect(
        os.environ["TEST_DATABASE_URL"], autocommit=True
    ) as owner:
        await owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            await migrate(dsn)
            async with AsyncConnectionPool(
                dsn, min_size=1, max_size=20, kwargs={"row_factory": dict_row}
            ) as pool:
                controls = [
                    await control(pool, schema, case)
                    for case in ["before_commit", "after_commit", "insufficient_funds", "horizon"]
                ]
        finally:
            await owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    report = {
        "date": "2026-11-09",
        "profile": "clock-transfer-v1",
        "source_hash": source_fingerprint(ROOT),
        "controls": controls,
        "passed": all(c["passed"] for c in controls),
        "provider_calls": 0,
        "new_cost_usd": "0",
        "owned_schema_removed": True,
        "development_database_touched": False,
        "limits": [
            "Supplied owner decisions and simulated-credit effects; no live provider or real bank.",
            "Each case uses a separate world. Accepted cases do not repeat one world payment.",
            "Test run IDs are in a removed schema; raw private traces are exported before removal.",
            "Not a seven-day medium-chaos model experiment.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "cases": [
                    {k: c[k] for k in ["case", "final", "due_seconds", "final_simulated_seconds"]}
                    for c in controls
                ],
                "provider_calls": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/clock-transfer-control-results.json"
    )
    parser.add_argument(
        "--worker", choices=["before_commit", "after_commit"], help=argparse.SUPPRESS
    )
    for name in ["schema", "run", "step"]:
        parser.add_argument("--" + name, help=argparse.SUPPRESS)
    asyncio.run(main(parser.parse_args()))
