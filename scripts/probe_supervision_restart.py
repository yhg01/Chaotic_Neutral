"""Free live control: terminate our worker during durable backoff, then resume it.

Creates one isolated simulation in the development database, never resets data,
never selects a model mode and never reads or transmits a provider credential.
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.budget import CampaignLedger
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.director import row, rows

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = UUID("635aa42e-386f-43dc-b679-b73cd0cd276f")


async def main():
    children = []

    async def launch(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "scripts/try_browser_shop.py"),
            *args,
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        children.append(process)
        return process

    try:
        async with AsyncConnectionPool(
            Settings().database_url.get_secret_value(),
            min_size=1,
            max_size=2,
            kwargs={"row_factory": dict_row},
        ) as pool:
            before = await CampaignLedger(pool).summary(CAMPAIGN, recent_limit=0)
            first = await launch(
                "--mode",
                "control",
                "--rounds",
                "30",
                "--supervise",
                "--retry-delay-seconds",
                "8",
                "--inject-checkout-interruption",
            )
            async with asyncio.timeout(120):
                while True:
                    line = (await first.stdout.readline()).decode().strip()
                    if line.startswith("Run: "):
                        run = UUID(line.removeprefix("Run: "))
                        break
                    if not line and first.returncode is not None:
                        raise RuntimeError("The control worker did not create a run.")
                while True:
                    async with pool.connection() as conn:
                        pending = await row(
                            conn, "SELECT * FROM run_supervision WHERE run_id=%s", (run,)
                        )
                    if pending and pending["retry_due_at"] is not None:
                        break
                    if first.returncode is not None:
                        raise RuntimeError("The control stopped before saving a retry deadline.")
                    await asyncio.sleep(0.1)
            first.terminate()
            await asyncio.wait_for(first.wait(), 5)
            print(
                f"Stopped owned worker {first.pid}; saved run {run} and retry deadline.", flush=True
            )
            folder = ROOT / ".local/browser-shop-runs" / str(run)
            second = await launch(
                "--resume", str(folder / "credentials.json"), "--restart-every", "6"
            )
            print(
                f"Started new worker {second.pid}; using the saved deadline and job IDs.",
                flush=True,
            )
            async with asyncio.timeout(300):
                while line := await second.stdout.readline():
                    text = line.decode().strip()
                    if text.startswith("Window "):
                        print(text, flush=True)
                await second.wait()
            if second.returncode:
                raise RuntimeError("The resumed control failed; its checkpoint is retained.")
            report = json.loads((folder / "report.json").read_text())
            async with pool.connection() as conn:
                events = await rows(
                    conn,
                    "SELECT kind,payload,created_at FROM run_events WHERE run_id=%s "
                    "AND kind IN ('recovery_scheduled','recovery_resumed') ORDER BY created_at",
                    (run,),
                )
                provider = await row(
                    conn, "SELECT count(*) AS n FROM model_spend_attempts WHERE run_id=%s", (run,)
                )
            resumed = [e for e in events if e["kind"] == "recovery_resumed"]
            after = await CampaignLedger(pool).summary(CAMPAIGN, recent_limit=0)
            checks = {
                "new_process": first.pid != second.pid,
                "one_original_deadline": sum(e["kind"] == "recovery_scheduled" for e in events)
                == 1,
                "no_early_resume": len(resumed) == 1
                and resumed[0]["created_at"] >= pending["retry_due_at"],
                "one_recovery": report["supervision"]["recoveries"] == 1,
                "thirty_purchases_pass": report["passed"],
                "zero_provider_attempts": provider["n"] == 0,
                "campaign_unchanged": before == after,
            }
            evidence = {
                "date": datetime.now(UTC).isoformat(),
                "run_id": str(run),
                "first_process_id": first.pid,
                "first_exit_code": first.returncode,
                "second_process_id": second.pid,
                "second_exit_code": second.returncode,
                "original_retry_deadline": pending["retry_due_at"].isoformat(),
                "events": events,
                "checks": checks,
                "campaign_before": before,
                "campaign_after": after,
                "scope": "Supplied decisions with actual Agent graph, Chrome, HTTP "
                "and PostgreSQL; no provider calls.",
            }
            save_private(folder / "restart-probe.json", evidence)
            print(json.dumps({"run_id": str(run), "checks": checks}, indent=2), flush=True)
            if not all(checks.values()):
                raise SystemExit(1)
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()


if __name__ == "__main__":
    asyncio.run(main())
