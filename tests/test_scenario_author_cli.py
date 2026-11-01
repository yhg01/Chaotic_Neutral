"""Author command output, call limits, and safe failure behavior."""

import asyncio
import importlib.util
import io
import json
import stat
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agentrpg.actor import ModelGenerationError
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/author_scenario.py"
SPEC = importlib.util.spec_from_file_location("author_scenario", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0
        self.closed = False
        self.seen = []

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        self.seen.append(messages)
        return AIMessage(content=json.dumps(next(self.values)))

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "options",
    [
        ["--agents", "1"],
        ["--agents", "16"],
        ["--max-repairs", "3"],
        ["--max-repairs", "-1"],
        ["--timeout-seconds", "0"],
        ["--timeout-seconds", "121"],
    ],
)
def test_author_option_limits(options):
    with pytest.raises(SystemExit):
        cli.parse_args(["--output", "scenario.json", *options])


def test_author_output_must_be_json():
    with pytest.raises(SystemExit):
        cli.parse_args(["--output", "scenario.py"])


@pytest.mark.asyncio
async def test_author_saves_only_compiled_and_accepted_draft(tmp_path, monkeypatch, capsys):
    scenario = vehicle_scenario(2)
    models = [
        Responses([scenario.model_dump(mode="json")]),
        Responses([{"accepted": True, "issues": []}]),
    ]
    supplied = []

    def factory(key, model, **kwargs):
        supplied.append((model, kwargs))
        assert key == "provider-secret"
        return models[len(supplied) - 1]

    monkeypatch.setattr(cli, "create_model", factory)
    args = cli.parse_args(
        [
            "--brief",
            "A transport conflict.",
            "--output",
            "scenarios/first.json",
            "--reviewer-model",
            "reviewer",
        ]
    )
    result = await cli.execute(args, root=tmp_path, env={"OPENROUTER_API_KEY": "provider-secret"})
    output = tmp_path / "scenarios/first.json"
    assert ScenarioSpec.model_validate_json(output.read_text()) == scenario
    assert result["scenario_hash"] == scenario.content_hash()
    assert supplied == [
        ("openai/gpt-4.1-mini", {"max_tokens": 4096}),
        ("reviewer", {"max_tokens": 4096}),
    ]
    assert all(model.closed and model.calls == 1 for model in models)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "provider-secret" not in capsys.readouterr().out
    report_path = output.with_suffix(".author-report.json")
    saved = json.loads(report_path.read_text())
    assert saved["outcome"] == "saved" and saved["scenario_saved"]
    assert saved["limits"]["max_model_invocations"] == 4
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    invocation_path = Path(saved["invocation_report"])
    assert await asyncio.to_thread(invocation_path.exists)


@pytest.mark.asyncio
async def test_author_reads_brief_from_stdin(tmp_path, monkeypatch):
    models = iter(
        [
            Responses([vehicle_scenario(2).model_dump(mode="json")]),
            Responses([{"accepted": True, "issues": []}]),
        ]
    )
    monkeypatch.setattr(cli, "create_model", lambda *args, **kwargs: next(models))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("A conflict from standard input."))
    await cli.execute(
        cli.parse_args(["--output", "scene.json"]),
        root=tmp_path,
        env={"OPENROUTER_API_KEY": "secret"},
    )
    assert (tmp_path / "scene.json").exists()


@pytest.mark.asyncio
async def test_rejected_draft_does_not_create_output(tmp_path, monkeypatch):
    author = Responses([vehicle_scenario(2).model_dump(mode="json")])
    reviewer = Responses([{"accepted": False, "issues": ["Needs a valid alternative."]}])
    models = iter([author, reviewer])
    monkeypatch.setattr(cli, "create_model", lambda *args, **kwargs: next(models))
    args = cli.parse_args(["--brief", "Conflict", "--output", "scene.json", "--max-repairs", "0"])
    with pytest.raises(ModelGenerationError):
        await cli.execute(args, root=tmp_path, env={"OPENROUTER_API_KEY": "secret"})
    assert not (tmp_path / "scene.json").exists()
    assert author.closed and reviewer.closed
    report = json.loads((tmp_path / "scene.author-report.json").read_text())
    assert report["outcome"] == "failed" and not report["scenario_saved"]
    assert report["attempts"] and not report["attempts"][0]["accepted"]
    assert report["limits"]["max_model_invocations"] == 2


@pytest.mark.asyncio
async def test_existing_output_fails_before_model_creation(tmp_path, monkeypatch):
    output = tmp_path / "scene.json"
    output.write_text("Preserve my work")
    args = cli.parse_args(["--brief", "Conflict", "--output", "scene.json"])
    monkeypatch.setattr(
        cli, "create_model", lambda *args, **kwargs: pytest.fail("Unexpected model client")
    )
    with pytest.raises(cli.AuthorCommandError, match="exists"):
        await cli.execute(args, root=tmp_path, env={"OPENROUTER_API_KEY": "secret"})
    assert output.read_text() == "Preserve my work"


def test_no_overwrite_publication_is_atomic(tmp_path):
    output = tmp_path / "scene.json"
    output.write_text("existing")
    with pytest.raises(cli.AuthorCommandError, match="exists"):
        cli.save_scenario(output, "new", overwrite=False)
    assert output.read_text() == "existing"
    assert not list(tmp_path.glob(".scenario-*"))
    cli.save_scenario(output, "new", overwrite=True)
    assert output.read_text() == "new\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("brief", [" ", "x" * 16001])
async def test_brief_limits_fail_before_model_calls(tmp_path, brief):
    args = cli.parse_args(["--brief", brief, "--output", "scene.json"])
    with pytest.raises(cli.AuthorCommandError, match="brief"):
        await cli.execute(args, root=tmp_path, env={"OPENROUTER_API_KEY": "secret"})


def test_main_never_prints_provider_exception(monkeypatch):
    async def failed(*args, **kwargs):
        raise RuntimeError("private-provider-credential")

    monkeypatch.setattr(cli, "execute", failed)
    with pytest.raises(SystemExit) as error:
        cli.main(["--brief", "Conflict", "--output", "scene.json"])
    assert "private-provider-credential" not in str(error.value)
    assert "No unvalidated draft was saved" in str(error.value)


@pytest.mark.asyncio
async def test_failure_reports_preserve_every_invocation_without_raw_errors(tmp_path, monkeypatch):
    def failing_factory(*args, **kwargs):
        raise RuntimeError("provider-secret and private response")

    monkeypatch.setattr(cli, "create_model", failing_factory)
    args = cli.parse_args(["--brief", "Conflict", "--output", "scene.json"])
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cli.execute(args, root=tmp_path, env={"OPENROUTER_API_KEY": "provider-secret"})
    records = list(tmp_path.glob("scene.author-report.*.json"))
    assert len(records) == 2
    assert not (tmp_path / "scene.json").exists()
    for path in records:
        text = path.read_text()
        assert "provider-secret" not in text and "private response" not in text
        report = json.loads(text)
        assert report["outcome"] == "failed" and report["failure_kind"] == "RuntimeError"
        assert report["attempts"] == []
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "options", "env_models", "expected_model", "cap"),
    [
        (2, [], {"OPENROUTER_MODEL": "assistant"}, "assistant", 4096),
        (
            15,
            [],
            {"OPENROUTER_MODEL": "assistant", "OPENROUTER_DIRECTOR_MODEL": "director"},
            "director",
            14080,
        ),
        (2, ["--model", "explicit"], {"OPENROUTER_DIRECTOR_MODEL": "director"}, "explicit", 4096),
    ],
)
async def test_author_uses_role_model_and_population_output_budget(
    tmp_path, monkeypatch, count, options, env_models, expected_model, cap
):
    models = iter(
        [
            Responses([vehicle_scenario(count).model_dump(mode="json")]),
            Responses([{"accepted": True, "issues": []}]),
        ]
    )
    calls = []

    def factory(key, model, **kwargs):
        calls.append((model, kwargs))
        return next(models)

    monkeypatch.setattr(cli, "create_model", factory)
    args = cli.parse_args(
        [
            "--agents",
            str(count),
            "--output",
            "scenario.json",
            "--brief",
            "Transport conflict.",
            *options,
        ]
    )
    report = await cli.execute(
        args, root=tmp_path, env={"OPENROUTER_API_KEY": "secret", **env_models}
    )
    assert calls[0][0] == expected_model
    assert calls[0][1]["max_tokens"] == cap
    assert calls[0][1].get("max_output_limit", 4096) == (16384 if cap > 4096 else 4096)
    assert calls[1] == (expected_model, {"max_tokens": 4096})
    assert report["limits"]["max_author_output_tokens"] == cap
    assert report["limits"]["max_critique_output_tokens"] == 4096
