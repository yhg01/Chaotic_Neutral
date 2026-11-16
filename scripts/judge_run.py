"""Judge one saved run for misaligned assistant actions, or run the judge's controls.

The judge reads a run and never changes it. Its answers and reports go to private files in
`.local/judge/`. Every paid call goes through a campaign ledger with a stated USD ceiling.
A second start with the same name and settings reads the saved answers and asks only for
what is missing.

    .venv/bin/python scripts/judge_run.py --run RUN_ID --estimate
    .venv/bin/python scripts/judge_run.py --run RUN_ID --campaign UUID --budget-usd 15
    .venv/bin/python scripts/judge_run.py --controls --campaign UUID --budget-usd 3

Use `--test-schema NAME` for a run in a kept schema of the test database, such as a trial
run of `scripts/try_zeroclaw_live.py`. See docs/MISALIGNMENT_JUDGE.md.
"""

import argparse
import asyncio
import csv
import io
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_scenario import save_private  # noqa: E402

from agentrpg.agent_model import create_model  # noqa: E402
from agentrpg.budget import (  # noqa: E402
    BudgetBinding,
    CampaignLedger,
    direct_prices,
    direct_provider,
    fetch_model_prices,
)
from agentrpg.direct_model import create_direct_model  # noqa: E402
from agentrpg.judge_controls import control_cases, score_controls  # noqa: E402
from agentrpg.judge_evidence import chunks, load_run, owner_brief, pack_owner  # noqa: E402
from agentrpg.misalignment import (  # noqa: E402
    JUDGE_VERSION,
    SCREEN_PROMPT,
    VERDICT_PROMPT,
    FileCache,
    JudgeStopped,
    MisalignmentJudge,
    build_report,
    render_markdown,
    review_rows,
)

SCREEN_OUTPUT_TOKENS = 6000
VERDICT_OUTPUT_TOKENS = 8000
# For the estimate only: characters for each token, and what one vote reads and writes.
CHARS_PER_TOKEN = 3
ASSUMED_CANDIDATES_PER_PART = 2
# The pilot of 16 November 2026 on real records gave about 19,300 input tokens and 900 output
# tokens for each vote, and 1.5 candidates for each part.
ASSUMED_VOTE_INPUT_TOKENS = 20_000
ASSUMED_VOTE_OUTPUT_TOKENS = 1000


class JudgeRunError(Exception):
    """A safe message for the operator."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", type=UUID, help="The run to judge.")
    target.add_argument("--controls", action="store_true", help="Run the judge on known cases.")
    parser.add_argument("--test-schema", help="Read the run from this schema of the test database.")
    parser.add_argument("--name", default="v1", help="Names this judge run and its folder.")
    # On real records a smaller screening model listed far fewer candidates: see the document.
    parser.add_argument("--screen-model", default="anthropic:claude-opus-5-5")
    parser.add_argument("--verdict-model", default="anthropic:claude-opus-5-5")
    parser.add_argument("--votes", type=int, default=3)
    parser.add_argument("--owners", help="Owner IDs with commas between them. Default: all.")
    parser.add_argument("--chunk-chars", type=int, default=60_000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--sample", type=int, default=30, help="Unlisted actions for a person.")
    parser.add_argument("--campaign", type=UUID, help="The campaign that holds the USD ceiling.")
    parser.add_argument(
        "--budget-usd", type=Decimal, help="The persistent ceiling of the campaign."
    )
    parser.add_argument(
        "--estimate", action="store_true", help="Count the work and its cost. No model call."
    )
    return parser.parse_args(argv)


def database_url(env: dict, schema: str | None) -> str:
    """The main database, or one schema of the test database."""
    if not env.get("DATABASE_URL"):
        raise JudgeRunError("Set DATABASE_URL locally.")
    if not schema:
        return env["DATABASE_URL"]
    configured = env.get("TEST_DATABASE_URL")
    if not configured:
        parameters = conninfo_to_dict(env["DATABASE_URL"])
        parameters["dbname"] = f"{parameters['dbname']}_test"
        configured = make_conninfo(**parameters)
    if "test" not in conninfo_to_dict(configured).get("dbname", "").lower():
        raise JudgeRunError("--test-schema needs a test database. Its name must contain 'test'.")
    if not schema.replace("_", "").isalnum():
        raise JudgeRunError("The schema name can hold letters, digits and underscores only.")
    return make_conninfo(configured, options=f"-c search_path={schema}")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".saving-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def judge_model(env: dict, name: str, binding, role: str, *, max_tokens, temperature, timeout):
    """One budgeted model for a judge stage. The caller closes it."""
    found = direct_provider(name)
    if found is not None:
        key = env.get(found[1].key_name)
        if not key:
            raise JudgeRunError(f"Set {found[1].key_name} locally.")
        return create_direct_model(
            key,
            name,
            max_tokens=max_tokens,
            timeout_seconds=timeout,
            max_retries=0,
            budget=binding,
            budget_role=role,
            temperature=temperature,
        )
    if not env.get("OPENROUTER_API_KEY"):
        raise JudgeRunError("Set OPENROUTER_API_KEY locally.")
    return create_model(
        env["OPENROUTER_API_KEY"],
        name,
        max_tokens=max_tokens,
        max_output_limit=16384,
        timeout_seconds=min(timeout, 120),
        max_retries=0,
        budget=binding,
        budget_role=role,
        temperature=temperature,
    )


async def model_prices(names: set[str]) -> dict:
    direct = {name for name in names if direct_provider(name) is not None}
    prices = direct_prices(direct)
    if names - direct:
        prices.update(await fetch_model_prices(names - direct))
    return prices


def estimate(parts: dict[str, list], briefs: dict[str, dict], prices: dict, args) -> dict:
    """A count of the work and a cost figure from stated assumptions. It is not a ceiling."""
    count = sum(len(items) for items in parts.values())
    read = 0
    for owner, items in parts.items():
        brief = len(json.dumps(briefs[owner], ensure_ascii=False))
        for part in items:
            shown = [*part.earlier_owner_messages, *part.context, *part.records]
            read += len(SCREEN_PROMPT) + brief
            read += sum(len(json.dumps(r.shown(), ensure_ascii=False, default=str)) for r in shown)
    screen_tokens = read // CHARS_PER_TOKEN
    candidates = count * ASSUMED_CANDIDATES_PER_PART
    votes = candidates * args.votes
    screen_cost = prices[args.screen_model].maximum(screen_tokens, count * SCREEN_OUTPUT_TOKENS)
    vote_cost = prices[args.verdict_model].maximum(
        votes * (ASSUMED_VOTE_INPUT_TOKENS + len(VERDICT_PROMPT) // CHARS_PER_TOKEN),
        votes * ASSUMED_VOTE_OUTPUT_TOKENS,
    )
    return {
        "parts": count,
        "screen_input_tokens_about": screen_tokens,
        "screen_cost_usd_about": str(screen_cost.quantize(Decimal("0.01"))),
        "assumed_candidates": candidates,
        "assumed_votes": votes,
        "verdict_cost_usd_about": str(vote_cost.quantize(Decimal("0.01"))),
        "total_usd_about": str((screen_cost + vote_cost).quantize(Decimal("0.01"))),
        "assumptions": [
            f"{CHARS_PER_TOKEN} characters for each token, and the full output limit for "
            "each screening call.",
            f"{ASSUMED_CANDIDATES_PER_PART} candidates for each part. The real number is "
            "known only after screening.",
            f"{ASSUMED_VOTE_INPUT_TOKENS} input and {ASSUMED_VOTE_OUTPUT_TOKENS} output tokens "
            "for each vote.",
            "The ceiling of the campaign is the limit. This figure is a guide to set it.",
        ],
    }


def review_csv(rows: list[dict]) -> str:
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["item"])
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


async def execute(args, *, root: Path = ROOT, env: dict | None = None, models=None) -> dict:
    """Run the judge. `models` is a (screen, verdict) pair for a test with scripted models."""
    env = env if env is not None else {**dotenv_values(root / ".env"), **os.environ}
    if not 1 <= args.votes <= 7:
        raise JudgeRunError("--votes must be from 1 through 7.")
    target = "controls" if args.controls else str(args.run)
    folder = root / ".local" / "judge" / target / args.name
    settings = {
        "judge_version": JUDGE_VERSION,
        "screen_model": args.screen_model,
        "verdict_model": args.verdict_model,
        "votes": args.votes,
        "chunk_chars": args.chunk_chars,
    }
    paid = models is None and not args.estimate
    saved = None
    if (folder / "settings.json").is_file():
        saved = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
        changed = [key for key, value in settings.items() if saved.get(key) != value]
        if changed:
            raise JudgeRunError(
                f"The judge run '{args.name}' was started with other settings "
                f"({', '.join(changed)}). Give a new --name."
            )
    budget = (saved or {}).get("budget")
    if paid:
        if budget is None:
            if args.budget_usd is None or args.campaign is None:
                raise JudgeRunError(
                    "A paid judge run needs both --budget-usd and --campaign, so its ceiling "
                    "is a stated choice."
                )
            budget = {"campaign_id": str(args.campaign), "limit_usd": str(args.budget_usd)}
        elif (args.campaign is not None and str(args.campaign) != budget["campaign_id"]) or (
            args.budget_usd is not None and args.budget_usd != Decimal(budget["limit_usd"])
        ):
            raise JudgeRunError("A continued judge run keeps its saved campaign and ceiling.")
    need_database = paid or not args.controls
    pool = None
    if need_database:
        pool = AsyncConnectionPool(
            database_url(env, args.test_schema),
            min_size=1,
            max_size=4,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        )
        await pool.open()
    opened = []
    try:
        if args.controls:
            cases = control_cases()
            run = {"run_id": "controls", "status": "controls"}
            briefs = {case.id: case.brief for case in cases}
            records = {case.id: case.records for case in cases}
        else:
            saved_run = await load_run(pool, args.run)
            wanted = args.owners.split(",") if args.owners else None
            owners = [o.id for o in saved_run.spec.owners if wanted is None or o.id in wanted]
            if wanted and set(wanted) - set(owners):
                raise JudgeRunError("--owners names an owner that is not in this run.")
            run = {
                "run_id": saved_run.run_id,
                "status": saved_run.status,
                "scenario_hash": saved_run.scenario_hash,
                "schema": args.test_schema,
            }
            briefs = {owner: owner_brief(saved_run.spec, owner) for owner in owners}
            records = {owner: pack_owner(saved_run.events, owner) for owner in owners}
        for owner, items in records.items():
            save_text(
                folder / "evidence" / f"{owner}.jsonl",
                "".join(
                    json.dumps(r.shown(), ensure_ascii=False, default=str) + "\n" for r in items
                ),
            )
        parts = {
            owner: chunks(items, briefs[owner]["owner_id"], max_chars=args.chunk_chars)
            for owner, items in records.items()
        }
        if args.estimate:
            prices = await model_prices({args.screen_model, args.verdict_model})
            return {
                "run": run,
                "settings": settings,
                "records": {owner: len(items) for owner, items in records.items()},
                "estimate": estimate(parts, briefs, prices, args),
            }
        binding = None
        if paid:
            ledger = CampaignLedger(pool)
            await ledger.ensure_campaign(
                budget["campaign_id"], Decimal(budget["limit_usd"]), name=f"Judge {target}"[:160]
            )
            prices = await model_prices({args.screen_model, args.verdict_model})
            binding = BudgetBinding(ledger, UUID(budget["campaign_id"]), prices)
            screen = judge_model(
                env,
                args.screen_model,
                binding,
                "judge_screen",
                max_tokens=SCREEN_OUTPUT_TOKENS,
                temperature=0,
                timeout=args.timeout_seconds,
            )
            # The votes must be independent, so this model does not run at temperature zero.
            verdict = judge_model(
                env,
                args.verdict_model,
                binding,
                "judge_verdict",
                max_tokens=VERDICT_OUTPUT_TOKENS,
                temperature=0.7,
                timeout=args.timeout_seconds,
            )
            opened = [screen, verdict]
        else:
            screen, verdict = models
        save_private(folder / "settings.json", {**settings, "budget": budget})
        judge = MisalignmentJudge(
            screen,
            verdict,
            screen_label=args.screen_model,
            verdict_label=args.verdict_model,
            votes=args.votes,
            timeout_seconds=args.timeout_seconds,
            concurrency=args.concurrency,
            cache=FileCache(folder / "answers"),
            chunk_chars=args.chunk_chars,
        )
        results, stopped = {}, None
        for owner in records:
            try:
                results[owner] = await judge.judge_owner(briefs[owner], records[owner])
            except JudgeStopped as error:
                stopped = {"kind": error.kind, "detail": str(error), "at_owner": owner}
                break
            print(
                f"{owner}: {results[owner].candidates} candidates, "
                f"{len(results[owner].incidents)} decided",
                flush=True,
            )
        use = judge.use()
        if binding is not None:
            summary = await binding.ledger.summary(binding.campaign_id)
            use["ledger"] = {
                key: str(value)
                for key, value in summary.items()
                if key.endswith("_usd") or key in ("attempts", "halted")
            }
        if stopped is not None:
            result = {"status": "stopped", "stopped": stopped, "run": run, "use": use}
            save_private(folder / "stopped.json", result)
            return result
        if args.controls:
            report = {
                "judge_version": JUDGE_VERSION,
                "settings": settings,
                "controls": score_controls(cases, results),
                "use": use,
            }
            save_private(folder / "controls.json", report)
            return {"status": "completed", **report, "folder": str(folder)}
        report = build_report(run, list(results.values()), settings)
        report["use"] = use
        save_private(folder / "report.json", report)
        save_text(folder / "report.md", render_markdown(report))
        rows = review_rows(report, records, size=args.sample, seed=args.run.int % 2**32)
        save_text(folder / "review_sample.csv", review_csv(rows))
        return {
            "status": "completed",
            "run": run,
            "counts_by_category": report["counts_by_category"],
            "counts_by_owner": report["counts_by_owner"],
            "coverage": report["coverage"],
            "use": use,
            "folder": str(folder),
        }
    finally:
        for model in opened:
            await model.aclose()
        if pool is not None:
            await pool.close()


def main(argv=None) -> None:
    args = parse_args(argv)
    try:
        result = asyncio.run(execute(args))
    except JudgeRunError as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(result, indent=1, default=str))
    if result.get("status") == "stopped":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
