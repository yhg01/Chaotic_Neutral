"""Check that story order does not depend on real time. No provider is called.

Three owners run with scripted models and random injected latency, repeatedly, with the
same seed. Their committed tool calls and effects, with random IDs normalised, must be
byte-identical for every latency and for one or eight parallel model calls.

Run with a separate PostgreSQL test database:

    TEST_DATABASE_URL=... .venv/bin/python scripts/try_clock_control.py --repeats 3
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agentrpg.actor import ActorOutput
from agentrpg.agent_surface import INSTRUCTIONS, NATURAL, clock_instructions
from agentrpg.agent_tools import BoardTools
from agentrpg.clock import ClockConfig, agent_process
from agentrpg.clock_engine import ClockEngine
from agentrpg.clock_gate import ClockGate
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.scenario_schema import ScenarioSpec

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
# A paging cursor encodes a hash of the cluster's random ID and a shared row number.
_CURSOR = re.compile(r'"((?:next_)?cursor)": "[A-Za-z0-9_=-]+"')
# Failed attempts depend on real time. They are diagnostics, not story.
_NOT_STORY = {"job_failure"}


class ScriptModel(BaseChatModel):
    """Replays a list of replies through the real graph, with optional injected latency.

    The reply comes from the number of model messages already in history, not from a call
    counter. A retried or resumed call therefore gets the reply that was lost, as a
    deterministic model would give it.
    """

    script: list[Any] = Field(default_factory=list)
    calls: int = 0
    seen: list[Any] = Field(default_factory=list)
    all_seen: list[Any] = Field(default_factory=list)
    latency: Any = None
    # finished needs every notified email read first. A model with no script cannot do that.
    resting_status: str = "finished"

    @property
    def _llm_type(self):
        return "clock-script"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.seen = messages
        self.all_seen.append(list(messages))
        if self.latency is not None:
            await asyncio.sleep(self.latency())
        index = sum(1 for message in messages if message.type == "ai")
        reply = (
            self.script[index] if index < len(self.script) else end_turn(status=self.resting_status)
        )
        if callable(reply) and not isinstance(reply, AIMessage):
            reply = reply(messages)
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(generations=[ChatGeneration(message=reply)])


def calls(*items, text=""):
    """One model reply with several tool calls. Each item is (name, args) or (name, args, id)."""
    tool_calls = []
    for index, item in enumerate(items):
        name, args = item[0], item[1]
        call_id = item[2] if len(item) > 2 else f"{name}-{uuid_free(args)}-{index}"
        tool_calls.append({"name": name, "args": args, "id": call_id, "type": "tool_call"})
    return AIMessage(content=text, tool_calls=tool_calls)


def uuid_free(args) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:10]


def end_turn(text="", status="finished", wake_in_minutes=None, **fields):
    arguments = {"status": status, "text": text, **fields}
    if wake_in_minutes is not None:
        arguments["wake_in_minutes"] = wake_in_minutes
    return calls(("end_turn", arguments))


class ScriptedOwner:
    """A person who follows a list of turns, then rests. One frozen view gets one turn."""

    def __init__(self, turns=(), latency=None, *, by_spoken=False):
        self.turns = list(turns)
        self.views = []
        self.latency = latency
        self.last_attempts = []
        self.assigned = {}
        # Choose by what this person has already said, so a new process picks the same turn.
        self.by_spoken = by_spoken

    async def respond(self, view):
        self.views.append(view)
        if self.latency is not None:
            await asyncio.sleep(self.latency())
        if self.by_spoken:
            spoken = sum(1 for message in view.conversation if message.role == "owner")
            turn = self.turns[spoken] if spoken < len(self.turns) else {"kind": "wait"}
        else:
            if view.source_event_id not in self.assigned:
                self.assigned[view.source_event_id] = (
                    self.turns.pop(0) if self.turns else {"kind": "wait"}
                )
            turn = self.assigned[view.source_event_id]
        if callable(turn):
            turn = turn(view)
        if isinstance(turn, Exception):
            raise turn
        return ActorOutput(source_event_id=view.source_event_id, **turn)


def _owner(owner_id, name):
    return {
        "id": owner_id,
        "name": name,
        "request": f"Please help {name} plan the week.",
        "goals": ["Get the week in order."],
        "max_spend": 50,
    }


def scenario(owners=("dan", "mei", "sam"), *, seed=7, **clock):
    """A short clocked day. Keyword arguments replace parts of the clock section."""
    section = {
        "start_at": "2026-11-09T06:00:00+00:00",
        "duration_minutes": 360,
        "epoch_minutes": 60,
        "daily_wake": "08:00",
        "director": {"enabled": False},
        **clock,
    }
    windows = -(-section["duration_minutes"] // section["epoch_minutes"])
    return ScenarioSpec.model_validate(
        {
            "id": "clock-control",
            "name": "Clock control",
            "owners": [_owner(owner, owner.title()) for owner in owners],
            "beats": [
                {
                    "id": f"start-{owner}",
                    "trigger": {"kind": "run_started"},
                    "recipients": [owner],
                    "cue": "The week is starting. Tell your assistant what you need.",
                }
                for owner in owners
            ],
            "world": {
                "assets": [{"id": "car", "name": "Shared car", "price": 5}],
                "balances": dict.fromkeys(owners, 100),
            },
            "max_windows": windows,
            "seed": seed,
            "clock": section,
        }
    )


def control_scripts(latency):
    """The three owners of the latency control, with tool chains that interleave."""
    owners = {
        "dan": ScriptedOwner(
            [
                {
                    "kind": "reply",
                    "text": "Ask Mei if she can lend me the drill on Saturday.",
                    "next_action": {
                        "description": "Drive to work",
                        "minutes": 45,
                        "reachable": False,
                        "category": "work",
                    },
                },
                {
                    "kind": "reply",
                    "text": "Good. Tell her thanks.",
                    "next_action": {
                        "description": "At work",
                        "minutes": 120,
                        "reachable": True,
                        "category": "work",
                    },
                },
            ],
            latency,
            by_spoken=True,
        ),
        "mei": ScriptedOwner(
            [
                {
                    "kind": "reply",
                    "text": "Find out what the car costs today.",
                    "next_action": {
                        "description": "Morning run",
                        "minutes": 30,
                        "reachable": False,
                        "category": "errand",
                    },
                },
                {"kind": "reply", "text": "He can have the drill."},
            ],
            latency,
            by_spoken=True,
        ),
        "sam": ScriptedOwner(
            [
                {
                    "kind": "reply",
                    "text": "Post a note that I have spare tomatoes.",
                    "next_action": {
                        "description": "Gardening",
                        "minutes": 60,
                        "reachable": True,
                        "category": "errand",
                    },
                },
            ],
            latency,
            by_spoken=True,
        ),
    }
    models = {
        "dan": ScriptModel(
            latency=latency,
            script=[
                calls(("get_my_info", {})),
                calls(
                    (
                        "send_mail",
                        {
                            "recipient_owner_id": "mei",
                            "subject": "Drill",
                            "body": "Could Dan borrow your drill on Saturday?",
                        },
                    ),
                    ("send_owner_message", {"content": "I asked Mei about the drill."}),
                ),
                end_turn(status="waiting", wait_for=["inbox_received"]),
                calls(("list_mail", {"mailbox": "inbox"})),
                end_turn(text="Mei says yes.", wake_in_minutes=30),
                end_turn(status="finished"),
                end_turn(status="finished"),
                end_turn(status="finished"),
            ],
        ),
        "mei": ScriptModel(
            latency=latency,
            script=[
                calls(("check_bookings", {})),
                end_turn(text="The car is 5 dollars.", status="responded"),
                calls(("list_mail", {"mailbox": "inbox"})),
                calls(
                    ("send_owner_message", {"content": "Dan wants your drill on Saturday."}),
                    ("read_resources", {}),
                ),
                end_turn(status="waiting", wait_for=["owner_message"]),
                calls(
                    (
                        "send_mail",
                        {
                            "recipient_owner_id": "dan",
                            "subject": "Re: Drill",
                            "body": "Yes, he can have it.",
                        },
                    )
                ),
                end_turn(status="finished"),
                end_turn(status="finished"),
            ],
        ),
        "sam": ScriptModel(
            latency=latency,
            script=[
                calls(("post_thread", {"title": "Spare tomatoes", "body": "Sam has plenty."})),
                end_turn(text="Posted it.", status="finished", wake_in_minutes=20),
                calls(("list_threads", {})),
                end_turn(status="finished"),
                end_turn(status="finished"),
                end_turn(status="finished"),
            ],
        ),
    }
    return owners, models


@asynccontextmanager
async def clocked_run(
    app,
    database_url,
    spec,
    models,
    actors,
    *,
    brain=None,
    parallel=None,
    pool_size=16,
    timeout_seconds=30,
    retry_base_seconds=0.01,
    created=None,
    web=False,
    agent_factory=None,
):
    """Open a run with real graphs and a real API. A saved created dict resumes that run.

    agent_factory(runtime, **arguments) builds each assistant. The default is the Deep Agents
    runtime; another backend takes the same arguments.
    """
    make_agent = agent_factory or (lambda runtime, **arguments: runtime.agent(**arguments))
    if parallel is not None:
        section = spec.clock.model_dump(mode="json")
        section["limits"]["max_parallel_thinkers"] = parallel
        spec = ScenarioSpec.model_validate({**spec.model_dump(mode="json"), "clock": section})
    director = Director(app.state.pool)
    created = created or await director.create_run(spec)
    run = await director.get_run(created["run_id"])
    saved = ScenarioSpec.model_validate(run["scenario"])
    config = ClockConfig.from_section(saved.clock.model_dump(mode="json"), saved.seed)
    gate = ClockGate(config)
    async with AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(AgentRuntime(database_url, pool_size=pool_size))
        agents = {}
        for participant in created["participants"]:
            owner = participant["owner_id"]
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test/api/v1/",
                    headers={"Authorization": f"Bearer {participant['token']}"},
                    timeout=30,
                )
            )
            adapter = None
            if web:
                from agentrpg.web_tools import WebTools

                adapter = WebTools(runtime.pool, created["run_id"], owner, brain)
            agents[owner] = make_agent(
                runtime,
                model=models[owner],
                board=BoardTools(client, created["cluster_id"], direct=True),
                environment=EnvironmentTools(client, created["run_id"]),
                agent_id=participant["agent_id"],
                session_id=participant["session_id"],
                instructions=INSTRUCTIONS + "\n\n" + clock_instructions(saved.clock.epoch_minutes),
                max_model_calls=config.max_model_calls,
                timeout_seconds=timeout_seconds,
                surface=NATURAL,
                gate=gate,
                process=agent_process(owner),
                web=adapter,
            )
        engine = ClockEngine(
            runtime.pool, actors, agents, brain, retry_base_seconds=retry_base_seconds
        )
        yield engine, created, runtime, agents


def normalise(value):
    """Replace random IDs by first appearance and real times by a mark. Keep everything else."""
    seen: dict[str, str] = {}

    def identity(match):
        return seen.setdefault(match[0], f"id-{len(seen)}")

    text = json.dumps(value, sort_keys=True, default=str)
    text = _CURSOR.sub(r'"\1": "C"', _MOMENT.sub("T", text))
    return _UUID.sub(identity, text)


async def story_trace(pool, run_id) -> list:
    """Every committed event in story order, with its story second and cause."""
    async with pool.connection() as conn:
        state = await (
            await conn.execute("SELECT epoch FROM clock_state WHERE run_id=%s", (run_id,))
        ).fetchone()
        rows = await (
            await conn.execute(
                "SELECT kind,owner_id,cause_process,payload,created_at FROM run_events "
                "WHERE run_id=%s ORDER BY created_at,id",
                (run_id,),
            )
        ).fetchall()
        pops = await (
            await conn.execute(
                "SELECT kind,key,due_s,class FROM clock_events WHERE run_id=%s AND status='done' "
                "ORDER BY popped_seq,key",
                (run_id,),
            )
        ).fetchall()
        tools = await (
            await conn.execute(
                "SELECT label,start_s,duration_s,key,status FROM clock_actions WHERE run_id=%s "
                "ORDER BY start_s,key",
                (run_id,),
            )
        ).fetchall()
    events = [
        {
            "at": int((item["created_at"] - state["epoch"]).total_seconds()),
            "kind": item["kind"],
            "owner": item["owner_id"],
            "cause": item["cause_process"],
            "payload": item["payload"],
        }
        for item in rows
        if item["kind"] not in _NOT_STORY
    ]
    return [events, pops, tools]


def trace_text(trace) -> str:
    return normalise(trace)


def trace_digest(trace) -> str:
    return hashlib.sha256(trace_text(trace).encode()).hexdigest()


async def run_control(
    app, database_url, *, latency_seed, parallel, max_latency=0.03, agent_factory=None
):
    generator = random.Random(latency_seed)
    latency = (lambda: generator.random() * max_latency) if max_latency else None
    actors, models = control_scripts(latency)
    spec = scenario()
    async with clocked_run(
        app, database_url, spec, models, actors, parallel=parallel, agent_factory=agent_factory
    ) as (
        engine,
        created,
        _runtime,
        _agents,
    ):
        run = await engine.run(created["run_id"])
        trace = await story_trace(app.state.pool, created["run_id"])
    return run, trace, models, actors


@asynccontextmanager
async def _isolated_schema(url):
    """A generated schema that this check owns, so no shared table is touched."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    if "test" not in conninfo_to_dict(url).get("dbname", "").lower():
        raise SystemExit("TEST_DATABASE_URL must name a database containing 'test'.")
    schema = "clockcontrol_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as control:
        await control.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(url, options=f"-c search_path={schema}")
        finally:
            await control.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _application(url):
    from agentrpg.config import Settings
    from agentrpg.main import create_app

    return create_app(
        Settings(_env_file=None, database_url=url, admin_token=f"control-{uuid4().hex}" * 2)
    )


async def _compare(url, repeats) -> int:
    from agentrpg.migrate import migrate

    await migrate(url)
    app = _application(url)
    digests = {}
    async with app.router.lifespan_context(app):
        for parallel in (1, 8):
            for repeat in range(repeats):
                run, trace, _models, _actors = await run_control(
                    app, url, latency_seed=repeat, parallel=parallel
                )
                digest = trace_digest(trace)
                digests[(parallel, repeat)] = digest
                print(f"parallel={parallel} latency_seed={repeat} {run['status']} {digest[:16]}")
    same = len(set(digests.values())) == 1
    print("Story order was byte-identical." if same else "Story order differed.")
    return 0 if same else 1


def _read_saved(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


async def _worker(url, state_path: Path) -> int:
    """One process of a crash test. It creates the run or resumes the saved one."""
    app = _application(url)
    actors, models = control_scripts(None)
    created = await asyncio.to_thread(_read_saved, state_path)
    async with app.router.lifespan_context(app):
        if created is None:
            created = await Director(app.state.pool).create_run(scenario())
            await asyncio.to_thread(state_path.write_text, json.dumps(created))
        async with clocked_run(app, url, scenario(), models, actors, created=created) as (
            engine,
            created,
            _runtime,
            _agents,
        ):
            run = await engine.run(created["run_id"])
    print(run["status"], flush=True)
    return 0 if run["status"] == "completed" else 1


async def _main(args) -> int:
    url = args.database_url or os.environ.get("TEST_DATABASE_URL")
    if not url:
        print("Set TEST_DATABASE_URL to a separate PostgreSQL test database.", file=sys.stderr)
        return 2
    if args.worker:
        return await _worker(url, args.worker)
    async with _isolated_schema(url) as isolated:
        return await _compare(isolated, args.repeats)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--database-url", help=argparse.SUPPRESS)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
