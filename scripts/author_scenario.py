"""Write and validate a Director scenario with bounded model calls."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.agent_model import create_model, local_model_calls
from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices
from agentrpg.migrate import migrate
from agentrpg.scenario_author import ScenarioAuthor

ROOT = Path(__file__).resolve().parents[1]


class AuthorCommandError(RuntimeError):
    """Public errors contain no private brief or provider response."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", help="Scenario instructions; read standard input when omitted.")
    parser.add_argument(
        "--agents", type=int, default=2, help="Owner/assistant pairs, from 2 through 15."
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Destination ScenarioSpec JSON file."
    )
    parser.add_argument(
        "--model", help="Author model; default: OPENROUTER_DIRECTOR_MODEL, then OPENROUTER_MODEL."
    )
    parser.add_argument("--reviewer-model", help="Critique model; default: author model.")
    parser.add_argument(
        "--max-repairs", type=int, default=1, help="Repair allowance, from 0 through 2."
    )
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file.")
    parser.add_argument("--campaign", type=UUID, help="Share an existing model-cost campaign.")
    parser.add_argument("--budget-usd", type=Decimal, help="Total campaign cap in US dollars.")
    args = parser.parse_args(argv)
    if not 2 <= args.agents <= 15:
        parser.error("--agents must be between 2 and 15.")
    if not 0 <= args.max_repairs <= 2:
        parser.error("--max-repairs must be between 0 and 2.")
    if not 1 <= args.timeout_seconds <= 120:
        parser.error("--timeout-seconds must be between 1 and 120.")
    if args.output.suffix.lower() != ".json":
        parser.error("--output must name a JSON file.")
    if args.budget_usd is not None and (
        not args.budget_usd.is_finite() or not 0 < args.budget_usd <= 1000000
    ):
        parser.error("--budget-usd must be positive and finite.")
    if args.campaign is not None and args.budget_usd is None:
        parser.error("--campaign requires the saved --budget-usd limit.")
    return args


def save_scenario(path: Path, content: str, *, overwrite: bool) -> None:
    """Publish complete validated JSON atomically; prevent an overwrite race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".scenario-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Hard-link publication fails atomically if another process created
            # the destination during authoring. The temporary file is complete.
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise AuthorCommandError(
                    "The output exists. Select a new path or use --overwrite."
                ) from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_author_report(output: Path, report: dict) -> Path:
    """Keep every invocation, plus a convenient summary beside the scenario."""
    report_path = output.with_suffix(".author-report.json")
    history_path = output.with_suffix(f".author-report.{report['invocation_id']}.json")
    report["invocation_report"] = str(history_path)
    report["report"] = str(report_path)
    content = json.dumps(report, indent=2, default=str)
    save_scenario(history_path, content, overwrite=False)
    # Concurrent invocations can replace the latest summary, but each immutable
    # invocation record keeps its own counts and outcome.
    save_scenario(report_path, content, overwrite=True)
    return report_path


async def execute(args, *, root: Path = ROOT, env: dict | None = None) -> dict:
    brief = args.brief if args.brief is not None else sys.stdin.read(16_001)
    if not isinstance(brief, str) or not brief.strip() or len(brief) > 16_000:
        raise AuthorCommandError("The brief must contain between 1 and 16,000 characters.")
    output = args.output if args.output.is_absolute() else root / args.output
    if output.exists() and not args.overwrite:
        raise AuthorCommandError("The output exists. Select a new path or use --overwrite.")
    if output.is_dir():
        raise AuthorCommandError("The output must be a file.")
    env = env if env is not None else {**dotenv_values(root / ".env"), **os.environ}
    if not env.get("OPENROUTER_API_KEY"):
        raise AuthorCommandError("Set OPENROUTER_API_KEY in the local configuration.")
    model_name = (
        args.model
        or env.get("OPENROUTER_DIRECTOR_MODEL")
        or env.get("OPENROUTER_MODEL")
        or "openai/gpt-4.1-mini"
    )
    author_output_tokens = min(16384, 4096 + 768 * (args.agents - 2))
    started = time.monotonic()
    author = None
    report = {
        "invocation_id": str(uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
        "output": str(output),
        "participants": args.agents,
        "author_model": model_name,
        "reviewer_model": args.reviewer_model or model_name,
        "outcome": "failed",
        "scenario_saved": False,
        "limits": {
            "max_repairs": args.max_repairs,
            "max_model_invocations": 2 * (args.max_repairs + 1),
            "max_output_tokens_per_call": author_output_tokens,
            "max_author_output_tokens": author_output_tokens,
            "max_critique_output_tokens": 4096,
            "timeout_seconds_per_call": args.timeout_seconds,
            "provider_retries_per_invocation": 2,
        },
    }
    try:
        async with AsyncExitStack() as stack:
            binding = None
            if getattr(args, "budget_usd", None) is not None:
                if not env.get("DATABASE_URL"):
                    raise AuthorCommandError("A budgeted author needs DATABASE_URL locally.")
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
                        report["campaign_budget"] = {
                            "id": str(campaign_id),
                            "status": "Read the durable ledger for final cost status.",
                        }

                # Capture before the pool closes, including failed model attempts.
                stack.push_async_callback(capture_campaign)
                prices = await fetch_model_prices([model_name, args.reviewer_model or model_name])
                binding = BudgetBinding(ledger, campaign_id, prices)
            author_options = {"max_tokens": author_output_tokens}
            reviewer_options = {"max_tokens": 4096}
            if binding is not None:
                author_options.update(budget=binding, budget_role="author")
                reviewer_options.update(budget=binding, budget_role="critic")
            if author_output_tokens > 4096:
                author_options["max_output_limit"] = 16384
            model = create_model(env["OPENROUTER_API_KEY"], model_name, **author_options)
            stack.push_async_callback(model.aclose)
            reviewer = create_model(
                env["OPENROUTER_API_KEY"], args.reviewer_model or model_name, **reviewer_options
            )
            stack.push_async_callback(reviewer.aclose)
            author = ScenarioAuthor(
                model,
                reviewer=reviewer,
                max_repairs=args.max_repairs,
                timeout_seconds=args.timeout_seconds,
            )
            with local_model_calls():
                scenario = await author.author(brief, participant_count=args.agents)
            save_scenario(output, scenario.model_dump_json(indent=2), overwrite=args.overwrite)
            report.update(
                {
                    "outcome": "saved",
                    "scenario_saved": True,
                    "scenario_id": scenario.id,
                    "scenario_hash": scenario.content_hash(),
                    "status": "Validated draft saved. Review it before comparison runs.",
                }
            )
    except asyncio.CancelledError:
        report.update(outcome="interrupted", failure_kind="cancelled")
        raise
    except Exception as error:
        # Only a class name is retained. Raw errors can expose prompts or credentials.
        report.update(
            outcome="failed",
            failure_kind=type(error).__name__,
            status="No unvalidated draft was saved.",
        )
        raise
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report["attempts"] = author.last_attempts if author is not None else []
        report["model_usage"] = author.usage.summary() if author is not None else None
        save_author_report(output, report)
        print(json.dumps(report, indent=2, default=str), flush=True)
    return report


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        raise SystemExit("Scenario authoring interrupted. No incomplete draft was saved.") from None
    except AuthorCommandError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit(
            "Scenario authoring failed validation or a model request. "
            "No unvalidated draft was saved. Check the brief and local provider configuration."
        ) from None


if __name__ == "__main__":
    main()
