"""Provider and CLI checks without external model requests."""

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langchain_openrouter import ChatOpenRouter
from langsmith.run_helpers import get_tracing_context
from openrouter.errors.openroutererror import OpenRouterError

from agentrpg.agent_model import (
    ProviderCallError,
    UsageTracker,
    create_model,
    local_model_calls,
)
from agentrpg.deep_agent import AgentBusyError, AgentLimitError, AgentTaskConflictError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_agent.py"
SPEC = importlib.util.spec_from_file_location("run_agent", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.fixture
def local_files(tmp_path):
    cluster_id, agent_id = uuid4(), uuid4()
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local/demo-credentials.json").write_text(
        json.dumps(
            {
                "cluster": {"id": str(cluster_id)},
                "agents": [
                    {
                        "agent": {"id": str(agent_id), "cluster_id": str(cluster_id)},
                        "token": "test-agent-secret",
                    }
                ],
            }
        )
    )
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=test-provider-secret\nDATABASE_URL=postgresql://unused/test\n"
    )
    return tmp_path, cluster_id, agent_id


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:8000",
        "http://example.com",
        "http://127.0.0.1.example.com",
        "http://token@127.0.0.1",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:8000/api/v1",
        "http://127.0.0.1?key=value",
        "http://127.0.0.1#fragment",
    ],
)
def test_rejects_nonlocal_or_ambiguous_board_urls(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.loopback_url(value)


@pytest.mark.parametrize(
    "value", ["http://127.0.0.1:8000", "http://localhost", "http://[::1]:8000/"]
)
def test_accepts_loopback_board_urls(value):
    assert cli.loopback_url(value) == value.rstrip("/")


@pytest.mark.parametrize("index", ["0", "16"])
def test_rejects_agent_index_outside_cluster_capacity(index):
    with pytest.raises(SystemExit):
        cli.parse_args(["--agent", index])


def test_credentials_and_default_sessions_are_identity_scoped(local_files):
    root, cluster_id, agent_id = local_files
    assert cli.load_agent(root, 1) == (cluster_id, agent_id, "test-agent-secret")
    assert cli.default_session(cluster_id, agent_id) == cli.default_session(cluster_id, agent_id)
    assert cli.default_session(cluster_id, agent_id) != cli.default_session(cluster_id, uuid4())
    assert cli.default_session(cluster_id, agent_id) != cli.default_session(uuid4(), agent_id)
    with pytest.raises(ValueError, match="no valid local credential"):
        cli.load_agent(root, 2)


def test_rejects_mismatched_cluster_credentials(local_files):
    root, _, _ = local_files
    path = root / ".local/demo-credentials.json"
    data = json.loads(path.read_text())
    data["agents"][0]["agent"]["cluster_id"] = str(uuid4())
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="no valid local credential"):
        cli.load_agent(root, 1)


async def test_factory_enforces_limits_and_hides_credentials(monkeypatch):
    monkeypatch.setenv("OPENROUTER_DEBUG", "true")
    monkeypatch.setenv("OPENROUTER_SESSION_ID", "ambient-session")
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://unexpected.example")
    model = create_model("test-provider-secret")
    try:
        assert model.max_tokens == 1000
        assert model.request_timeout == 45_000
        assert model.client.sdk_configuration.timeout_ms == 45_000
        assert model.client.sdk_configuration.retry_config is None
        assert model.max_retries == 2
        assert model.session_id is None
        assert model.trace is None
        assert model.openrouter_api_base == "https://openrouter.ai/api/v1"
        assert model.client.sdk_configuration.debug_logger.__class__.__name__ == "NoOpLogger"
        assert "test-provider-secret" not in repr(model)
        assert "test-provider-secret" not in json.dumps(model.to_json())
    finally:
        await model.aclose()


@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": ""},
        {"api_key": "test", "max_tokens": 0},
        {"api_key": "test", "max_tokens": 4097},
        {"api_key": "test", "timeout_seconds": 121},
        {"api_key": "test", "max_retries": 3},
        {"api_key": "test", "model": " "},
    ],
)
def test_invalid_model_configuration_fails_before_client_creation(arguments):
    with pytest.raises(ValueError):
        create_model(**arguments)


async def test_provider_retries_transient_errors_with_exact_bound(monkeypatch):
    calls = []

    async def completion(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadError("private response content")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Done"))])

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", completion)
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    model = create_model("test-provider-secret")
    try:
        result = await model._agenerate([])
        assert result.generations[0].message.content == "Done"
        assert len(calls) == 3
    finally:
        await model.aclose()


@pytest.mark.parametrize("status,expected_attempts", [(401, 1), (429, 3), (503, 3)])
async def test_provider_errors_are_sanitized_before_callbacks(
    monkeypatch, status, expected_attempts
):
    attempts = []

    async def failure(*args, **kwargs):
        attempts.append(1)
        raise OpenRouterError(
            "test-provider-secret", httpx.Response(status, text="private-provider-body")
        )

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", failure)
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    model = create_model("test-provider-secret")
    try:
        with pytest.raises(ProviderCallError) as captured:
            await model._agenerate([])
        assert str(captured.value) == f"Model provider returned HTTP {status}."
        assert len(attempts) == expected_attempts
    finally:
        await model.aclose()


async def test_task_cancellation_is_not_retried(monkeypatch):
    calls = []

    async def cancelled(*args, **kwargs):
        calls.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", cancelled)
    model = create_model("test-provider-secret")
    try:
        with pytest.raises(asyncio.CancelledError):
            await model._agenerate([])
        assert len(calls) == 1
    finally:
        await model.aclose()


async def test_trace_context_disables_ambient_tracing_in_child_tasks(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    before = get_tracing_context()

    async def child():
        return get_tracing_context()["enabled"]

    with local_model_calls():
        assert get_tracing_context()["enabled"] is False
        assert await asyncio.create_task(child()) is False
    assert get_tracing_context() == before


async def test_usage_tracks_parallel_models_and_failure_without_message_content():
    usage = UsageTracker()
    runs = [uuid4() for _ in range(15)]
    for run_id in runs:
        await usage.on_chat_model_start({}, [["private-message"]], run_id=run_id)
    result = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="private-response",
                        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                        response_metadata={"cost": 0.001},
                    )
                )
            ]
        ],
        llm_output={"model_name": "test/model"},
    )
    for run_id in runs[:-1]:
        await usage.on_llm_end(result, run_id=run_id)
    await usage.on_llm_error(ValueError("private-error"), run_id=runs[-1])
    await usage.on_llm_end(result, run_id=runs[0])
    await usage.on_llm_error(ValueError("duplicate"), run_id=runs[-1])
    report = usage.summary()
    assert report["requests"] == 15
    assert report["completed_requests"] == 14
    assert report["failed_requests"] == 1
    assert report["peak_concurrent_requests"] == 15
    assert report["input_tokens"] == 140
    assert report["output_tokens"] == 70
    assert report["total_tokens"] == 210
    assert report["reported_cost_usd"] == 0.014
    assert report["cost_reports"] == 14
    assert report["models"] == ["test/model"]
    assert "private" not in repr(vars(usage))


async def test_cli_passes_retry_ids_and_credentials_to_the_correct_boundary(
    monkeypatch, local_files, capsys
):
    root, cluster_id, agent_id = local_files
    task_id, session_id = uuid4(), uuid4()
    recorded = {}

    class FakeModel:
        async def aclose(self):
            recorded["closed"] = True

    def model_factory(key, model):
        recorded["provider_key"] = key
        return FakeModel()

    class FakeAgent:
        async def run(self, task, *, task_id):
            output = capsys.readouterr().out
            assert str(task_id) in output  # Retry ID is visible before work starts.
            assert str(session_id) in output
            assert get_tracing_context()["enabled"] is False
            recorded.update(task=task, task_id=task_id)
            return "Task complete."

    class FakeRuntime:
        def __init__(self, database_url):
            recorded["database_url"] = database_url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def agent(self, **kwargs):
            recorded.update(kwargs)
            return FakeAgent()

    for name in ["OPENROUTER_API_KEY", "DATABASE_URL", "OPENROUTER_MODEL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "create_model", model_factory)
    monkeypatch.setitem(
        sys.modules, "agentrpg.deep_agent", SimpleNamespace(AgentRuntime=FakeRuntime)
    )
    args = cli.parse_args(
        ["--session", str(session_id), "--task-id", str(task_id), "--task", "Read the board."]
    )
    assert await cli.run_task(args, root=root) == "Task complete."
    assert recorded["agent_id"] == agent_id
    assert recorded["session_id"] == session_id
    assert recorded["task_id"] == task_id
    assert recorded["provider_key"] == "test-provider-secret"
    assert recorded["closed"]
    assert recorded["board"].cluster_id == str(cluster_id)
    assert "secret" not in recorded["instructions"]
    output = capsys.readouterr().out
    assert "Task complete." in output
    assert "secret" not in output


def test_cli_does_not_print_arbitrary_exception_details(monkeypatch, capsys):
    async def failure(*args, **kwargs):
        raise RuntimeError("postgresql://user:private-password@localhost/database")

    monkeypatch.setattr(cli, "run_task", failure)
    with pytest.raises(SystemExit) as captured:
        cli.main(["--task", "Read the board."])
    assert "private-password" not in str(captured.value)
    assert "Agent task failed" in str(captured.value)
    assert "private-password" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "error",
    [
        AgentBusyError("This Agent session already has an active run."),
        AgentTaskConflictError("This task ID was used with different text."),
        AgentLimitError("The task reached its time limit. Retry with the same task ID."),
    ],
)
def test_cli_preserves_safe_runtime_error_messages(monkeypatch, error):
    async def failure(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "run_task", failure)
    with pytest.raises(SystemExit) as captured:
        cli.main(["--task", "Read the board."])
    assert str(captured.value) == str(error)


def test_exhausted_budget_explains_required_explicit_change(monkeypatch):
    async def failure(*args, **kwargs):
        raise AgentLimitError("The task reached its model-call limit.")

    monkeypatch.setattr(cli, "run_task", failure)
    with pytest.raises(SystemExit) as captured:
        cli.main(["--task", "Read the board."])
    assert "higher --max-model-calls" in str(captured.value)
    assert "does not undo completed board actions" in str(captured.value)
    assert cli.parse_args(["--max-model-calls", "24"]).max_model_calls == 24
    with pytest.raises(SystemExit):
        cli.parse_args(["--max-model-calls", "101"])
