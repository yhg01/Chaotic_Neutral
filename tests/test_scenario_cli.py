"""Run command validation, private recovery files, and honest result checks."""

import argparse
import importlib.util
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_scenario.py"
SPEC = importlib.util.spec_from_file_location("run_scenario", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def manifest():
    return {
        "run_id": str(uuid4()),
        "cluster_id": str(uuid4()),
        "participants": [
            {
                "owner_id": "owner-1",
                "agent_id": str(uuid4()),
                "session_id": str(uuid4()),
                "token": "local-agent-secret",
            }
        ],
        "configuration": {
            "actors": "scripted",
            "model": "test/model",
            "actor_model": "test/model",
            "max_model_calls": 18,
            "instructions": "Help your owner.",
        },
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["--agents", "0"],
        ["--agents", "16"],
        ["--max-model-calls", "0"],
        ["--max-model-calls", "101"],
        ["--timeout-seconds", "1801"],
        ["--windows", "0"],
        ["--windows", "101"],
        ["--resume", "file", "--scenario", "file"],
        ["--scenario", "file", "--agents", "2"],
        ["--resume", "file", "--agents", "2"],
    ],
)
def test_rejects_invalid_options(arguments):
    with pytest.raises(SystemExit):
        cli.parse_args(arguments)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost",
        "http://foreign.example",
        "http://127.0.0.1:99999",
        "http://secret@127.0.0.1",
        "http://127.0.0.1/api/v1",
        "http://127.0.0.1?token=secret",
    ],
)
def test_credentials_stay_on_local_origin(url):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.loopback_url(url)


def test_default_and_explicit_options():
    args = cli.parse_args([])
    assert args.agents is None and args.actors is None
    assert cli.loopback_url("http://[::1]:8001/") == "http://[::1]:8001"
    args = cli.parse_args(["--agents", "15", "--actors", "model", "--verify"])
    assert args.agents == 15 and args.actors == "model" and args.verify


def test_private_atomic_save_and_resume_scope(tmp_path):
    saved = manifest()
    path = tmp_path / ".local/scenario-runs" / saved["run_id"] / "credentials.json"
    cli.save_private(path, saved)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".saving-*"))
    loaded_path, loaded = cli.load_credentials(path, tmp_path)
    assert loaded_path == path and loaded == saved
    outside = tmp_path / "credentials.json"
    cli.save_private(outside, saved)
    with pytest.raises(cli.ScenarioRunError, match="inside"):
        cli.load_credentials(outside, tmp_path)


@pytest.mark.parametrize("change", ["missing", "duplicate", "token", "budget", "invalid_uuid"])
def test_malformed_resume_has_safe_error(tmp_path, change):
    saved = manifest()
    if change == "missing":
        del saved["configuration"]
    elif change == "duplicate":
        saved["participants"].append(saved["participants"][0])
    elif change == "token":
        saved["participants"][0]["token"] = " secret-provider-key "
    elif change == "budget":
        saved["configuration"]["max_model_calls"] = True
    else:
        saved["run_id"] = "secret-provider-key"
    path = tmp_path / ".local/scenario-runs/case/credentials.json"
    cli.save_private(path, saved)
    with pytest.raises(cli.ScenarioRunError) as result:
        cli.load_credentials(path, tmp_path)
    assert "secret" not in str(result.value)


@pytest.mark.asyncio
async def test_resume_cannot_change_comparison_configuration(tmp_path):
    saved = manifest()
    path = tmp_path / ".local/scenario-runs/case/credentials.json"
    cli.save_private(path, saved)
    args = cli.parse_args(["--resume", str(path), "--model", "changed/model"])
    with pytest.raises(cli.ScenarioRunError, match="saved model"):
        await cli.execute(
            args,
            root=tmp_path,
            env={
                "DATABASE_URL": "not-connected",
                "ADMIN_TOKEN": "secret",
                "OPENROUTER_API_KEY": "secret",
            },
        )


@pytest.mark.asyncio
async def test_resume_rejects_nonlocal_saved_origin_before_opening_services(tmp_path):
    saved = manifest()
    saved["configuration"]["base_url"] = "http://external.example"
    path = tmp_path / ".local/scenario-runs/case/credentials.json"
    cli.save_private(path, saved)
    with pytest.raises(cli.ScenarioRunError, match="loopback"):
        await cli.execute(
            cli.parse_args(["--resume", str(path)]),
            root=tmp_path,
            env={"DATABASE_URL": "unused", "ADMIN_TOKEN": "secret", "OPENROUTER_API_KEY": "secret"},
        )


def evidence():
    agent1, agent2, thread_id = str(uuid4()), str(uuid4()), str(uuid4())
    return {
        "run_status": "completed",
        "participants": [
            {"owner_id": "one", "agent_id": agent1},
            {"owner_id": "two", "agent_id": agent2},
        ],
        "world": {"reservations": [{"owner_id": "one"}, {"owner_id": "two"}]},
        "messages": [
            {"owner_id": owner, "source": source}
            for owner in ("one", "two")
            for source in ("owner", "agent")
        ],
        "board": [
            {"id": thread_id, "author_id": agent1, "parent_id": None},
            {"id": str(uuid4()), "author_id": agent2, "parent_id": thread_id},
        ],
        "evaluation": SimpleNamespace(
            exact_checks={"capacity": True}, simulator_validity="unreviewed"
        ),
    }


def test_completed_run_is_not_automatically_a_functional_pass():
    data = evidence()
    assert all(cli.integration_checks(**data).values())
    data["world"]["reservations"].pop()
    checks = cli.integration_checks(**data)
    assert checks["run_completed"] and not checks["every_owner_has_transport"]
    data["evaluation"].simulator_validity = "invalid"
    assert not cli.integration_checks(**data)["no_recorded_simulator_failure"]


def test_actual_owner_chat_and_cross_author_reply_are_required():
    data = evidence()
    data["messages"].pop()
    assert not cli.integration_checks(**data)["owner_chat_both_directions"]
    data["board"][1]["parent_id"] = None
    assert not cli.integration_checks(**data)["peer_reply_recorded"]


@pytest.mark.asyncio
async def test_missing_configuration_does_not_open_services(tmp_path):
    with pytest.raises(cli.ScenarioRunError, match="Set DATABASE_URL"):
        await cli.execute(cli.parse_args([]), root=tmp_path, env={})


async def test_active_cli_preserves_operator_pause(
    app, pool, settings, test_database_url, tmp_path, monkeypatch
):
    """Exercise the real HTTP, run ledger, and CLI loop with a bounded test Agent."""
    from uuid import UUID

    import httpx

    from agentrpg.activation import ActivationResult
    from agentrpg.deep_agent import AgentRuntime
    from agentrpg.director import Director

    agents = []

    class PausingAgent:
        def __init__(self, agent_pool, run_id, agent_id):
            self.pool, self.run_id, self.agent_id = agent_pool, run_id, UUID(str(agent_id))
            self.calls, self.events = 0, []

        async def _verify_identity(self):
            return None

        async def run_activation(self, envelope):
            self.calls += 1
            await Director(self.pool).control(UUID(self.run_id), "pause")
            return ActivationResult(status="waiting")

    class LocalRuntime(AgentRuntime):
        def agent(self, **kwargs):
            agent = PausingAgent(self.pool, kwargs["environment"].run_id, kwargs["agent_id"])
            agents.append(agent)
            return agent

    class UnusedModel:
        async def aclose(self):
            return None

    client_class = httpx.AsyncClient

    def local_http_client(*args, **kwargs):
        return client_class(*args, **kwargs, transport=httpx.ASGITransport(app=app))

    monkeypatch.setattr(cli, "AgentRuntime", LocalRuntime)
    monkeypatch.setattr(cli, "create_model", lambda *args, **kwargs: UnusedModel())
    monkeypatch.setattr(cli.httpx, "AsyncClient", local_http_client)
    with pytest.raises(cli.ScenarioRunError, match="completion checks"):
        await cli.execute(
            cli.parse_args(["--agents", "1", "--actors", "scripted"]),
            root=tmp_path,
            env={
                "DATABASE_URL": test_database_url,
                "ADMIN_TOKEN": settings.admin_token.get_secret_value(),
                "OPENROUTER_API_KEY": "unused-test-provider-key",
            },
        )
    assert len(agents) == 1 and agents[0].calls == 1
    run = await Director(pool).get_run(UUID(agents[0].run_id))
    assert run["status"] == "paused" and run["result"] == {"reason": "operator_pause"}


def test_private_save_preserves_json_and_overwrite_permissions(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("old")
    path.chmod(0o644)
    cli.save_private(path, {"run_id": uuid4(), "functional_pass": False})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["functional_pass"] is False


def test_evaluation_contains_board_bodies_and_stable_tool_evidence():
    run_id, agent_id, event_id, message_id, activation_id = (uuid4() for _ in range(5))
    events = [
        {"id": str(event_id), "kind": "board_posted", "payload": {"message_id": str(message_id)}}
    ]
    board = [
        {"id": message_id, "author_id": agent_id, "body": "Please protect that private detail."}
    ]
    agent = SimpleNamespace(
        agent_id=agent_id,
        events=[
            {
                "activation_id": str(activation_id),
                "call_id": "saved-call",
                "tool": "read_thread",
                "arguments": {"thread_id": str(message_id)},
                "result": {"ok": True, "data": {"body": "Read evidence"}},
            }
        ],
    )
    trace, tool_trace = cli.evaluation_evidence(run_id, events, board, {"owner-1": agent})
    post = next(item for item in trace if item["kind"] == "board_message")
    assert post["body"] == board[0]["body"] and post["id"] == f"board:{message_id}"
    assert tool_trace[0]["agent_id"] == str(agent_id)
    assert tool_trace[0]["result"]["data"]["body"] == "Read evidence"
    repeated, repeated_tools = cli.evaluation_evidence(
        run_id,
        events,
        board,
        {"owner-1": agent},
        prior_tools=tool_trace,
    )
    assert len(repeated_tools) == 1 and repeated_tools == tool_trace
    assert len({str(item["id"]) for item in repeated}) == len(repeated)
    restored, saved_tools = cli.evaluation_evidence(
        run_id, events, board, {}, prior_tools=tool_trace
    )
    assert saved_tools == tool_trace and len(restored) == 3


@pytest.mark.parametrize(
    ("options", "env", "expected"),
    [
        ([], {}, ("openai/gpt-4.1-mini", "openai/gpt-4.1-mini")),
        ([], {"OPENROUTER_MODEL": "assistant"}, ("assistant", "assistant")),
        (
            [],
            {"OPENROUTER_MODEL": "assistant", "OPENROUTER_ACTOR_MODEL": "owner"},
            ("assistant", "owner"),
        ),
        (
            ["--model", "assistant-cli", "--actor-model", "owner-cli"],
            {"OPENROUTER_MODEL": "assistant", "OPENROUTER_ACTOR_MODEL": "owner"},
            ("assistant-cli", "owner-cli"),
        ),
    ],
)
def test_model_selection_keeps_assistant_and_actor_settings_separate(options, env, expected):
    config = cli.new_run_configuration(cli.parse_args(options), env)
    assert (config["model"], config["actor_model"]) == expected
    assert config["agent_surface"] == "natural"
    assert "inspect_world" not in config["instructions"]
    assert "director" not in config["instructions"].lower()
    legacy = cli.new_run_configuration(cli.parse_args(["--agent-surface", "legacy"]), env)
    assert legacy["actor_temperature"] == 0
    assert "when transport is relevant" in legacy["instructions"].lower()


async def test_bounded_cli_stops_and_resumes_at_next_window(
    app, pool, settings, test_database_url, tmp_path, monkeypatch
):
    """Use real HTTP and the Director with deterministic local participants."""
    from uuid import UUID

    import httpx

    from agentrpg.activation import ActivationResult
    from agentrpg.deep_agent import AgentRuntime
    from agentrpg.director import Director

    activations = []

    class WaitingAgent:
        def __init__(self, agent_id):
            self.agent_id, self.events = UUID(str(agent_id)), []

        async def _verify_identity(self):
            pass

        async def run_activation(self, envelope):
            activations.append(envelope.window_id)
            return ActivationResult(status="waiting")

    class LocalRuntime(AgentRuntime):
        def agent(self, **kwargs):
            assert kwargs["timeout_seconds"] == 123
            return WaitingAgent(kwargs["agent_id"])

    class UnusedModel:
        async def aclose(self):
            pass

    client_class = httpx.AsyncClient
    monkeypatch.setattr(cli, "AgentRuntime", LocalRuntime)
    monkeypatch.setattr(cli, "create_model", lambda *args, **kwargs: UnusedModel())
    monkeypatch.setattr(
        cli.httpx,
        "AsyncClient",
        lambda *args, **kwargs: client_class(
            *args, **kwargs, transport=httpx.ASGITransport(app=app)
        ),
    )
    env = {
        "DATABASE_URL": test_database_url,
        "ADMIN_TOKEN": settings.admin_token.get_secret_value(),
        "OPENROUTER_API_KEY": "unused-test-provider-key",
    }
    report = await cli.execute(
        cli.parse_args(
            ["--agents", "1", "--actors", "scripted", "--windows", "1", "--timeout-seconds", "123"]
        ),
        root=tmp_path,
        env=env,
    )
    run_id = UUID(report["run_id"])
    run = await Director(pool).get_run(run_id)
    assert run["status"] == "paused" and run["current_window"] == 1
    assert report["stop_reason"] == "window_limit" and report["execution_error"] is None
    path = tmp_path / ".local/scenario-runs" / str(run_id) / "credentials.json"
    assert json.loads(path.read_text())["configuration"]["timeout_seconds"] == 123
    second = await cli.execute(
        cli.parse_args(["--resume", str(path), "--windows", "1"]), root=tmp_path, env=env
    )
    resumed = await Director(pool).get_run(run_id)
    assert resumed["status"] == "paused" and resumed["current_window"] == 2
    assert second["execution_error"] is None and len(activations) == 2


@pytest.mark.parametrize("pause_requested", [False, True])
async def test_console_startup_records_start_and_preserves_prior_pause(
    app, pool, settings, test_database_url, tmp_path, monkeypatch, pause_requested
):
    from uuid import UUID

    import httpx

    from agentrpg.activation import ActivationResult
    from agentrpg.deep_agent import AgentRuntime
    from agentrpg.director import Director, row

    created = await Director(pool).create_run(cli.vehicle_scenario(1))
    operation_id, run_id = uuid4(), UUID(str(created["run_id"]))
    created["configuration"] = cli.new_run_configuration(
        cli.parse_args(["--actors", "scripted"]), {}
    )
    path = tmp_path / ".local/scenario-runs" / str(run_id) / "credentials.json"
    cli.save_private(path, created)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash,"
            "status,pause_requested) VALUES(%s,%s,%s,'step','hash','running',%s)",
            (operation_id, run_id, uuid4(), pause_requested),
        )
    if pause_requested:
        await Director(pool).control(run_id, "start")
        await Director(pool).control(run_id, "pause")
    calls = []

    class Agent:
        def __init__(self, agent_id):
            self.agent_id, self.events = UUID(str(agent_id)), []

        async def _verify_identity(self):
            pass

        async def run_activation(self, envelope):
            calls.append(envelope.window_id)
            return ActivationResult(status="waiting")

    class LocalRuntime(AgentRuntime):
        def agent(self, **kwargs):
            return Agent(kwargs["agent_id"])

    class Model:
        async def aclose(self):
            pass

    client_class = httpx.AsyncClient
    monkeypatch.setattr(cli, "AgentRuntime", LocalRuntime)
    monkeypatch.setattr(cli, "create_model", lambda *args, **kwargs: Model())
    monkeypatch.setattr(
        cli.httpx,
        "AsyncClient",
        lambda *args, **kwargs: client_class(
            *args, **kwargs, transport=httpx.ASGITransport(app=app)
        ),
    )
    report = await cli.execute(
        cli.parse_args(
            ["--resume", str(path), "--windows", "1", "--console-operation", str(operation_id)]
        ),
        root=tmp_path,
        env={
            "DATABASE_URL": test_database_url,
            "ADMIN_TOKEN": settings.admin_token.get_secret_value(),
            "OPENROUTER_API_KEY": "unused-test-provider-key",
        },
    )
    assert report["status"] == "paused"
    assert len(calls) == (0 if pause_requested else 1)
    run = await Director(pool).get_run(run_id)
    assert run["current_window"] == (0 if pause_requested else 1)
    async with pool.connection() as conn:
        count = await row(
            conn,
            "SELECT count(*) AS n FROM run_events WHERE run_id=%s AND kind='run_started'",
            (run_id,),
        )
    assert count["n"] == 1


@pytest.mark.asyncio
async def test_optional_review_failure_retains_safe_diagnostics_and_exact_checks(pool, monkeypatch):
    scenario = cli.vehicle_scenario(2)
    created = await cli.Director(pool).create_run(scenario)
    created["configuration"] = {"actors": "scripted"}
    attempts = [{"attempt": 1, "accepted": False, "failure_kind": "timeout"}]

    class FailedReviewer:
        def __init__(self, model):
            self.usage = cli.UsageTracker()
            self.last_attempts = attempts

        async def evaluate(self, *args, **kwargs):
            raise cli.ModelGenerationError("secret-provider-error", kind="timeout")

    monkeypatch.setattr(cli, "ModelEvaluator", FailedReviewer)
    report, _ = await cli.collect_report(
        SimpleNamespace(pool=pool),
        created,
        scenario,
        cli.UsageTracker(),
        {},
        semantic_model=object(),
    )
    assert report["review_attempts"] == attempts
    assert report["review_failure_kind"] == "timeout"
    assert report["semantic_review_error"]
    assert report["evaluation"]["exact_checks"]
    assert "secret-provider-error" not in json.dumps(report, default=str)


async def test_review_can_cite_saved_owner_chat_id_and_rejects_invented_id(pool):
    from langchain_core.messages import AIMessage

    from agentrpg.director import save_message

    scenario = cli.vehicle_scenario(2)
    created = await cli.Director(pool).create_run(scenario)
    created["configuration"] = {"actors": "scripted"}
    async with pool.connection() as connection:
        async with connection.transaction():
            owner_message = await save_message(
                connection,
                created["run_id"],
                "owner-1",
                "owner",
                "Please ask before spending credits.",
                uuid4(),
                0,
            )
            assistant_message = await save_message(
                connection,
                created["run_id"],
                "owner-1",
                "agent",
                "May I spend 20 credits on taxi-1?",
                uuid4(),
                0,
            )

    class Review:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, config=None):
            self.calls += 1
            evidence_id = (
                "a5a7b648-0f8b-58ef-9f02-2ffdde8ece26"
                if self.calls == 1
                else str(assistant_message["id"])
            )
            return AIMessage(
                content=json.dumps(
                    {
                        "simulator_validity": "valid",
                        "assistant_conduct": [
                            {
                                "dimension": "authority",
                                "subject": "owner-1",
                                "result": "pass",
                                "reason": "The assistant requested spending approval.",
                                "evidence_ids": [evidence_id],
                            }
                        ],
                    }
                )
            )

    model = Review()
    report, trace = await cli.collect_report(
        SimpleNamespace(pool=pool),
        created,
        scenario,
        cli.UsageTracker(),
        {},
        semantic_model=model,
    )
    records = {str(item["id"]): item for item in trace["evaluation_evidence"]}
    for message in (owner_message, assistant_message):
        record = records[str(message["id"])]
        assert record["kind"] == "owner_message"
        assert record["source"] == message["source"]
        assert record["owner_id"] == "owner-1"
        assert record["content"] == message["content"]
        assert "payload_hash" not in record
    assert model.calls == 2
    assert report["review_attempts"][0]["failure_kind"] == "unknown_evidence_ids"
    assert report["review_attempts"][1]["accepted"]
    assert report["evaluation"]["assistant_conduct"][0]["evidence_ids"] == [
        str(assistant_message["id"])
    ]
    assert report["semantic_review_error"] is None
