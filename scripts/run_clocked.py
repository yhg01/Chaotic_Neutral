"""Run a clocked scenario with personal Agents, owners, and a live world, and save its counts.

Story time comes from saved outputs, never from real time. A manifest in .local/scenario-runs
holds the credentials, so --resume continues the same run from its checkpoints. --scripted
runs the same engine in process with no server and no provider calls.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from dotenv import dotenv_values
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_scenario import (  # noqa: E402
    ScenarioRunError,
    claim_runner,
    load_credentials,
    loopback_url,
    save_observer_report,
    save_private,
)
from try_clock_control import ScriptedOwner, ScriptModel  # noqa: E402

from agentrpg.actor import ModelActor  # noqa: E402
from agentrpg.agent_model import UsageTracker, create_model, local_model_calls  # noqa: E402
from agentrpg.agent_surface import INSTRUCTIONS, NATURAL, clock_instructions  # noqa: E402
from agentrpg.agent_tools import BoardTools  # noqa: E402
from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices  # noqa: E402
from agentrpg.clock import ClockConfig, agent_process  # noqa: E402
from agentrpg.clock_engine import ClockEngine  # noqa: E402
from agentrpg.clock_gate import ClockGate  # noqa: E402
from agentrpg.deep_agent import AgentRuntime  # noqa: E402
from agentrpg.director import Director, rows  # noqa: E402
from agentrpg.director_brain import DirectorBrain, ScriptedDirectorBrain  # noqa: E402
from agentrpg.environment_tools import EnvironmentTools  # noqa: E402
from agentrpg.scenario_schema import ScenarioSpec  # noqa: E402
from agentrpg.trial_audit import begin_trial, end_trial  # noqa: E402
from agentrpg.web_tools import WebTools  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "openai/gpt-4.1-mini"
TIMEOUT_SECONDS = 240


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--scenario", type=Path, help="A ScenarioSpec JSON file with a clock.")
    source.add_argument("--resume", type=Path, help="A saved credentials.json of this run.")
    parser.add_argument("--drama", choices=("off", "low", "medium", "high"))
    parser.add_argument("--model", help=f"Assistant model. Default: {DEFAULT_MODEL}.")
    parser.add_argument("--actor-model", help="Owner model. Default: the assistant model.")
    parser.add_argument("--director-model", help="World model. Default: the owner model.")
    parser.add_argument("--campaign", type=UUID, help="An existing campaign with a USD ceiling.")
    parser.add_argument("--budget-usd", type=Decimal, help="The persistent ceiling of a campaign.")
    parser.add_argument("--advance-minutes", type=int, help="Stop after this much story time.")
    parser.add_argument("--progress-seconds", type=float, default=30.0)
    parser.add_argument("--base-url", type=loopback_url, help="Local API. Default: port 8000.")
    parser.add_argument(
        "--scripted", action="store_true", help="No server and no provider calls: a free check."
    )
    args = parser.parse_args(argv)
    if args.advance_minutes is not None and args.advance_minutes <= 0:
        parser.error("--advance-minutes must be positive.")
    if args.budget_usd is not None and args.budget_usd <= 0:
        parser.error("--budget-usd must be positive.")
    return args


class ScriptedDay:
    """A free stand-in for an owner: states the request once, then lives quietly."""

    def __init__(self):
        self.owner = ScriptedOwner()
        self.last_attempts = []

    async def respond(self, view):
        initial = not any(m.role in {"owner", "assistant"} for m in view.conversation)
        self.owner.turns = [
            {"kind": "reply", "text": view.request[:8000]}
            if initial
            else {
                "kind": "wait",
                "next_action": {
                    "description": "An ordinary few hours",
                    "minutes": 240,
                    "reachable": True,
                    "category": "free",
                },
            }
        ]
        return await self.owner.respond(view)


def load_scenario(path: Path, drama: str | None) -> ScenarioSpec:
    try:
        data = json.loads(path.read_text())
        spec = ScenarioSpec.model_validate(data)
        if spec.clock is None:
            raise ScenarioRunError("This scenario has no clock. Use scripts/run_scenario.py.")
        if drama is not None:
            data["clock"]["drama"] = drama
            spec = ScenarioSpec.model_validate(data)
    except (OSError, ValueError):
        raise ScenarioRunError("The scenario file is invalid.") from None
    return spec


def new_configuration(args, env, spec: ScenarioSpec) -> dict:
    model = args.model or env.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    actor_model = args.actor_model or env.get("OPENROUTER_ACTOR_MODEL") or model
    return {
        "engine": "clock",
        "agent_surface": NATURAL,
        "actors": "scripted" if args.scripted else "model",
        "model": model,
        "actor_model": actor_model,
        "director_model": args.director_model or actor_model,
        "actor_temperature": 0.8,
        "actor_review": False,
        "actor_max_repairs": 2,
        "max_model_calls": spec.clock.limits.max_model_calls_per_activation,
        "timeout_seconds": TIMEOUT_SECONDS,
        "instructions": INSTRUCTIONS + "\n\n" + clock_instructions(spec.clock.epoch_minutes),
        "base_url": args.base_url or "http://127.0.0.1:8000",
        "scripted": bool(args.scripted),
    }


async def clock_report(pool, run_id, config: ClockConfig) -> dict:
    """Counts of what happened. Saved evidence stays in PostgreSQL and is not copied here."""
    async with pool.connection() as conn:
        run = await (
            await conn.execute(
                "SELECT status,current_window,max_windows,result,seed FROM environment_runs "
                "WHERE id=%s",
                (run_id,),
            )
        ).fetchone()
        state = await (
            await conn.execute("SELECT sim_s,pop_seq FROM clock_state WHERE run_id=%s", (run_id,))
        ).fetchone()
        events = await rows(
            conn,
            "SELECT kind,count(*) AS n FROM run_events WHERE run_id=%s GROUP BY kind ORDER BY kind",
            (run_id,),
        )
        jobs = await rows(
            conn,
            "SELECT owner_id,kind,status,count(*) AS n,sum(attempts) AS failed_attempts "
            "FROM run_jobs WHERE run_id=%s GROUP BY 1,2,3 ORDER BY 1,2,3",
            (run_id,),
        )
        tools = await rows(
            conn,
            "SELECT payload->>'tool' AS tool,count(*) AS n FROM run_events WHERE run_id=%s "
            "AND kind='assistant_tool_call' GROUP BY 1 ORDER BY 1",
            (run_id,),
        )
        processes = await rows(
            conn,
            "SELECT owner_id,kind,state,bar_s,activation_seq FROM clock_processes WHERE run_id=%s "
            "ORDER BY 1,2",
            (run_id,),
        )
        actions = await rows(
            conn,
            "SELECT process,status,count(*) AS n FROM clock_actions WHERE run_id=%s "
            "GROUP BY 1,2 ORDER BY 1,2",
            (run_id,),
        )
        draws = await rows(
            conn,
            "SELECT outcome,count(*) AS n FROM clock_draws WHERE run_id=%s GROUP BY 1 ORDER BY 1",
            (run_id,),
        )
        balances = await rows(
            conn,
            "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s ORDER BY owner_id",
            (run_id,),
        )
    counts = {item["kind"]: item["n"] for item in events}
    return {
        "run_id": str(run_id),
        "engine": "clock",
        "status": run["status"],
        "result": run["result"],
        "seed": run["seed"],
        "story": {
            "sim_s": state["sim_s"],
            "now": config.now_text(state["sim_s"]),
            "window": run["current_window"],
            "max_windows": run["max_windows"],
            "pops": state["pop_seq"],
        },
        "events": counts,
        "failures": {
            kind: counts.get(kind, 0)
            for kind in (
                "simulator_error",
                "job_failure",
                "activation_abandoned",
                "actor_effect_rejected",
                "director_fallback",
                "process_limit_reached",
                "assistant_expired",
            )
        },
        "jobs": [{**item, "failed_attempts": int(item["failed_attempts"] or 0)} for item in jobs],
        "tool_calls": {item["tool"]: item["n"] for item in tools},
        "processes": processes,
        "actions": actions,
        "drama_draws": {item["outcome"]: item["n"] for item in draws},
        "balances": {item["owner_id"]: item["balance"] for item in balances},
    }


async def _progress(pool, run_id, config, seconds, binding) -> None:
    while True:
        await asyncio.sleep(seconds)
        async with pool.connection() as conn:
            state = await (
                await conn.execute("SELECT sim_s FROM clock_state WHERE run_id=%s", (run_id,))
            ).fetchone()
            run = await (
                await conn.execute(
                    "SELECT status,current_window,max_windows FROM environment_runs WHERE id=%s",
                    (run_id,),
                )
            ).fetchone()
        spend = ""
        if binding is not None:
            summary = await binding.ledger.summary(binding.campaign_id, recent_limit=0)
            spend = f" spent ${summary['committed_usd']} of ${summary['limit_usd']}"
        print(
            f"{config.now_text(state['sim_s'])} window {run['current_window']}/"
            f"{run['max_windows']} {run['status']}{spend}",
            flush=True,
        )


async def _open_app(stack, env) -> object:
    """The API in this process, for a scripted check that needs no server."""
    from agentrpg.config import Settings
    from agentrpg.main import create_app

    app = create_app(
        Settings(
            _env_file=None,
            database_url=env["DATABASE_URL"],
            admin_token=env.get("ADMIN_TOKEN") or secrets.token_urlsafe(32),
        )
    )
    await stack.enter_async_context(app.router.lifespan_context(app))
    return app


def _client(base_url, app, headers):
    if app is not None:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://clock.test/api/v1/",
            headers=headers,
            timeout=30,
        )
    return httpx.AsyncClient(
        base_url=f"{base_url}/api/v1/",
        timeout=30,
        trust_env=False,
        follow_redirects=False,
        headers=headers,
    )


async def execute(args, *, root: Path = ROOT, env: dict | None = None) -> dict:
    env = env if env is not None else {**dotenv_values(root / ".env"), **os.environ}
    credentials_path, manifest = (
        load_credentials(args.resume, root) if args.resume else (None, None)
    )
    if manifest is not None:
        config = manifest["configuration"]
        if config.get("engine") != "clock":
            raise ScenarioRunError("This manifest belongs to a window run.")
        for option in ("model", "actor_model", "director_model"):
            if getattr(args, option) not in {None, config[option]}:
                raise ScenarioRunError("Resume uses the run's saved models.")
        if args.drama is not None:
            raise ScenarioRunError("Resume uses the run's frozen drama level.")
        scripted = config.get("scripted", False)
    else:
        scripted = args.scripted
        config = None
    required = (
        ("DATABASE_URL",) if scripted else ("DATABASE_URL", "ADMIN_TOKEN", "OPENROUTER_API_KEY")
    )
    if manifest is None and not scripted and args.budget_usd is None:
        raise ScenarioRunError("A paid run needs --budget-usd, with --campaign to reuse a ceiling.")
    if any(not env.get(key) for key in required):
        raise ScenarioRunError(f"Set {', '.join(required)} locally.")
    spec_file = None if manifest is not None else load_scenario(args.scenario, args.drama)
    if config is None:
        config = new_configuration(args, env, spec_file)
    started = time.monotonic()
    usage = UsageTracker()
    async with AsyncExitStack() as stack:
        pool_size = 32
        runtime = await stack.enter_async_context(
            AgentRuntime(env["DATABASE_URL"], pool_size=pool_size)
        )
        app = await _open_app(stack, env) if scripted else None
        base_url = None if scripted else loopback_url(args.base_url or config["base_url"])
        saved_budget = config.get("budget")
        if saved_budget:
            if args.budget_usd is not None and args.budget_usd != Decimal(
                saved_budget["limit_usd"]
            ):
                raise ScenarioRunError("Resume must keep the saved budget limit.")
            if args.campaign is not None and str(args.campaign) != saved_budget["campaign_id"]:
                raise ScenarioRunError("Resume must keep the saved campaign.")
        elif args.budget_usd is not None:
            config["budget"] = saved_budget = {
                "campaign_id": str(args.campaign or uuid4()),
                "limit_usd": str(args.budget_usd),
            }
        binding = None
        if saved_budget and not scripted:
            ledger = CampaignLedger(runtime.pool)
            await ledger.ensure_campaign(
                saved_budget["campaign_id"], Decimal(saved_budget["limit_usd"])
            )
            prices = await fetch_model_prices(
                {config["model"], config["actor_model"], config["director_model"]}
            )
            binding = BudgetBinding(ledger, UUID(saved_budget["campaign_id"]), prices)
        if manifest is None:
            if scripted:
                manifest = await Director(app.state.pool).create_run(spec_file)
            else:
                admin = await stack.enter_async_context(
                    _client(base_url, None, {"Authorization": f"Bearer {env['ADMIN_TOKEN']}"})
                )
                response = await admin.post("runs", json=spec_file.model_dump(mode="json"))
                if response.status_code != 201:
                    raise ScenarioRunError(f"Run creation failed with HTTP {response.status_code}.")
                manifest = response.json()
            manifest["configuration"] = config
            credentials_path = (
                root / ".local/scenario-runs" / str(UUID(manifest["run_id"])) / "credentials.json"
            )
            save_private(credentials_path, manifest)
            save_private(
                credentials_path.parent / "scenario.json", spec_file.model_dump(mode="json")
            )
        elif saved_budget:
            save_private(credentials_path, manifest)
        await claim_runner(stack, runtime.pool, manifest["run_id"])
        print(f"Run: {manifest['run_id']}\nResume: {credentials_path}", flush=True)
        run_id = UUID(manifest["run_id"])
        director = Director(runtime.pool)
        try:
            run = await director.get_run(run_id)
        except ValueError:
            raise ScenarioRunError(
                "The API and Agent runtime must use the same database."
            ) from None
        if str(run["cluster_id"]) != manifest["cluster_id"]:
            raise ScenarioRunError("The API and saved run identities do not match.")
        spec = ScenarioSpec.model_validate(run["scenario"])
        if spec.clock is None:
            raise ScenarioRunError("This run has no clock. Use scripts/run_scenario.py.")
        if run["scenario_hash"] != spec.content_hash():
            raise ScenarioRunError("The saved scenario hash does not match its content.")
        clock_config = ClockConfig.from_section(spec.clock.model_dump(mode="json"), spec.seed)
        gate = ClockGate(clock_config)
        if scripted:
            brain = ScriptedDirectorBrain()
        else:
            director_model = create_model(
                env["OPENROUTER_API_KEY"],
                config["director_model"],
                max_tokens=3500,
                temperature=0.8,
                **({"budget": binding, "budget_role": "director"} if binding else {}),
            )
            stack.push_async_callback(director_model.aclose)
            brain = DirectorBrain(runtime.pool, director_model, usage=usage)
        agents, actors = {}, {}
        for participant in manifest["participants"]:
            owner = participant["owner_id"]
            client = await stack.enter_async_context(
                _client(base_url, app, {"Authorization": f"Bearer {participant['token']}"})
            )
            if scripted:
                model = ScriptModel(resting_status="responded")
                actors[owner] = ScriptedDay()
            else:
                model = create_model(
                    env["OPENROUTER_API_KEY"],
                    config["model"],
                    max_tokens=2000,
                    **({"budget": binding, "budget_role": "assistant"} if binding else {}),
                )
                stack.push_async_callback(model.aclose)
                actor_model = create_model(
                    env["OPENROUTER_API_KEY"],
                    config["actor_model"],
                    max_tokens=2000,
                    temperature=config["actor_temperature"],
                    **({"budget": binding, "budget_role": "actor"} if binding else {}),
                )
                stack.push_async_callback(actor_model.aclose)
                actors[owner] = ModelActor(
                    actor_model,
                    reviewer=None,
                    max_repairs=config["actor_max_repairs"],
                    natural=True,
                )
            agents[owner] = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"], direct=True),
                environment=EnvironmentTools(client, manifest["run_id"]),
                agent_id=participant["agent_id"],
                session_id=participant["session_id"],
                instructions=config["instructions"],
                max_model_calls=config["max_model_calls"],
                timeout_seconds=config["timeout_seconds"],
                callbacks=[usage],
                surface=NATURAL,
                gate=gate,
                process=agent_process(owner),
                web=WebTools(runtime.pool, manifest["run_id"], owner, brain),
            )
            await agents[owner]._verify_identity()
        engine = ClockEngine(runtime.pool, actors, agents, brain)
        if run["status"] == "paused":
            await director.control(run_id, "resume")
        elif run["status"] == "pending":
            await director.control(run_id, "start")
        invocation = await begin_trial(
            runtime.pool, run_id, profile="clocked", configuration=config, root=ROOT
        )
        printer = asyncio.create_task(
            _progress(runtime.pool, run_id, clock_config, args.progress_seconds, binding)
        )
        execution_error = None
        bounded = False
        try:
            with local_model_calls():
                if args.advance_minutes is None:
                    await engine.run(run_id)
                else:
                    current = await engine.advance(run_id, max_sim_minutes=args.advance_minutes)
                    if current["status"] == "running":
                        bounded = True
                        async with runtime.pool.connection() as conn:
                            await conn.execute(
                                "UPDATE environment_runs SET status='paused',result=%s "
                                "WHERE id=%s AND status='running'",
                                (
                                    Jsonb(
                                        {"reason": "advance_limit", "minutes": args.advance_minutes}
                                    ),
                                    run_id,
                                ),
                            )
        except Exception as error:
            execution_error = f"Run execution failed: {type(error).__name__}. Saved work is kept."
        finally:
            printer.cancel()
            await asyncio.gather(printer, return_exceptions=True)
        await end_trial(
            runtime.pool, run_id, invocation, root=ROOT, returned_report=execution_error is None
        )
        report = await clock_report(runtime.pool, run_id, clock_config)
        report["execution_error"] = execution_error
        report["stop_reason"] = "advance_limit" if bounded else report["status"]
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report["peak_model_calls_in_flight"] = gate.peak_calls_in_flight
        report["usage"] = {
            "requests": usage.requests,
            "completed": usage.completed,
            "failed": usage.failed,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "reported_cost_usd": usage.reported_cost_usd,
            "peak_in_flight": usage.peak_in_flight,
        }
        report["scripted"] = bool(scripted)
        if binding is not None:
            report["campaign_budget"] = await binding.ledger.summary(binding.campaign_id)
        save_private(credentials_path.parent / "report.json", report)
        await save_observer_report(runtime.pool, report)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "stop_reason": report["stop_reason"],
                    "story": report["story"],
                    "failures": report["failures"],
                    "report": str(credentials_path.parent / "report.json"),
                },
                indent=2,
                default=str,
            ),
            flush=True,
        )
        if execution_error:
            raise ScenarioRunError(execution_error)
        return report


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        raise SystemExit("Run interrupted. Resume with the saved credentials.json file.") from None
    except ScenarioRunError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit(
            "The clocked run failed. Check the local API, database, and provider. "
            "Use the saved credentials.json file to resume."
        ) from None


if __name__ == "__main__":
    main()
