"""Try the ZeroClaw trial backend with real models, in a kept schema of the test database.

This is a bounded trial, not a campaign run. The models are called at their providers directly,
so the campaign ledger is not used. Hard caps on calls and tokens stop the run instead, and the
report gives measured token counts. Keys come from the local `.env` file and are never printed.

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
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg
from dotenv import dotenv_values
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import Field, SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import try_clock_control as control  # noqa: E402
from run_clocked import load_scenario  # noqa: E402
from run_scenario import save_private  # noqa: E402

from agentrpg.actor import ModelActor  # noqa: E402
from agentrpg.agent_model import ProviderCallError, local_model_calls  # noqa: E402
from agentrpg.agent_surface import INSTRUCTIONS, NATURAL, clock_instructions  # noqa: E402
from agentrpg.agent_tools import BoardTools  # noqa: E402
from agentrpg.budget import BudgetError  # noqa: E402
from agentrpg.clock import ClockConfig, agent_process  # noqa: E402
from agentrpg.clock_engine import ClockEngine  # noqa: E402
from agentrpg.clock_gate import ClockGate  # noqa: E402
from agentrpg.config import Settings  # noqa: E402
from agentrpg.deep_agent import AgentRuntime  # noqa: E402
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

PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/chat/completions", "DEEPSEEK_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1/chat/completions", "ANTHROPIC_API_KEY"),
}
RETRYABLE = frozenset({408, 429, 500, 502, 503, 504, 529})


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


def _wire(messages) -> list[dict]:
    out = []
    for message in messages:
        text = message.content if isinstance(message.content, str) else json.dumps(message.content)
        if message.type == "system":
            out.append({"role": "system", "content": text})
        elif message.type == "human":
            out.append({"role": "user", "content": text})
        elif message.type == "tool":
            out.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": text})
        else:
            item = {"role": "assistant", "content": text}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                    }
                    for call in message.tool_calls
                ]
            out.append(item)
    return out


class DirectChatModel(BaseChatModel):
    """One model at its own provider, through the chat-completions form. One try for each call."""

    endpoint: str
    provider_model: str
    key: SecretStr
    role: str
    meter: Any = None
    max_tokens: int = 4000
    temperature: float | None = None
    extra: dict = Field(default_factory=dict)
    tools: list = Field(default_factory=list)
    timeout: float = 120

    @property
    def _llm_type(self) -> str:
        return "direct-chat"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": [convert_to_openai_tool(tool) for tool in tools]})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        body = {
            "model": self.provider_model,
            "messages": _wire(messages),
            "max_tokens": self.max_tokens,
            "stream": False,
            **self.extra,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.tools:
            body["tools"] = self.tools
        self.meter.before(self.role)
        self.meter.first_request.setdefault(
            self.role,
            {"messages": body["messages"], "tools": [t["function"]["name"] for t in self.tools]},
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    self.endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {self.key.get_secret_value()}"},
                )
        except httpx.HTTPError:
            self.meter.failed[self.role] = self.meter.failed.get(self.role, 0) + 1
            raise ProviderCallError("Model provider connection failed.", retryable=True) from None
        if response.status_code != 200:
            self.meter.failed[self.role] = self.meter.failed.get(self.role, 0) + 1
            raise ProviderCallError(
                f"Model provider returned HTTP {response.status_code}.",
                retryable=response.status_code in RETRYABLE,
                status=response.status_code,
            )
        try:
            data = response.json()
            choice = data["choices"][0]
            reply = choice["message"]
            calls, invalid = [], []
            for call in reply.get("tool_calls") or []:
                name, raw = call["function"]["name"], call["function"].get("arguments") or "{}"
                try:
                    arguments = json.loads(raw)
                    if not isinstance(arguments, dict):
                        raise ValueError
                    calls.append(
                        {"name": name, "args": arguments, "id": call["id"], "type": "tool_call"}
                    )
                except ValueError:
                    invalid.append(
                        {
                            "name": name,
                            "args": raw,
                            "id": call.get("id"),
                            "error": "Arguments are not a JSON object.",
                            "type": "invalid_tool_call",
                        }
                    )
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            self.meter.failed[self.role] = self.meter.failed.get(self.role, 0) + 1
            raise ProviderCallError(
                "Model provider returned an invalid response.", status=0
            ) from None
        self.meter.after(self.role, usage)
        message = AIMessage(
            content=reply.get("content") or "",
            tool_calls=calls,
            invalid_tool_calls=invalid,
            id=data.get("id"),
            response_metadata={
                "finish_reason": choice.get("finish_reason"),
                "model_name": self.provider_model,
            },
            usage_metadata={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def model_for(spec: str, role: str, env: dict, meter: Meter, **settings) -> DirectChatModel:
    """spec is provider:model, for example deepseek:deepseek-flash."""
    provider, _, name = spec.partition(":")
    if provider not in PROVIDERS or not name:
        raise SystemExit("A model is written as provider:model, with deepseek or anthropic.")
    endpoint, key_name = PROVIDERS[provider]
    if not env.get(key_name):
        raise SystemExit(f"Set {key_name} in the local .env file.")
    extra = dict(settings.pop("extra", {}))
    if provider == "deepseek":
        # As on the earlier runs: no thinking text on the small models.
        extra.setdefault("thinking", {"type": "disabled"})
    else:
        extra.pop("response_format", None)
        settings["temperature"] = None
    return DirectChatModel(
        endpoint=endpoint,
        provider_model=name,
        key=SecretStr(env[key_name]),
        role=role,
        meter=meter,
        extra=extra,
        **settings,
    )


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
        config = ClockConfig.from_section(spec.clock.model_dump(mode="json"), spec.seed)
        gate = ClockGate(config)
        brain = None
        if not scripted_owners:
            brain = DirectorBrain(
                runtime.pool,
                model_for(args.director_model, "director", env, meter, max_tokens=5000),
                timeout_seconds=120,
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
                    args.assistant_model, "assistant", env, meter, max_tokens=4000, temperature=0
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
    parser.add_argument("--max-assistant-calls", type=int, default=300)
    parser.add_argument("--max-owner-calls", type=int, default=150)
    parser.add_argument("--max-director-calls", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=3_000_000)
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
