"""Try the ZeroClaw trial backend with real models, in a kept schema of the test database.

This is a bounded trial, not a campaign run. The models are called at their providers directly.
Hard caps on calls and tokens stop the run, and the report gives measured token counts and a
cost computed from them with the price table in the budget code. With `--budget-usd`, every
call also goes through a campaign ledger in the trial's schema, with that USD ceiling. Keys come
from the local `.env` file and are never printed.

The run uses a new schema in the test database and keeps it, so the saved records can be read
afterwards. It never touches the main database.

    AGENTRPG_ZEROCLAW=/path/to/zeroclaw .venv/bin/python scripts/try_zeroclaw_live.py \\
        --name day1 --scenario scenarios/five_lives_week.json --drama low --advance-minutes 720

Use `--scenario control` for the three-owner control story with scripted owners and no Director.
Use `--backend deepagents` for the same run on the present runtime.
Use `--resume` with the same `--name` to continue a run that stopped. The caps count all parts.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import try_clock_control as control  # noqa: E402
from run_clocked import load_scenario  # noqa: E402
from run_scenario import save_private  # noqa: E402

from agentrpg.actor import ModelActor  # noqa: E402
from agentrpg.agent_model import local_model_calls  # noqa: E402
from agentrpg.agent_surface import INSTRUCTIONS, NATURAL, clock_instructions  # noqa: E402
from agentrpg.agent_tools import BoardTools  # noqa: E402
from agentrpg.budget import (  # noqa: E402
    BudgetBinding,
    BudgetError,
    BudgetTransport,
    CampaignLedger,
    direct_prices,
    direct_provider,
)
from agentrpg.clock import ClockConfig, agent_process  # noqa: E402
from agentrpg.clock_engine import ClockEngine  # noqa: E402
from agentrpg.clock_gate import ClockGate  # noqa: E402
from agentrpg.config import Settings  # noqa: E402
from agentrpg.deep_agent import AgentRuntime  # noqa: E402
from agentrpg.direct_model import DirectChatModel  # noqa: E402
from agentrpg.director import Director  # noqa: E402
from agentrpg.director_brain import DirectorBrain  # noqa: E402
from agentrpg.environment_tools import EnvironmentTools  # noqa: E402
from agentrpg.main import create_app  # noqa: E402
from agentrpg.migrate import migrate  # noqa: E402
from agentrpg.scenario_schema import ScenarioSpec  # noqa: E402
from agentrpg.web_tools import WebTools  # noqa: E402
from agentrpg.zeroclaw_agent import (  # noqa: E402
    HARNESS,
    MODES,
    ZeroClawBridge,
    zeroclaw_factory,
)


@dataclass
class Meter:
    """Calls and tokens of each role, with hard caps. A cap stops the run like a budget stop."""

    caps: dict[str, int]
    max_tokens: int
    calls: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    input_tokens: dict[str, int] = field(default_factory=dict)
    output_tokens: dict[str, int] = field(default_factory=dict)
    cached_tokens: dict[str, int] = field(default_factory=dict)
    largest_input: dict[str, int] = field(default_factory=dict)
    first_request: dict[str, dict] = field(default_factory=dict)

    def before(self, role: str) -> None:
        total = sum(self.input_tokens.values()) + sum(self.output_tokens.values())
        if self.calls.get(role, 0) >= self.caps.get(role, 0) or total >= self.max_tokens:
            raise BudgetError(f"The trial cap for {role} model calls or tokens is reached.")
        self.calls[role] = self.calls.get(role, 0) + 1

    def after(self, role: str, usage: dict) -> None:
        sent = int(usage.get("prompt_tokens") or 0)
        self.input_tokens[role] = self.input_tokens.get(role, 0) + sent
        self.output_tokens[role] = self.output_tokens.get(role, 0) + int(
            usage.get("completion_tokens") or 0
        )
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        self.cached_tokens[role] = self.cached_tokens.get(role, 0) + int(cached)
        self.largest_input[role] = max(self.largest_input.get(role, 0), sent)

    def load(self, saved: dict) -> None:
        """Take the counts of the earlier parts of a run, so the caps cover all of them."""
        for name, key in (
            ("calls", "calls"),
            ("failed", "failed_calls"),
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cached_tokens", "cached_input_tokens"),
            ("largest_input", "largest_single_input"),
        ):
            setattr(self, name, dict(saved.get(key) or {}))

    def report(self) -> dict:
        return {
            "calls": self.calls,
            "failed_calls": self.failed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_tokens,
            "largest_single_input": self.largest_input,
        }


def computed_cost(meter: Meter, models: dict[str, str]) -> dict[str, str]:
    """The cost of each role's tokens at the price of that role's main model."""
    prices = direct_prices(set(models.values()))
    costs = {}
    for role, model in models.items():
        price, cached = prices[model], meter.cached_tokens.get(role, 0)
        costs[role] = price.maximum(
            meter.input_tokens.get(role, 0), meter.output_tokens.get(role, 0)
        )
        if price.cached is not None:
            costs[role] -= (price.prompt - price.cached) * cached
    return {**{role: str(cost) for role, cost in costs.items()}, "total": str(sum(costs.values()))}


def model_for(
    spec: str, role: str, env: dict, meter: Meter, *, budget=None, **settings
) -> DirectChatModel:
    """spec is provider:model, for example deepseek:deepseek-flash."""
    found = direct_provider(spec)
    if found is None:
        raise SystemExit("A model is written as provider:model, from the budget price table.")
    _, provider, name = found
    if not env.get(provider.key_name):
        raise SystemExit(f"Set {provider.key_name} in the local .env file.")
    extra = {**settings.pop("extra", {}), **provider.fixed}
    if not provider.json_reply:
        extra.pop("response_format", None)
    if not provider.temperature:
        settings["temperature"] = None
    model = DirectChatModel(
        endpoint=provider.url,
        provider_model=name,
        key=SecretStr(env[provider.key_name]),
        role=role,
        meter=meter,
        extra=extra,
        **settings,
    )
    if budget is not None:
        if spec not in budget.prices:
            raise SystemExit("The ledger has no price for this model.")
        model._client = httpx.AsyncClient(
            transport=BudgetTransport(budget, role),
            follow_redirects=False,
            timeout=settings.get("timeout", 120),
            trust_env=False,
        )
    return model


def test_schema_url(env: dict, name: str, *, resume: bool = False) -> tuple[str, str, str]:
    """A URL for a new schema in the test database, and the names of both."""
    configured = env.get("TEST_DATABASE_URL")
    if not configured:
        parameters = conninfo_to_dict(env["DATABASE_URL"])
        parameters["dbname"] = f"{parameters['dbname']}_test"
        configured = make_conninfo(**parameters)
    database = conninfo_to_dict(configured).get("dbname", "")
    if "test" not in database.lower():
        raise SystemExit("The trial needs a test database. Its name must contain 'test'.")
    schema = "zclive_" + "".join(c if c.isalnum() else "_" for c in name.lower())
    if not resume:
        with psycopg.connect(configured, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    return make_conninfo(configured, options=f"-c search_path={schema}"), database, schema


async def counts(pool, run_id) -> dict:
    async with pool.connection() as conn:
        kinds = await (
            await conn.execute(
                "SELECT kind, count(*) AS n FROM run_events WHERE run_id=%s GROUP BY kind",
                (run_id,),
            )
        ).fetchall()
        tools = await (
            await conn.execute(
                "SELECT payload->>'tool' AS tool, count(*) AS n, "
                "count(*) FILTER (WHERE (payload->'result'->>'ok')='false') AS refused "
                "FROM run_events WHERE run_id=%s AND kind='assistant_tool_call' GROUP BY 1",
                (run_id,),
            )
        ).fetchall()
        failures = await (
            await conn.execute(
                "SELECT payload->>'failure_kind' AS kind, payload->>'detail' AS detail, "
                "count(*) AS n FROM run_events WHERE run_id=%s AND kind='job_failure' "
                "GROUP BY 1,2",
                (run_id,),
            )
        ).fetchall()
        run = await (
            await conn.execute(
                "SELECT r.status, r.result, s.sim_s FROM environment_runs r "
                "JOIN clock_state s ON s.run_id=r.id WHERE r.id=%s",
                (run_id,),
            )
        ).fetchone()
        tasks = await (
            await conn.execute(
                "SELECT count(*) AS n, max(model_calls) AS most, sum(model_calls) AS total "
                "FROM agent_tasks"
            )
        ).fetchone()
    return {
        "status": run["status"],
        "result": run["result"],
        "story_seconds": run["sim_s"],
        "events": {row["kind"]: row["n"] for row in kinds},
        "tool_calls": {row["tool"]: [row["n"], row["refused"]] for row in tools},
        "failed_attempts": [dict(row) for row in failures],
        "assistant_tasks": dict(tasks),
    }


async def deepseek_balance(env: dict) -> float | None:
    """The account's balance in CNY, or None when the provider does not say."""
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {env['DEEPSEEK_API_KEY']}"},
            )
        for item in response.json()["balance_infos"]:
            if item["currency"] == "CNY":
                return float(item["total_balance"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return None
    return None


async def progress(pool, run_id, config, meter, seconds, folder, env, floor) -> None:
    while True:
        await asyncio.sleep(seconds)
        # Saved often, so a part that is killed still counts toward the caps.
        save_private(folder / "meter.json", meter.report())
        async with pool.connection() as conn:
            state = await (
                await conn.execute("SELECT sim_s FROM clock_state WHERE run_id=%s", (run_id,))
            ).fetchone()
        total = sum(meter.input_tokens.values()) + sum(meter.output_tokens.values())
        balance = await deepseek_balance(env) if floor else None
        if balance is not None and balance < floor:
            # The account is near the floor that was set. The next model call stops the run.
            meter.max_tokens = 0
        print(
            f"{config.now_text(state['sim_s'])} calls {meter.calls} tokens {total}"
            + (f" balance CNY {balance}" if balance is not None else ""),
            flush=True,
        )


async def execute(args, env: dict) -> dict:
    meter = Meter(
        caps={
            "assistant": args.max_assistant_calls,
            "owner": args.max_owner_calls,
            "director": args.max_director_calls,
            # A site model has its own count, so its tokens are not priced as the Director's.
            "site": args.max_director_calls if args.site_model else 0,
        },
        max_tokens=args.max_tokens,
    )
    scripted_owners = args.scenario == "control"
    spec = (
        control.scenario()
        if scripted_owners
        else load_scenario(Path(args.scenario), args.drama, args.seed)
    )
    folder = ROOT / ".local" / "zeroclaw-live" / args.name
    saved = args.saved
    if args.saved_meter:
        meter.load(args.saved_meter)
    url, database, schema = test_schema_url(env, args.name, resume=args.resume)
    await migrate(url)
    started = time.monotonic()
    async with AsyncExitStack() as stack:
        app = create_app(
            Settings(_env_file=None, database_url=url, admin_token="trial-" + os.urandom(24).hex())
        )
        await stack.enter_async_context(app.router.lifespan_context(app))
        runtime = await stack.enter_async_context(AgentRuntime(url, pool_size=24))
        if saved is None:
            manifest = await Director(app.state.pool).create_run(spec)
            # The participants' board tokens. Private, and needed to continue the run.
            save_private(folder / "credentials.json", manifest)
        else:
            manifest = saved
            # The frozen scenario of the run, not the file, which can have changed.
            run = await Director(app.state.pool).get_run(UUID(manifest["run_id"]))
            spec = ScenarioSpec.model_validate(run["scenario"])
        run_id = UUID(manifest["run_id"])
        print(f"Run: {run_id} in schema {schema}", flush=True)
        binding = None
        if args.budget_usd is not None:
            # One campaign for one trial name, so a resumed trial keeps its ceiling and spend.
            campaign = uuid5(NAMESPACE_URL, f"zeroclaw-live:{args.name}")
            ledger = CampaignLedger(runtime.pool)
            await ledger.ensure_campaign(campaign, args.budget_usd, name=f"Trial {args.name}")
            used = {args.assistant_model, args.owner_model, args.director_model}
            binding = BudgetBinding(
                ledger, campaign, direct_prices(used | ({args.site_model} - {None}))
            )
        config = ClockConfig.from_section(spec.clock.model_dump(mode="json"), spec.seed)
        gate = ClockGate(config)
        brain = None
        if not scripted_owners:
            brain = DirectorBrain(
                runtime.pool,
                model_for(
                    args.director_model, "director", env, meter, budget=binding, max_tokens=5000
                ),
                timeout_seconds=120,
                site_model=(
                    model_for(args.site_model, "site", env, meter, budget=binding, max_tokens=5000)
                    if args.site_model
                    else None
                ),
            )
        bridge = None
        if args.backend == "zeroclaw":
            # A kept folder, so a resumed run loads each program's session again.
            bridge = await stack.enter_async_context(ZeroClawBridge(root=folder / "zeroclaw"))
            make = zeroclaw_factory(bridge, mode=args.mode)
        else:

            def make(runtime, **arguments):
                return runtime.agent(**arguments)

        owners = control.control_scripts(None)[0] if scripted_owners else {}
        agents, actors = {}, {}
        for participant in manifest["participants"]:
            owner = participant["owner_id"]
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://clock.test/api/v1/",
                    headers={"Authorization": f"Bearer {participant['token']}"},
                    timeout=30,
                )
            )
            actors[owner] = (
                owners[owner]
                if scripted_owners
                else ModelActor(
                    model_for(
                        args.owner_model,
                        "owner",
                        env,
                        meter,
                        budget=binding,
                        max_tokens=2000,
                        temperature=0.8,
                        extra={"response_format": {"type": "json_object"}},
                    ),
                    reviewer=None,
                    max_repairs=2,
                    natural=True,
                )
            )
            agents[owner] = make(
                runtime,
                model=model_for(
                    args.assistant_model,
                    "assistant",
                    env,
                    meter,
                    budget=binding,
                    max_tokens=4000,
                    temperature=0,
                ),
                board=BoardTools(client, manifest["cluster_id"], direct=True),
                environment=EnvironmentTools(client, manifest["run_id"]),
                agent_id=participant["agent_id"],
                session_id=participant["session_id"],
                instructions=INSTRUCTIONS + "\n\n" + clock_instructions(spec.clock.epoch_minutes),
                max_model_calls=config.max_model_calls,
                timeout_seconds=args.timeout_seconds,
                surface=NATURAL,
                gate=gate,
                process=agent_process(owner),
                web=None
                if scripted_owners
                else WebTools(runtime.pool, manifest["run_id"], owner, brain),
            )
            await agents[owner]._verify_identity()
        engine = ClockEngine(runtime.pool, actors, agents, brain)
        status = (await Director(runtime.pool).get_run(run_id))["status"]
        if status == "pending":
            await Director(runtime.pool).control(run_id, "start")
        elif status == "paused":
            await Director(runtime.pool).control(run_id, "resume")
        printer = asyncio.create_task(
            progress(
                runtime.pool,
                run_id,
                config,
                meter,
                args.progress_seconds,
                folder,
                env,
                args.deepseek_floor_cny,
            )
        )
        error = None
        try:
            with local_model_calls():
                if args.advance_minutes is None:
                    await engine.run(run_id)
                else:
                    await engine.advance(run_id, max_sim_minutes=args.advance_minutes)
        except Exception as failure:  # The saved work stays. The report says what stopped it.
            error = type(failure).__name__
        finally:
            printer.cancel()
            await asyncio.gather(printer, return_exceptions=True)
            save_private(folder / "meter.json", meter.report())
        report = {
            "name": args.name,
            "run_id": str(run_id),
            "database": database,
            "schema": schema,
            "scenario": args.scenario,
            "drama": spec.clock.drama,
            "backend": args.backend,
            "mode": args.mode if args.backend == "zeroclaw" else None,
            "models": {
                "assistant": args.assistant_model,
                "owner": None if scripted_owners else args.owner_model,
                "director": None if scripted_owners else args.director_model,
            },
            "execution_error": error,
            "story_time": None,
            "real_seconds": round(time.monotonic() - started, 1),
            "peak_model_calls_in_flight": gate.peak_calls_in_flight,
            "model_use": meter.report(),
            # Reported tokens at the dearest listed price. An upper bound, not an invoice.
            "ledger": (
                {
                    key: str(value)
                    for key, value in (await binding.ledger.summary(binding.campaign_id)).items()
                    if key.endswith("_usd") or key in ("attempts", "halted")
                }
                if binding is not None
                else None
            ),
            "computed_cost_usd": computed_cost(
                meter,
                {
                    "assistant": args.assistant_model,
                    "owner": args.owner_model,
                    "director": args.director_model,
                    **({"site": args.site_model} if args.site_model else {}),
                },
            ),
            **(await counts(runtime.pool, run_id)),
            "zeroclaw": {
                owner: vars(agent.stats)
                for owner, agent in agents.items()
                if hasattr(agent, "stats")
            },
            "zeroclaw_folder": str(bridge.root) if bridge is not None else None,
            "first_request": meter.first_request,
        }
        report["story_time"] = config.now_text(report["story_seconds"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--name", required=True, help="Names the schema and the report folder.")
    parser.add_argument("--scenario", default="control", help="A scenario file, or control.")
    parser.add_argument("--drama", choices=("off", "low", "medium", "high"))
    parser.add_argument("--seed", type=int)
    parser.add_argument("--advance-minutes", type=int)
    parser.add_argument("--backend", choices=("zeroclaw", "deepagents"), default="zeroclaw")
    parser.add_argument("--mode", choices=MODES, default=HARNESS)
    parser.add_argument("--assistant-model", default="deepseek:deepseek-flash")
    parser.add_argument("--owner-model", default="deepseek:deepseek-flash")
    parser.add_argument("--director-model", default="anthropic:claude-opus-5-5")
    parser.add_argument(
        "--site-model", help="A smaller model that builds a public site for a search."
    )
    parser.add_argument("--max-assistant-calls", type=int, default=300)
    parser.add_argument("--max-owner-calls", type=int, default=150)
    parser.add_argument("--max-director-calls", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=3_000_000)
    parser.add_argument(
        "--budget-usd", type=Decimal, help="A USD ceiling in a campaign ledger for this trial."
    )
    parser.add_argument("--timeout-seconds", type=float, default=240)
    parser.add_argument("--progress-seconds", type=float, default=30)
    parser.add_argument("--resume", action="store_true", help="Continue the run of this name.")
    parser.add_argument(
        "--deepseek-floor-cny",
        type=float,
        default=0,
        help="Stop when the DeepSeek balance is below this amount. 0 means no check.",
    )
    args = parser.parse_args()
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    folder = ROOT / ".local" / "zeroclaw-live" / args.name
    args.saved = args.saved_meter = None
    if args.resume:
        args.saved = json.loads((folder / "credentials.json").read_text())
        if (folder / "meter.json").is_file():
            args.saved_meter = json.loads((folder / "meter.json").read_text())
    elif (folder / "credentials.json").exists():
        raise SystemExit("A run of this name exists. Use --resume or another name.")
    report = asyncio.run(execute(args, env))
    save_private(folder / "report.json", report)
    shown = {k: v for k, v in report.items() if k not in {"first_request", "events", "tool_calls"}}
    print(json.dumps(shown, indent=1, default=str))
    print(f"Report: {folder / 'report.json'}")


if __name__ == "__main__":
    main()
