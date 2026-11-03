"""Export safe browser-shop evidence, retaining failed runs and source hashes."""

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.budget import CampaignLedger
from agentrpg.config import Settings
from agentrpg.director import rows
from agentrpg.shop_runtime import shop_report

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = UUID("635aa42e-386f-43dc-b679-b73cd0cd276f")


async def main():
    config = Settings()
    evidence = []
    forbidden = []
    async with AsyncConnectionPool(
        config.database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"row_factory": dict_row},
    ) as pool:
        folder = ROOT / ".local/browser-shop-runs"
        sources = sorted(folder.glob("*/report.json")) + sorted(folder.glob("*/invocation-*.json"))
        seen = set()
        for source in sources:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            original = json.loads(source.read_text())
            credentials = json.loads(source.with_name("credentials.json").read_text())
            forbidden += [m["token"] for m in credentials["participants"]]
            audited = await shop_report(pool, UUID(original["run_id"]), rounds=original["rounds"])
            selected = {
                key: original[key]
                for key in (
                    "run_id",
                    "mode",
                    "run_status",
                    "run_result",
                    "window",
                    "rounds",
                    "checks",
                    "passed",
                    "buyer_balance",
                    "seller_balance",
                    "elapsed_seconds",
                    "runtime_recreations",
                    "committed_checkout_recoveries",
                    "owner_conversation",
                    "orders",
                    "tasks",
                )
            }
            selected["original_checks"] = selected.pop("checks")
            selected["original_passed"] = selected.pop("passed")
            unchanged = (
                original["run_status"] == audited["run_status"]
                and original["window"] == audited["window"]
            )
            selected["checks"] = audited["checks"] if unchanged else original["checks"]
            selected["passed"] = all(selected["checks"].values())
            selected["current_run_checks"] = audited["checks"]
            selected["latest_invocation"] = source.name == "report.json"
            selected["source_sha256"] = digest
            selected["source_report"] = str(source.relative_to(ROOT))
            for key in (
                "invocation_id",
                "process_id",
                "stop_reason",
                "invocation_completed_windows",
                "supervision",
                "task_health",
            ):
                if key in original:
                    selected[key] = original[key]
            selected["configuration"] = {
                k: credentials["configuration"][k]
                for k in (
                    "runtime_revision",
                    "mode",
                    "rounds",
                    "model_roles",
                    "model",
                    "actor_model",
                    "require_shopping_task_coverage_review",
                    "preserve_shopping_consent",
                    "independent_shopping_stimulus_review",
                    "shopping_initial_setup_only",
                    "require_initial_shopping_consent",
                    "stimulus_model",
                    "step_interval_seconds",
                    "supervision",
                )
                if k in credentials["configuration"]
            }
            selected["scope"] = "Real model owners, reviewers, buyer and seller; actual Chrome."
            if original["mode"] != "model_all_roles":
                selected["scope"] = (
                    "Real GPT-4.1-mini buyer; supplied owner Actors and seller Agent."
                    if original["mode"] == "model_buyer"
                    else "Supplied control decisions; real Agent graph, Chrome, HTTP, "
                    "Director and PostgreSQL."
                )
            selected["provider_usage"] = (
                original["model_usage"]
                if original["mode"].startswith("model")
                else {
                    "requests": 0,
                    "reported_cost_usd": 0,
                }
            )
            if original["mode"] == "supplied_control":
                selected["supplied_buyer_graph_calls"] = original.get(
                    "supplied_buyer_graph_calls", original["model_usage"]["requests"]
                )
            trace_source = audited if unchanged else original
            selected["actor_attempts"] = [
                {
                    "owner_id": j["owner_id"],
                    "window": j["window"],
                    "attempts": j.get("output_meta", {}).get("attempts", []),
                }
                for j in trace_source["jobs"]
                if j["kind"] == "actor" and j.get("output_meta", {}).get("attempts")
            ]
            completions = sorted(
                (e for e in trace_source["trajectory"] if e["kind"] == "window_completed"),
                key=lambda e: e["window"],
            )
            times = [
                datetime.fromisoformat(e["created_at"])
                if isinstance(e["created_at"], str)
                else e["created_at"]
                for e in completions
            ]
            gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
            selected["clock"] = {
                "completed_window_count": len(completions),
                "minimum_window_gap_seconds": min(gaps) if gaps else None,
                "maximum_window_gap_seconds": max(gaps) if gaps else None,
                "window_execution_span_seconds": sum(gaps),
            }
            selected["tool_calls"] = [
                {
                    "window": e["window"],
                    "owner_id": e["owner_id"],
                    **{k: e["payload"][k] for k in ("tool", "call_id", "arguments")},
                    "ok": e["payload"]["result"].get("ok"),
                    "replayed_effect": e["payload"]["result"]
                    .get("data", {})
                    .get("replayed_effect"),
                    "error": e["payload"]["result"].get("error"),
                }
                for e in trace_source["trajectory"]
                if e["kind"] == "assistant_tool_call"
            ]
            selected["failures"] = [
                {"kind": e["kind"], "window": e["window"], "payload": e["payload"]}
                for e in trace_source["trajectory"]
                if e["kind"]
                in {
                    "simulator_error",
                    "infrastructure_failure",
                    "job_failure",
                    "recovery_blocked",
                    "task_stalled",
                }
            ]
            selected["unfinished_jobs"] = [
                {
                    k: j[k]
                    for k in ("id", "window", "owner_id", "kind", "status", "error", "attempts")
                }
                for j in trace_source["jobs"]
                if j["status"] != "completed"
            ]
            evidence.append(selected)
        budget = await CampaignLedger(pool).summary(CAMPAIGN, recent_limit=0)
        async with pool.connection() as conn:
            spending = await rows(
                conn,
                "SELECT run_id,role,count(*) AS attempts,"
                "sum(reported_usd) AS reported_usd FROM model_spend_attempts "
                "WHERE campaign_id=%s AND run_id=ANY(%s) GROUP BY run_id,role",
                (CAMPAIGN, list({UUID(r["run_id"]) for r in evidence})),
            )
    result = {
        "schema_version": 1,
        "date": "2026-11-03",
        "runs": evidence,
        "campaign": budget,
        "spending_by_run_and_role": spending,
        "limits": [
            "No real purchases, mail, bank accounts or external shopping sites.",
            "Scope is recorded per run: buyer-only, all-model roles or supplied control.",
            "30-purchase controls use supplied decisions; paced process-resume evidence "
            "is separate from the original run in under two minutes.",
            "No multi-hour, natural multi-person, 15-browser, or out-of-stock model study.",
            "Earlier buyer-only models reported from accepted Director settlement events "
            "without reopening the order page; preserve this workflow limit.",
            "Full trace and original reports remain in PostgreSQL and ignored local run folders.",
        ],
    }
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    if any(secret in text for secret in forbidden):
        raise SystemExit("Credential check failed; no evidence file was written.")
    (ROOT / "docs/browser-shop-results.json").write_text(text)
    print(
        json.dumps(
            {
                "runs": len(evidence),
                "passed": [r["run_id"] for r in evidence if r["passed"]],
                "remaining_usd": budget["remaining_usd"],
            },
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
