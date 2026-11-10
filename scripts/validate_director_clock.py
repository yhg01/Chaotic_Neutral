"""Free Director/Clock controls with actual child-process termination recovery.

Uses one owned schema in a separate PostgreSQL test database. No provider is
installed. The supplied outputs establish dispatch mechanics, not model conduct.
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

from agentrpg.director import Director, DirectorError
from agentrpg.director_clock import DirectorClock, TaskOutput
from agentrpg.migrate import migrate
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.simulation_clock import ClockContract, Control, ProcessSpec, Schedule, Sleep
from agentrpg.trial_audit import source_fingerprint

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]


def test_dsn(schema):
    value = os.environ.get("TEST_DATABASE_URL", "")
    if not value or "test" not in conninfo_to_dict(value).get("dbname", "").lower():
        raise ValueError("A separate TEST_DATABASE_URL containing test is required.")
    if not re.fullmatch(r"director_clock_validation_[0-9a-f]{32}", schema):
        raise ValueError("An owned Director Clock validation schema is required.")
    return make_conninfo(value, options=f"-c search_path={schema}")


async def worker(args):
    async with AsyncConnectionPool(test_dsn(args.schema), kwargs={"row_factory": dict_row}) as pool:
        service = DirectorClock(pool)

        async def execute(packet):
            if args.worker == "running":
                print("dispatch-entered", flush=True)
                await asyncio.Event().wait()
            return TaskOutput(
                text="Saved local control reply.", output_tokens=10, until_seconds=args.until
            )

        if args.worker == "saved":

            async def park(*unused):
                print("output-saved", flush=True)
                await asyncio.Event().wait()

            service.arm = park
        await service.dispatch(UUID(args.run), UUID(args.job), execute)


async def terminate_worker(schema, run, job, mode, until):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        "--worker",
        mode,
        "--schema",
        schema,
        "--run",
        str(run),
        "--job",
        str(job),
        "--until",
        str(until),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(child.stdout.readline(), 20)
        assert line.strip() == (b"output-saved" if mode == "saved" else b"dispatch-entered")
    finally:
        if child.returncode is None:
            child.kill()
        stdout, stderr = await child.communicate()
    if stderr:
        folder = ROOT / ".local/director-clock"
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"worker-{mode}-stderr.log"
        p.write_bytes(stderr)
        p.chmod(0o600)
    return {"phase": mode, "owned_child_terminated": True, "return_code": child.returncode}


async def control(pool, schema, pairs, horizon, faults):
    start = time.monotonic()
    spec = vehicle_scenario(pairs)
    run = UUID((await Director(pool).create_run(spec))["run_id"])
    service = DirectorClock(pool)
    contract = ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=horizon,
        output_tokens_per_minute=600,
        processes=tuple(
            ProcessSpec(id=f"{role}-{i}", owner_id=o.id, role=role)
            for i, o in enumerate(spec.owners)
            for role in ["actor", "agent"]
        ),
    )
    clock = await service.bind(run, contract)
    failures = []
    steps = 0
    delivery = None

    async def response(packet):
        return TaskOutput(
            text="",
            output_tokens=10,
            until_seconds=horizon,
            interrupt_on=("reminder", "owner_message", "assistant_reply"),
        )

    if faults:
        for p in contract.processes:
            if p.id != "agent-0":
                await service.control(
                    run,
                    uuid4(),
                    Sleep(
                        process_id=p.id,
                        until_seconds=horizon,
                        interrupt_on=("reminder", "owner_message", "assistant_reply"),
                    ),
                )
        first = await service.prepare(run, "agent-0", uuid4())
        failures.append(await terminate_worker(schema, run, first["id"], "saved", horizon))
        assert (await DirectorClock(pool).recover(run, first["id"]))["status"] == "saved"
        await service.control(run, uuid4(), Control(operation="pause"))
        assert (await service.arm(run, first["id"]))["status"] == "saved"
        assert (await service.step(run, uuid4()))["reason"] == "paused"
        await service.control(run, uuid4(), Control(operation="resume"))
        await service.arm(run, first["id"])
        request = uuid4()
        delivery = await service.step(run, request)
        steps += 1
        assert (
            await DirectorClock(pool).step(run, request) == delivery
            and delivery["now_seconds"] == 1
        )
        await service.step(run, uuid4())
        steps += 1
        second = await service.prepare(run, "actor-0", uuid4())
        failures.append(await terminate_worker(schema, run, second["id"], "running", horizon))
        assert (await service.recover(run, second["id"]))["status"] == "uncertain"
        blocked = await service.step(run, uuid4())
        assert (
            blocked["reason"] == "ready_process_requires_decision" and blocked["now_seconds"] == 1
        )
        try:
            await service.dispatch(run, second["id"], response)
        except DirectorError:
            pass
        else:
            raise AssertionError("Unknown dispatch was retried.")
        await service.reconcile(
            run,
            second["id"],
            await response(second["input"]),
            "supplied-local-recovery-fixture-no-provider",
        )
        await service.arm(run, second["id"])
    for hour in range(1, horizon // 3600):
        await service.control(
            run,
            uuid4(),
            Schedule(
                event_key=f"hour-{hour:04}",
                at_seconds=hour * 3600,
                kind="reminder",
                recipients=(f"agent-{hour % pairs}",),
            ),
        )
    for name, at in [
        ("before-cutoff", horizon - 1),
        ("at-cutoff", horizon),
        ("after-cutoff", horizon + 1),
    ]:
        await service.control(
            run,
            uuid4(),
            Schedule(event_key=name, at_seconds=at, kind="reminder", recipients=("agent-0",)),
        )
    for _ in range(2000):
        state = await service.clock.snapshot(clock)
        for lane in state["processes"]:
            if lane["state"] != "ready":
                continue
            if state["now_seconds"] >= horizon - 1:
                await service.control(
                    run, uuid4(), Sleep(process_id=lane["process_id"], until_seconds=horizon + 1)
                )
            else:
                task = await service.prepare(run, lane["process_id"], uuid4())
                await service.dispatch(run, task["id"], response)
        result = await service.step(run, uuid4())
        steps += 1
        assert result["reason"] in {"event_delivered", "horizon_cutoff"}, result["reason"]
        if result["reason"] == "horizon_cutoff":
            break
    else:
        raise AssertionError("Supplied control exceeded its finite step bound.")
    snapshot = await service.snapshot(run)
    clockstate = await service.clock.snapshot(clock)
    assert snapshot["now_seconds"] == horizon and snapshot["status"] == "horizon"
    assert all(j["status"] == "applied" and j["attempts"] == 1 for j in snapshot["jobs"])
    assert {e["event_key"] for e in clockstate["pending_events"]} >= {"at-cutoff", "after-cutoff"}
    chat = await Director(pool).messages(run, spec.owners[0].id)
    assert len(chat) == (1 if faults else 0)
    return {
        "run_id": str(run),
        "clock_id": clock,
        "profile": "supplied_director_dispatch_control",
        "owner_pairs": pairs,
        "simulated_seconds": horizon,
        "elapsed_wall_seconds": round(time.monotonic() - start, 3),
        "jobs": len(snapshot["jobs"]),
        "dispatch_attempts": sum(j["attempts"] for j in snapshot["jobs"]),
        "clock_steps": steps,
        "private_messages_saved": len(chat),
        "faults": failures,
        "saved_reply_delivery_second": delivery["now_seconds"] if delivery else None,
        "same_step_replayed_without_duplicate_message": bool(faults),
        "unknown_attempt_blocked_until_supplied_reconciliation": bool(faults),
        "pending_events_at_horizon": clockstate["event_counts"]["pending"],
        "model_provider_calls": 0,
        "new_cost_usd": "0",
        "task_success": None,
        "passed": True,
    }


async def main(args):
    if args.worker:
        return await worker(args)
    schema = "director_clock_validation_" + uuid4().hex
    dsn = test_dsn(schema)
    async with await psycopg.AsyncConnection.connect(
        os.environ["TEST_DATABASE_URL"], autocommit=True
    ) as owner:
        await owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            await migrate(dsn)
            async with AsyncConnectionPool(
                dsn, min_size=1, max_size=40, kwargs={"row_factory": dict_row}
            ) as pool:
                controls = [
                    await control(pool, schema, 2, 3600, True),
                    await control(pool, schema, 15, 604800, False),
                ]
        finally:
            await owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    result = {
        "date": "2026-11-09",
        "source_hash": source_fingerprint(ROOT),
        "controls": controls,
        "passed": all(c["passed"] for c in controls),
        "owned_schema_removed": True,
        "development_database_touched": False,
        "provider_calls": 0,
        "new_cost_usd": "0",
        "limits": [
            "Supplied dispatch and private chat only; no live provider adapter.",
            "Unknown recovery uses a labelled local fixture, not provider reconciliation.",
            "No timed transfer/shop/mail tools, elapsed recurring compiler "
            "or medium-chaos calibration.",
            "Run IDs belong to a deleted test schema, not console trajectory URLs.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/director-clock-control-results.json"
    )
    parser.add_argument("--worker", choices=["running", "saved"], help=argparse.SUPPRESS)
    for name in ["schema", "run", "job"]:
        parser.add_argument("--" + name, help=argparse.SUPPRESS)
    parser.add_argument("--until", type=int, help=argparse.SUPPRESS)
    asyncio.run(main(parser.parse_args()))
