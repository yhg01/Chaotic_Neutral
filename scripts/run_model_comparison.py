"""Run a prepared role-swap matrix under its original cumulative campaign.

Execution requires --execute. Partial trials resume their original run and IDs;
they are never replaced or counted as new samples. Raw worker output stays private.
"""

import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.budget import CampaignLedger
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.funding_scenario import comparison_assignments, funding_scenario
from agentrpg.funding_validation import funding_report
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.trial_audit import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def validate_plan(plan, root=ROOT, *, check_source=True):
    if (
        plan.get("scenario_hash") != funding_scenario().content_hash()
        or plan.get("assignments") != comparison_assignments()
        or (check_source and plan.get("source_hash") != source_fingerprint(root))
        or plan.get("actor_model") != "openai/gpt-4.1"
        or plan.get("repeats_per_assignment") != 3
    ):
        raise ValueError(
            "The plan, scenario, roles or source changed. Preserve it and prepare a new plan."
        )
    UUID(str(plan["campaign"]["id"]))


def comparison_folder(plan_path, root=ROOT):
    folder = plan_path.resolve().parent
    if not folder.is_relative_to((root / ".local/model-comparisons").resolve()):
        raise ValueError("Use the prepared local comparison directory.")
    return folder


def validate_progress(progress, plan):
    if any(
        progress.get(k) != value
        for k, value in (
            ("scenario_hash", plan["scenario_hash"]),
            ("source_hash", plan["source_hash"]),
            ("campaign_id", str(plan["campaign"]["id"])),
        )
    ):
        raise SystemExit("The saved comparison belongs to another frozen plan.")


async def recover_result(args, plan):
    """Recover reporting only. Never execute or relax the source guard for generation."""
    validate_plan(plan, check_source=False)
    folder = comparison_folder(args.plan, ROOT)
    manifest_path = ROOT / ".local/scenario-runs" / str(args.recover_result) / "credentials.json"
    manifest = json.loads(manifest_path.read_text())
    config = manifest["configuration"]
    if (
        config.get("budget", {}).get("campaign_id") != str(plan["campaign"]["id"])
        or config.get("actor_model") != plan["actor_model"]
    ):
        raise SystemExit("The saved run does not belong to this comparison campaign.")
    progress_path = folder / "progress.json"
    progress = json.loads(progress_path.read_text())
    validate_progress(progress, plan)
    matching = [
        t
        for t in progress["trials"]
        if t["assignment"]["owner_models"] == config.get("owner_models")
        and t.get("run_id") in {None, str(args.recover_result)}
    ]
    if len(matching) != 1:
        raise SystemExit("The interrupted trial cannot be identified uniquely.")
    async with AsyncConnectionPool(
        Settings().database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"row_factory": dict_row},
    ) as pool:
        async with pool.connection() as conn:
            audit = await (
                await conn.execute(
                    "SELECT payload FROM run_events WHERE run_id=%s AND kind='trial_started'",
                    (args.recover_result,),
                )
            ).fetchall()
        if not audit or any(a["payload"].get("source_hash") != plan["source_hash"] for a in audit):
            raise SystemExit("The saved trial source does not match its original plan.")
        matching[0].update(
            await funding_report(pool, args.recover_result),
            resume_file=str(manifest_path.relative_to(ROOT)),
        )
        budget = await CampaignLedger(pool).summary(plan["campaign"]["id"], recent_limit=0)
        progress["campaign"] = {
            k: budget[k]
            for k in (
                "id",
                "limit_usd",
                "attempts",
                "reported_usd",
                "uncertain_usd",
                "remaining_usd",
            )
        }
    progress["report_recovery"] = {
        "mode": "read_only_no_model_calls",
        "original_source_hash": plan["source_hash"],
        "reporter_source_hash": source_fingerprint(ROOT),
    }
    save_private(progress_path, progress)
    print(
        json.dumps(
            {
                "recovered": str(args.recover_result),
                "report": str(progress_path),
                "status": matching[0]["run_status"],
                "model_calls_added": 0,
            }
        )
    )


def trial_command(plan, assignment, scenario_path, *, resume=None):
    command = [sys.executable, str(ROOT / "scripts/run_scenario.py")]
    if resume:
        command += ["--resume", str(resume)]
    else:
        command += [
            "--scenario",
            str(scenario_path),
            "--actors",
            "model",
            "--max-model-calls",
            "12",
            "--timeout-seconds",
            "180",
        ]
    command += [
        "--model",
        "openai/gpt-4.1-mini",
        "--actor-model",
        plan["actor_model"],
        "--campaign",
        str(plan["campaign"]["id"]),
        "--budget-usd",
        str(plan["campaign"]["limit_usd"]),
    ]
    for owner, model in sorted(assignment["owner_models"].items()):
        command += ["--owner-model", f"{owner}={model}"]
    return command


async def main(args):
    plan = json.loads(args.plan.read_text())
    if args.recover_result:
        if args.execute:
            raise SystemExit("Result recovery cannot execute a model.")
        await recover_result(args, plan)
        return
    validate_plan(plan)
    if not args.execute:
        print("Prepared plan is valid. No execution. Use --execute to make bounded model calls.")
        return
    folder = comparison_folder(args.plan, ROOT)
    scenario_path = (ROOT / plan["scenario_file"]).resolve()
    if (
        not folder.resolve().is_relative_to((ROOT / ".local/model-comparisons").resolve())
        or scenario_path != folder.resolve() / "scenario.json"
    ):
        raise SystemExit("Use the prepared local comparison directory.")
    if (
        funding_scenario().content_hash()
        != ScenarioSpec.model_validate_json(scenario_path.read_text()).content_hash()
    ):
        raise SystemExit("The frozen scenario file changed.")
    progress_path = folder / "progress.json"
    progress = (
        json.loads(progress_path.read_text())
        if progress_path.exists()
        else {
            "scenario_hash": plan["scenario_hash"],
            "source_hash": plan["source_hash"],
            "campaign_id": str(plan["campaign"]["id"]),
            "trials": [],
        }
    )
    validate_progress(progress, plan)
    async with AsyncConnectionPool(
        Settings().database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"row_factory": dict_row},
    ) as pool:
        ledger = CampaignLedger(pool)
        budget = await ledger.summary(plan["campaign"]["id"], recent_limit=0)
        if budget["limit_usd"] != Decimal(str(plan["campaign"]["limit_usd"])):
            raise SystemExit("The shared campaign ceiling changed.")
        processed = 0
        for repeat in range(3):
            for assignment in plan["assignments"]:
                key = f"{repeat + 1}:{assignment['id']}"
                prior = next((t for t in progress["trials"] if t["key"] == key), None)
                if prior and prior.get("run_status") == "completed":
                    continue
                if processed >= args.max_runs or budget["remaining_usd"] <= 0:
                    break
                validate_plan(plan)
                resume = ROOT / prior["resume_file"] if prior and prior.get("resume_file") else None
                if prior and resume is None:
                    raise SystemExit(
                        "An earlier setup failure needs inspection; it was not replaced."
                    )
                if prior and prior.get("run_id"):
                    await funding_report(
                        pool, prior["run_id"]
                    )  # Validate the saved scenario before paying.
                trial = prior or {
                    "key": key,
                    "repeat": repeat + 1,
                    "assignment": assignment,
                    "attempts": [],
                }
                if prior is None:
                    progress["trials"].append(trial)
                trial["run_status"] = "launching"
                save_private(progress_path, progress)
                worker = await asyncio.create_subprocess_exec(
                    *trial_command(plan, assignment, scenario_path, resume=resume),
                    cwd=ROOT,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                started = time.monotonic()
                try:
                    raw, _ = await worker.communicate()
                except BaseException:
                    if worker.returncode is None:
                        worker.terminate()
                        await asyncio.wait_for(worker.wait(), 5)
                    trial["run_status"] = "interrupted_needs_inspection"
                    save_private(progress_path, progress)
                    raise
                log = folder / f"{key.replace(':', '-')}.attempt-{uuid4()}.log"
                # Private files may include synthetic dialogue and tracebacks. Do not print them.
                log.touch(mode=0o600, exist_ok=False)
                log.write_bytes(raw)
                run_id = prior.get("run_id") if prior else None
                for line in raw.decode(errors="replace").splitlines():
                    if line.startswith("Run: "):
                        run_id = str(UUID(line.removeprefix("Run: ")))
                trial["attempts"].append(
                    {
                        "returncode": worker.returncode,
                        "elapsed_seconds": time.monotonic() - started,
                        "private_log": str(log.relative_to(ROOT)),
                    }
                )
                if run_id:
                    result = await funding_report(pool, run_id)
                    trial.update(
                        result, resume_file=f".local/scenario-runs/{run_id}/credentials.json"
                    )
                else:
                    trial.update(run_status="setup_failed", funding_outcome="deadline_not_observed")
                budget = await ledger.summary(plan["campaign"]["id"], recent_limit=0)
                progress["campaign"] = {
                    k: budget[k]
                    for k in (
                        "id",
                        "limit_usd",
                        "attempts",
                        "reported_usd",
                        "uncertain_usd",
                        "remaining_usd",
                    )
                }
                save_private(progress_path, progress)
                processed += 1
                print(
                    json.dumps(
                        {
                            "trial": key,
                            "run_id": run_id,
                            "status": trial["run_status"],
                            "outcome": trial["funding_outcome"],
                            "remaining_usd": str(budget["remaining_usd"]),
                        }
                    ),
                    flush=True,
                )
                if trial["run_status"] != "completed" or not trial.get("environment_checks_passed"):
                    return  # Preserve the checkpoint before another trial can start.
    print(progress_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--recover-result", type=UUID, help="Read-only repair of an interrupted report."
    )
    parser.add_argument("--max-runs", type=int, choices=range(1, 7), default=1)
    asyncio.run(main(parser.parse_args()))
