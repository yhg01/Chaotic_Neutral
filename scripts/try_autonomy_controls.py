"""Run a predeclared repeated control matrix with real PostgreSQL and Chrome.

No model mode, provider key, new campaign or budget change is available here.
Each case gets new identities. Every failure is retained in the batch report.
"""

import argparse
import asyncio
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from agentrpg.autonomy_validation import (
    AcceptancePolicy,
    acceptance_report,
    collect_run,
    markdown_report,
)
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.director import Director
from agentrpg.main import create_app
from agentrpg.migrate import migrate
from agentrpg.shop import ShopService
from agentrpg.shop_demo import shopping_scenario
from agentrpg.shop_runtime import INSTRUCTIONS, run_shop
from agentrpg.supervision import RuntimeSupervisor, SupervisionPolicy
from agentrpg.transfer_demo import run_local_check
from agentrpg.transfer_task_demo import run_lifecycle_case
from agentrpg.trial_audit import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "local-payment/baseline",
    "local-lifecycle/ack",
    "local-lifecycle/amount",
    "local-lifecycle/cancel",
    "shopping/3",
)


async def main(args):
    origin = urlsplit(args.origin)
    try:
        local = origin.hostname == "localhost" or ipaddress.ip_address(origin.hostname).is_loopback
    except ValueError:
        local = False
    if (
        not local
        or origin.scheme != "http"
        or origin.username
        or origin.password
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise SystemExit("Use a plain loopback HTTP origin without a path or credential.")
    # ShopBrowser owns a separate Chrome context per Agent.
    async with httpx.AsyncClient() as client:
        health = await client.get(args.origin.rstrip("/") + "/health", timeout=5)
        health.raise_for_status()
    settings = Settings()
    database_url = settings.database_url.get_secret_value()
    await migrate(database_url)
    app = create_app(settings)
    folder = ROOT / ".local/autonomy-trials" / str(uuid4())
    plan = {
        "schema_version": 1,
        "mode": "supplied_control",
        "repeats": args.repeats,
        "cases": CASES,
        "source_hash": source_fingerprint(ROOT),
        "provider_calls_allowed": 0,
    }
    save_private(folder / "plan.json", plan)
    runs, errors = [], []

    def save():
        report = acceptance_report(runs, AcceptancePolicy(), expected_control_cases=CASES)
        report["plan"] = plan
        report["execution_errors"] = errors
        report["matrix_complete"] = len(runs) == args.repeats * len(CASES) and not errors
        report["source_unchanged"] = plan["source_hash"] == source_fingerprint(ROOT)
        report["local_control_repeats_passed"] &= (
            report["matrix_complete"] and report["source_unchanged"]
        )
        save_private(folder / "report.json", report)
        (folder / "report.md").write_text(markdown_report(report))
        return report

    async with app.router.lifespan_context(app):
        for repeat in range(args.repeats):
            for name in CASES:
                profile, case = name.split("/")
                run_id = None
                print(f"Repeat {repeat + 1}/{args.repeats}: {name}", flush=True)
                try:
                    if source_fingerprint(ROOT) != plan["source_hash"]:
                        raise RuntimeError("source_changed")
                    if profile == "local-payment":
                        original = await run_local_check(app)
                    elif profile == "local-lifecycle":
                        original = await run_lifecycle_case(app, case)
                    else:
                        director = Director(app.state.pool)
                        manifest = await director.create_run(shopping_scenario(3))
                        run_id = UUID(manifest["run_id"])
                        manifest["configuration"] = {
                            "mode": "control",
                            "rounds": 3,
                            "instructions": INSTRUCTIONS,
                            "runtime_revision": "autonomy-control-matrix-v1",
                            "supervision": SupervisionPolicy().model_dump(),
                            "step_interval_seconds": 0,
                        }
                        save_private(folder / f"{run_id}.credentials.json", manifest)
                        await ShopService(app.state.pool).enable(run_id)
                        await RuntimeSupervisor(app.state.pool).bind(run_id, SupervisionPolicy())
                        await director.control(run_id, "start")
                        original = await run_shop(
                            app.state.pool,
                            database_url,
                            args.origin.rstrip("/"),
                            manifest,
                            restart_every=3,
                            inject=True,
                        )
                    run_id = UUID(original["run_id"])
                    save_private(folder / f"{run_id}.json", original)
                    evidence = await collect_run(
                        app.state.pool,
                        run_id,
                        profile=profile,
                        case=case,
                        local_checks=original["checks"],
                    )
                    evidence["repeat"] = repeat + 1
                    runs.append(evidence)
                except Exception as error:
                    # Exceptions may contain private request data. Retain only their type here;
                    # the original run/jobs and graph checkpoints remain in PostgreSQL.
                    errors.append(
                        {
                            "repeat": repeat + 1,
                            "case": name,
                            "run_id": str(run_id) if run_id else None,
                            "error_type": type(error).__name__,
                        }
                    )
                save()
    report = save()
    print(
        json.dumps(
            {
                "report": str(folder / "report.json"),
                "runs": len(runs),
                "local_control_repeats_passed": report["local_control_repeats_passed"],
                "model_repeat_gate_passed": report["model_repeat_gate_passed"],
            }
        ),
        flush=True,
    )
    if not report["local_control_repeats_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, choices=range(3, 11), default=3)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    asyncio.run(main(parser.parse_args()))
