"""Build the sites and papers a story needs, check them, and freeze them into a scenario.

A build file names a base scenario with a clock, a brief, and optionally a plan of orders.
With no plan, the Director model writes one from the brief. A coder then writes each module
and paper, and code checks each one. Only a build in which every order passed is saved.

  --scripted DIR   read finished modules and papers from DIR. No provider, no database.
  --coder-model M  write them with a model. A paid build needs --campaign or --budget-usd.

The output is one scenario JSON file, plus a folder beside it with the same content as
separate files for a person to read. Read that folder before the first run.
"""

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from author_scenario import save_scenario  # noqa: E402

from agentrpg.actor import ModelGenerationError  # noqa: E402
from agentrpg.agent_model import UsageTracker, create_model  # noqa: E402
from agentrpg.scenario_schema import ScenarioSpec  # noqa: E402
from agentrpg.world_build import (  # noqa: E402
    BuildError,
    BuildPlan,
    ModelCoder,
    ScriptedCoder,
    WorldPlanner,
    assemble,
    build,
    validate_plan,
    write_attempts,
    write_pack,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--build", type=Path, required=True, help="A build file (JSON).")
    parser.add_argument("--output", type=Path, required=True, help="The scenario file to write.")
    parser.add_argument("--pack", type=Path, help="Folder for the readable copy.")
    parser.add_argument("--scripted", type=Path, help="Folder of finished modules and papers.")
    parser.add_argument("--coder-model", help="Model that writes modules and papers.")
    parser.add_argument("--director-model", help="Model that plans, when the file has no plan.")
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--campaign", type=UUID, help="Share an existing model-cost campaign.")
    parser.add_argument("--budget-usd", type=Decimal, help="Total campaign cap in US dollars.")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if (args.scripted is None) == (args.coder_model is None):
        parser.error("Give exactly one of --scripted and --coder-model.")
    if not 0 <= args.max_repairs <= 4:
        parser.error("--max-repairs must be from 0 through 4.")
    if not 1 <= args.timeout_seconds <= 120:
        parser.error("--timeout-seconds must be from 1 through 120.")
    if args.output.suffix.lower() != ".json":
        parser.error("--output must name a JSON file.")
    if args.coder_model and args.budget_usd is None:
        parser.error("A build with a model needs --budget-usd.")
    if args.budget_usd is not None and not (
        args.budget_usd.is_finite() and 0 < args.budget_usd <= 1000
    ):
        parser.error("--budget-usd must be positive and at most 1000.")
    return args


def load_build(path: Path) -> tuple[ScenarioSpec, dict]:
    try:
        build_file = json.loads(path.read_text(encoding="utf-8"))
        base_path = (path.parent / build_file["base"]).resolve()
        base = ScenarioSpec.model_validate_json(base_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, ValueError):
        raise BuildError("The build file or its base scenario cannot be read.") from None
    if base.clock is None:
        raise BuildError("The base scenario has no clock.")
    return base, build_file


def _usage(tracker: UsageTracker) -> dict:
    return {
        "requests": tracker.requests,
        "completed": tracker.completed,
        "failed": tracker.failed,
        "input_tokens": tracker.input_tokens,
        "output_tokens": tracker.output_tokens,
        "reported_cost_usd": round(tracker.reported_cost_usd, 6),
    }


async def execute(args, *, root: Path = ROOT, env: dict | None = None) -> dict:
    output = args.output if args.output.is_absolute() else root / args.output
    if output.exists() and not args.overwrite:
        raise BuildError("The output exists. Select a new path or use --overwrite.")
    base, build_file = load_build(args.build if args.build.is_absolute() else root / args.build)
    pack = args.pack or output.parent / "packs" / output.stem
    pack = pack if pack.is_absolute() else root / pack
    report: dict = {
        "invocation_id": str(uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
        "build": str(args.build),
        "output": str(output),
        "mode": "scripted" if args.scripted else "model",
        "coder_model": args.coder_model,
        "director_model": None,
        "max_repairs": args.max_repairs,
        "outcome": "failed",
        "scenario_saved": False,
    }
    usage = {"coder": UsageTracker(), "director": UsageTracker()}
    try:
        async with AsyncExitStack() as stack:
            coder = None
            make = None
            if args.scripted is not None:
                scripted = args.scripted if args.scripted.is_absolute() else root / args.scripted
                coder = ScriptedCoder(scripted)
            else:
                env = env if env is not None else {**dotenv_values(root / ".env"), **os.environ}
                if not env.get("OPENROUTER_API_KEY") or not env.get("DATABASE_URL"):
                    raise BuildError("Set OPENROUTER_API_KEY and DATABASE_URL locally.")
                from psycopg.rows import dict_row
                from psycopg_pool import AsyncConnectionPool

                from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices
                from agentrpg.migrate import migrate

                await migrate(env["DATABASE_URL"])
                pool = await stack.enter_async_context(
                    AsyncConnectionPool(
                        env["DATABASE_URL"],
                        min_size=1,
                        max_size=3,
                        kwargs={"row_factory": dict_row},
                    )
                )
                ledger = CampaignLedger(pool)
                campaign_id = args.campaign or uuid4()
                await ledger.ensure_campaign(campaign_id, args.budget_usd)

                async def capture_campaign():
                    try:
                        report["campaign_budget"] = await ledger.summary(campaign_id)
                    except Exception:
                        report["campaign_budget"] = {"id": str(campaign_id)}

                stack.push_async_callback(capture_campaign)
                director_name = args.director_model or args.coder_model
                prices = await fetch_model_prices(sorted({args.coder_model, director_name}))
                binding = BudgetBinding(ledger, campaign_id, prices)

                def make(name, role, tokens):
                    model = create_model(
                        env["OPENROUTER_API_KEY"],
                        name,
                        max_tokens=tokens,
                        max_output_limit=16384,
                        timeout_seconds=args.timeout_seconds,
                        max_retries=0,
                        budget=binding,
                        budget_role=role,
                    )
                    stack.push_async_callback(model.aclose)
                    return model

                coder = ModelCoder(
                    make(args.coder_model, "builder", 12000),
                    usage=usage["coder"],
                    timeout_seconds=args.timeout_seconds,
                )
            if build_file.get("plan") is not None:
                plan = BuildPlan.model_validate(build_file["plan"])
                report["plan_source"] = "build file"
            else:
                if make is None:
                    raise BuildError("A scripted build needs a plan in the build file.")
                brief = build_file.get("brief")
                if not isinstance(brief, str) or not 1 <= len(brief.strip()) <= 16000:
                    raise BuildError("The build file needs a brief of 1 to 16,000 characters.")
                report["director_model"] = args.director_model or args.coder_model
                planner = WorldPlanner(
                    make(report["director_model"], "director", 12000),
                    usage=usage["director"],
                    timeout_seconds=args.timeout_seconds,
                )
                try:
                    plan = await planner.plan(base, brief)
                finally:
                    report["plan_failures"] = planner.failures
                report["plan_source"] = "director"
            validate_plan(plan, base)
            results = await build(plan, coder, base, max_repairs=args.max_repairs)
            report["orders"] = [result.report() for result in results]
            failed_plan = pack / "failed-plan.json"
            if not all(result.passed for result in results):
                pack.mkdir(parents=True, exist_ok=True)
                failed_plan.write_text(
                    json.dumps(plan.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
                )
                write_attempts(pack, results)
                report["outcome"] = "orders_failed"
                return report
            scenario = assemble(base, plan, results, scenario_id=build_file.get("scenario_id"))
            save_scenario(
                output,
                json.dumps(scenario.model_dump(mode="json"), indent=2, ensure_ascii=False),
                overwrite=args.overwrite,
            )
            manifest = write_pack(pack, scenario, plan, results)
            report.update(
                outcome="built",
                scenario_saved=True,
                scenario_hash=manifest["scenario_hash"],
                pack=str(pack),
            )
    except ModelGenerationError as error:
        report["outcome"] = f"provider_{getattr(error, 'kind', None) or 'failure'}"
    except (BuildError, ValidationError, ValueError) as error:
        report["outcome"] = "invalid"
        report["reason"] = (
            error.errors()[0]["msg"] if isinstance(error, ValidationError) else str(error)
        )[:500]
    except Exception as error:
        # An unexpected fault still leaves a report that names it.
        report["outcome"] = "error"
        report["reason"] = type(error).__name__
        raise
    finally:
        report["usage"] = {role: _usage(tracker) for role, tracker in usage.items()}
        report["finished_at"] = datetime.now(UTC).isoformat()
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "build-report.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
    return report


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        report = asyncio.run(execute(args))
    except BuildError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["outcome"] == "built" else 1


if __name__ == "__main__":
    raise SystemExit(main())
