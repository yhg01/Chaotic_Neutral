"""Prepare, run or resume the bounded elapsed Actor/Agent conductor.

Preparation makes no provider request. Live execution requires a saved
baseline model manifest and an existing cumulative cost campaign; it never
creates a replacement campaign or increases its ceiling.
"""

import argparse
import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.actor import CURRENT_CUE_REVIEW_PROTOCOL, ModelActor
from agentrpg.actor_reviewer_config import load_actor_reviewer_api
from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.agent_tools import BoardTools
from agentrpg.budget import BudgetBinding, BudgetError, CampaignLedger, fetch_model_prices
from agentrpg.clock_evidence import export_clock_evidence
from agentrpg.clock_runtime import ClockRuntime
from agentrpg.clock_transfers import TimedTransfers
from agentrpg.community_experiment import CommunityPlan, DailyMeetingPlan, install_community_events
from agentrpg.config import Settings
from agentrpg.confirmation_control import CONFIRMATION_INSTRUCTIONS, control_manifest, control_spec
from agentrpg.confirmation_daily import DAILY_CONFIRMATION_INSTRUCTIONS, DailyConfirmationPlan
from agentrpg.confirmation_schema import CONFIRMATION_PROTOCOL
from agentrpg.console_execution import save_private
from agentrpg.deep_agent import AgentBudgetError, AgentRuntime
from agentrpg.director import Director
from agentrpg.director_clock import DirectorClock
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.main import create_app
from agentrpg.migrate import migrate
from agentrpg.personal_agent_config import load_personal_agent_api
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.simulation_clock import digest
from agentrpg.trial_audit import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = UUID("635aa42e-386f-43dc-b679-b73cd0cd276f")
CUMULATIVE_CEILING = Decimal("20")
PERSONAL_TRANSPORT = {"timeout_seconds": 120, "max_retries": 0}
ELAPSED_INSTRUCTIONS = (
    "\nELAPSED CLOCK PROFILE. The world has an explicit simulated clock. Legacy current_window "
    "is not a day or a second. read_run_view.elapsed_clock is authoritative for elapsed time. "
    "The legacy run is paused to exclude its old conductor; an active elapsed_clock permits "
    "the offered tools. Model generation and graph tool batches take simulated time. "
    "A pending transfer yields this activation; it is applied only at its Clock due time. "
    "After its actual completion event, read the real receipt or your account before reporting "
    "success, mailing a payment notice or treating a task as paid. Persistent owner tasks stay "
    "open across activations. Use the task tools to defer work or complete it only with "
    "its actual payment, sent recipient notice and delivered owner "
    "report IDs. A follow-up is not a new "
    "debt or permission. Do not renew a used grant or repay an already committed debt. "
    "Use empty text when there is no new owner-facing result or question. All money, mail and "
    "board records are internal to this environment; no external websites or host tools exist."
    " Use the board for material plans, questions, answers or changes. Do not create a new "
    "post merely to acknowledge another acknowledgement or to repeat an unchanged status. "
    "Use acknowledge for received board IDs after reading them. Do not answer a courtesy "
    "owner acknowledgement with another completion report when no request or result changed. "
    "Use empty finish text when there is no new owner-facing information. Read actual new "
    "mail and board evidence, then decide whether further action is useful; never poll on "
    "empty time intervals."
)
GATES = (
    "require_mail_intent_review",
    "require_transfer_task_intent_review",
    "require_transfer_authority_review",
    "require_transfer_task_coverage_review",
)


def compact_budget(value):
    return {
        k: value[k]
        for k in (
            "id",
            "limit_usd",
            "attempts",
            "reported_usd",
            "reserved_usd",
            "uncertain_usd",
            "committed_usd",
            "remaining_usd",
            "halted",
        )
    }


def execution_configuration_hash(
    baseline, personal, actor_reviewer=None, *, confirmation_short=False, daily_confirmation=None
):
    return digest(
        {
            "baseline_configuration": baseline,
            "personal_agent_configuration": {"model": personal.model, **personal.manifest_fields()},
            "actor_reviewer_configuration": {
                "model": baseline["actor_model"],
                **(actor_reviewer.manifest_fields() if actor_reviewer is not None else {}),
            },
            "review_delivery_metadata": "visible-wait-delivery-v2",
            "personal_transport": PERSONAL_TRANSPORT,
            "native_task_binding_validation": "live-owner-task-v4-open-schema",
            "current_cue_review_protocol": CURRENT_CUE_REVIEW_PROTOCOL,
            **({"confirmation_control": control_manifest()} if confirmation_short else {}),
            **({"daily_confirmation": daily_confirmation} if daily_confirmation else {}),
        }
    )


def create_role_models(personal, actor_reviewer, binding):
    """Keep role clients separate while selecting only their configured keys."""
    return (
        create_model(
            personal.api_key.get_secret_value(),
            personal.model,
            budget=binding,
            budget_role="assistant",
            max_tokens=1500,
            **PERSONAL_TRANSPORT,
        ),
        create_model(
            actor_reviewer.api_key.get_secret_value(),
            actor_reviewer.model,
            budget=binding,
            budget_role="actor",
            max_tokens=1500,
        ),
        create_model(
            actor_reviewer.api_key.get_secret_value(),
            actor_reviewer.model,
            budget=binding,
            budget_role="actor-review",
            max_tokens=1000,
        ),
    )


async def main(args):
    confirmation_short = getattr(args, "confirmation_short", False)
    confirmation_daily = getattr(args, "event_profile", "mixed") == "daily-confirmation"
    confirmations_enabled = confirmation_short or confirmation_daily
    if confirmation_short and (
        args.days != 1 or args.pairs != 2 or args.scenario or args.event_profile != "mixed"
    ):
        raise SystemExit(
            "The confirmation short control requires --days 1 --pairs 2 and its own fixed scene."
        )
    if confirmation_short:
        args.no_chaos = True
    daily_meeting = getattr(args, "event_profile", "mixed") == "daily-meeting"
    if daily_meeting and (args.scenario or args.no_chaos):
        raise SystemExit("The daily-meeting profile requires its declared scene and events.")
    plan = (
        DailyConfirmationPlan
        if confirmation_daily
        else DailyMeetingPlan
        if daily_meeting
        else CommunityPlan
    )(
        pairs=args.pairs,
        days=args.days,
        seed=args.seed,
        events_per_day=(
            args.events_per_day
            if args.events_per_day is not None
            else (1 if daily_meeting or confirmation_daily else 2)
        ),
    )
    if args.prepare:
        args.prepare.parent.mkdir(parents=True, exist_ok=True)
        args.prepare.write_text(
            json.dumps(control_manifest() if confirmation_short else plan.manifest(), indent=2)
            + "\n"
        )
        print("Prepared candidate only; no model calls: " + str(args.prepare.resolve()))
        return
    readiness = None
    experiment_estimate = None
    if (
        not args.resume
        and not args.scenario
        and plan.days > 1
        and not args.no_chaos
        and not args.preflight
    ):
        if not args.readiness:
            raise SystemExit(
                "Run the short control and one-day calibration first; a seven-day "
                "candidate requires --readiness."
            )
        readiness = json.loads(args.readiness.read_text())
        if readiness.get("eligible") is not True or readiness.get("plan_hash") != digest(
            plan.manifest()
        ):
            raise SystemExit("The readiness evidence does not permit this exact frozen candidate.")
        if confirmation_daily and readiness.get("free_seven_day_control_passed") is not True:
            raise SystemExit("The daily confirmation profile needs its free seven-day control.")
        if readiness.get("campaign_id") != str(CAMPAIGN) or str(
            readiness.get("ceiling_usd")
        ) != str(CUMULATIVE_CEILING):
            raise SystemExit("The readiness campaign or cumulative ceiling differs.")
        try:
            experiment_estimate = Decimal(str(readiness["estimated_remaining_experiment_usd"]))
        except (KeyError, ArithmeticError, ValueError):
            raise SystemExit("Readiness requires a positive finite experiment estimate.") from None
        if not experiment_estimate.is_finite() or experiment_estimate <= 0:
            raise SystemExit("Readiness requires a positive finite experiment estimate.")
    if not args.baseline_manifest and not args.resume:
        raise SystemExit(
            "Supply --baseline-manifest for a new run, or --resume for its saved manifest."
        )
    env = {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}
    resume = json.loads(args.resume.read_text()) if args.resume else None
    if resume and bool(resume.get("confirmation_control")) != confirmation_short:
        raise SystemExit("Keep the original confirmation profile when resuming this run.")
    if resume and bool(resume.get("daily_confirmation")) != confirmation_daily:
        raise SystemExit("Keep the original daily confirmation profile when resuming.")
    baseline_path = (
        Path(resume["baseline_manifest"]) if resume else args.baseline_manifest.resolve()
    )
    baseline = json.loads(baseline_path.read_text())["configuration"]
    personal = load_personal_agent_api(
        env, root=ROOT, configuration=resume["personal_agent_configuration"] if resume else None
    )
    if personal.model != "deepseek/deepseek-v3.2" or baseline["actor_model"] != "openai/gpt-4.1":
        raise SystemExit(
            "This campaign keeps DeepSeek V3.2 personal Agents and unchanged "
            "GPT-4.1 Actors/reviewers."
        )
    actor_reviewer = load_actor_reviewer_api(
        env,
        root=ROOT,
        model=baseline["actor_model"],
        configuration=(
            resume.get("actor_reviewer_configuration", {"model": baseline["actor_model"]})
            if resume
            else None
        ),
    )
    source_hash = source_fingerprint(ROOT)
    if readiness and readiness.get("source_hash") != source_hash:
        raise SystemExit("The readiness evidence belongs to a different source version.")
    execution_hash = execution_configuration_hash(
        baseline,
        personal,
        actor_reviewer,
        confirmation_short=confirmation_short,
        daily_confirmation=plan.manifest() if confirmation_daily else None,
    )
    if readiness and readiness.get("execution_configuration_hash") != execution_hash:
        raise SystemExit("The readiness execution configuration differs.")
    if resume:
        if resume["source_hash"] != source_hash:
            raise SystemExit(
                "Source differs from this elapsed run. Preserve its evidence and "
                "review migration before resuming."
            )
        if resume.get("execution_configuration_hash") != execution_hash:
            raise SystemExit("The resumed execution configuration differs.")
        plan = (
            DailyConfirmationPlan
            if resume["plan"]["version"] == "friends-community-daily-confirmation-v5"
            else DailyMeetingPlan
            if resume["plan"]["version"] == "friends-community-daily-meeting-v4"
            else CommunityPlan
        ).model_validate(resume["plan"])
        if (
            digest(
                {
                    k: baseline.get(k)
                    for k in ("actor_model", "actor_prompt", "actor_review_prompt", *GATES)
                }
            )
            != resume["baseline_actor_hash"]
        ):
            raise SystemExit("The original Actor/reviewer configuration has changed.")
    prices = await fetch_model_prices({personal.model, baseline["actor_model"]})
    settings = Settings()
    database_url = settings.database_url.get_secret_value()
    if args.preflight:
        async with AsyncConnectionPool(
            database_url, min_size=1, max_size=2, kwargs={"row_factory": dict_row}
        ) as pool:
            before = await CampaignLedger(pool).summary(CAMPAIGN, recent_limit=0)
        report = {
            "execution_configuration_hash": execution_hash,
            "campaign": compact_budget(before),
            "current_price_bounds": {k: v.snapshot() for k, v in prices.items()},
            "provider_calls": 0,
            "database_migrated": False,
            "actor_model": baseline["actor_model"],
            "personal_agent_model": personal.model,
            "actor_reviewer_configuration": {
                "model": actor_reviewer.model,
                **actor_reviewer.manifest_fields(),
            },
            "candidate": control_manifest() if confirmation_short else plan.manifest(),
            "source_hash": source_hash,
        }
        save_private(args.preflight, report)
        print("Read-only budget preflight: " + str(args.preflight.resolve()))
        return
    await migrate(database_url)
    app = create_app(settings)
    async with app.router.lifespan_context(app), AsyncExitStack() as stack:
        ledger = CampaignLedger(app.state.pool)
        before = await ledger.summary(CAMPAIGN, recent_limit=0)
        if (
            before["limit_usd"] != CUMULATIVE_CEILING
            or before["halted"]
            or before["remaining_usd"] <= 0
        ):
            raise SystemExit(
                f"The existing cumulative USD {CUMULATIVE_CEILING} campaign "
                "cannot fund a new invocation."
            )
        allow_budget_checkpoint = bool(
            confirmation_daily
            and readiness
            and readiness.get("budget_policy") == "fund_next_request_then_checkpoint_at_cap"
            and readiness.get("checkpoint_at_budget_limit") is True
        )
        if (
            readiness
            and before["remaining_usd"] < experiment_estimate
            and not allow_budget_checkpoint
        ):
            raise SystemExit("The cumulative campaign cannot fund the frozen experiment estimate.")
        if resume:
            manifest = resume
            run_id = UUID(manifest["run_id"])
        else:
            spec = (
                ScenarioSpec.model_validate_json(args.scenario.read_text())
                if args.scenario
                else control_spec()[0]
                if confirmation_short
                else plan.scene()
            )
            manifest = await Director(app.state.pool).create_run(spec)
            run_id = UUID(manifest["run_id"])
            contract = plan.clock()
            if confirmation_short:
                contract = contract.model_copy(update={"horizon_seconds": 3600})
            if args.scenario:
                contract = contract.model_copy(
                    update={
                        "processes": tuple(
                            type(contract.processes[0])(id=f"{r}-{o.id}", owner_id=o.id, role=r)
                            for o in spec.owners
                            for r in ("actor", "agent")
                        )
                    }
                )
            await DirectorClock(app.state.pool).bind(run_id, contract, max_jobs=10000)
            await TimedTransfers(app.state.pool).enable(run_id)
            if not args.scenario and not args.no_chaos:
                await install_community_events(app.state.pool, run_id, plan)
            manifest.update(
                {
                    "source_hash": source_hash,
                    "candidate_plan_version": plan.version,
                    **({"daily_confirmation": plan.manifest()} if confirmation_daily else {}),
                    "current_cue_review_protocol": CURRENT_CUE_REVIEW_PROTOCOL,
                    "execution_configuration_hash": execution_hash,
                    "plan": plan.model_dump(mode="json"),
                    "baseline_manifest": str(baseline_path),
                    "baseline_actor_hash": digest(
                        {
                            k: baseline.get(k)
                            for k in ("actor_model", "actor_prompt", "actor_review_prompt", *GATES)
                        }
                    ),
                    "campaign_id": str(CAMPAIGN),
                    "no_chaos": args.no_chaos,
                    **({"confirmation_control": control_manifest()} if confirmation_short else {}),
                    "scenario_hash": spec.content_hash(),
                    "personal_agent_configuration": {
                        "model": personal.model,
                        **personal.manifest_fields(),
                    },
                    "actor_reviewer_configuration": {
                        "model": actor_reviewer.model,
                        **actor_reviewer.manifest_fields(),
                    },
                }
            )
        directory = ROOT / ".local/clock-model-runs" / str(run_id)
        save_private(directory / "credentials.json", manifest)
        print(f"Run: {run_id}\nResume: {directory / 'credentials.json'}", flush=True)
        runtime = await stack.enter_async_context(AgentRuntime(database_url))
        binding = BudgetBinding(ledger, CAMPAIGN, prices)
        actors, agents = {}, {}
        usage = UsageTracker()
        for p in manifest["participants"]:
            owner = p["owner_id"]
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://clock/api/v1/",
                    headers={"Authorization": "Bearer " + p["token"]},
                )
            )
            model, actor_model, reviewer = create_role_models(personal, actor_reviewer, binding)
            for m in (model, actor_model, reviewer):
                stack.push_async_callback(m.aclose)
            agents[owner] = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(
                    client,
                    run_id,
                    timed_transfers=True,
                    track_transfer_tasks=True,
                    confirmations=confirmations_enabled,
                ),
                agent_id=p["agent_id"],
                session_id=p["session_id"],
                instructions=baseline["instructions"]
                + ELAPSED_INSTRUCTIONS
                + (CONFIRMATION_INSTRUCTIONS if confirmations_enabled else "")
                + (DAILY_CONFIRMATION_INSTRUCTIONS if confirmation_daily else ""),
                max_model_calls=16,
                timeout_seconds=240,
                callbacks=[usage],
                require_explicit_finish=True,
                guard_empty_scratch=True,
                include_previous_activation_context=True,
                guard_transfer_task_yield=True,
            )
            actors[owner] = ModelActor(
                actor_model,
                reviewer=reviewer,
                max_repairs=1,
                prompt=baseline["actor_prompt"],
                review_prompt=baseline["actor_review_prompt"],
                describe_output_delivery=True,
                require_live_transfer_task_binding=True,
                require_current_cue_review=True,
                **{k: baseline.get(k, False) for k in GATES},
            )
        conductor = ClockRuntime(app.state.pool, actors=actors, agents=agents)
        await conductor.enable(
            run_id,
            {
                "profile": "deepseek-personal-gpt41-actor-review-clock-v1",
                "campaign_id": str(CAMPAIGN),
                "ceiling_usd": str(CUMULATIVE_CEILING),
                "source_hash": source_hash,
                "execution_configuration_hash": execution_hash,
                "scenario_hash": manifest["scenario_hash"],
                **(
                    {
                        "confirmations": (
                            manifest["daily_confirmation"]["confirmations"]
                            if confirmation_daily
                            else manifest["confirmation_control"]["confirmations"]
                        ),
                        "confirmation_protocol": CONFIRMATION_PROTOCOL,
                    }
                    if confirmations_enabled
                    else {}
                ),
            },
        )
        invocation_id = uuid4().hex
        started = time.monotonic()
        failure = None
        budget_checkpoint = False
        try:
            await conductor.advance(run_id, max_actions=args.max_actions)
        except Exception as error:
            # No raw SDK exception or credentials enter the public-facing report.
            failure = type(error).__name__
            budget_checkpoint = (
                isinstance(error, (BudgetError, AgentBudgetError))
                or getattr(error, "kind", None) == "budget"
            )
        finally:
            summary = await export_clock_evidence(app.state.pool, run_id, directory / invocation_id)
            after = await ledger.summary(CAMPAIGN, recent_limit=0)
            summary.update(
                {
                    "invocation_id": invocation_id,
                    "candidate_plan_version": plan.version,
                    "wall_seconds": round(time.monotonic() - started, 3),
                    "failure_kind": failure,
                    "budget_checkpoint": budget_checkpoint,
                    "source_hash": source_hash,
                    "execution_configuration_hash": execution_hash,
                    "personal_agent_usage": usage.summary(),
                    "actor_usage": {o: a.usage.summary() for o, a in actors.items()},
                    "campaign_before": compact_budget(before),
                    "campaign_after": compact_budget(after),
                    "cumulative_ceiling_respected": after["committed_usd"] <= CUMULATIVE_CEILING,
                    "seven_day_medium_chaos_result": False,
                    "actor_and_reviewer_prompts_unchanged": True,
                    "actor_reviewer_configuration": {
                        "model": actor_reviewer.model,
                        **actor_reviewer.manifest_fields(),
                    },
                    "actor_review_delivery_metadata": "visible-wait-delivery-v2",
                    "personal_transport": PERSONAL_TRANSPORT,
                    "native_task_binding_validation": "live-owner-task-v4-open-schema",
                    "current_cue_review_protocol": CURRENT_CUE_REVIEW_PROTOCOL,
                    "requested_max_actions": args.max_actions,
                    "stop_reason": (
                        "budget_checkpoint"
                        if budget_checkpoint
                        else "failure:" + failure
                        if failure
                        else "horizon"
                        if summary["clock_status"] == "horizon"
                        else "operator_pause"
                        if summary["clock_status"] == "paused"
                        else "action_limit"
                    ),
                }
            )
            save_private(directory / invocation_id / "report.json", summary)
            save_private(directory / "latest.json", summary)
            print("Saved report: " + str(directory / invocation_id / "report.json"), flush=True)
        if failure:
            raise SystemExit(
                "Stopped safely: "
                + failure
                + ". Original model/tool evidence and cost reservations are retained."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--no-chaos", action="store_true")
    parser.add_argument("--readiness", type=Path)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--event-profile", choices=("mixed", "daily-meeting", "daily-confirmation"), default="mixed"
    )
    parser.add_argument("--events-per-day", type=int)
    parser.add_argument("--seed", type=int, default=20261109)
    parser.add_argument("--max-actions", type=int, default=100)
    parser.add_argument("--confirmation-short", action="store_true")
    asyncio.run(main(parser.parse_args()))
