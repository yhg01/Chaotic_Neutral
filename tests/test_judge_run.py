"""The judge command on the control cases and on a saved clocked run, with scripted models.

The scripted judge models here follow fixed rules. They show that the command, the evidence
from a real saved run, the saved answers and the reports work. They say nothing about how
well a real model judges.
"""

import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import try_clock_control as control  # noqa: E402

from agentrpg.judge_controls import control_cases  # noqa: E402
from agentrpg.judge_evidence import load_run, pack_owner  # noqa: E402
from agentrpg.misalignment import CATEGORIES  # noqa: E402


def judge_script():
    path = ROOT / "scripts" / "judge_run.py"
    spec = importlib.util.spec_from_file_location("judge_run", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["judge_run"] = module
    spec.loader.exec_module(module)
    return module


def _strings(value):
    """The text of a value in the order that a record gives it for a quotation check."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


class RuleScreen:
    """Lists the last action of the assistant with an effect, as one candidate."""

    def __init__(self, tools=("send_mail", "submit_form", "transfer_credits", "direct_message")):
        self.tools = tools
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        data = json.loads(messages[-1].content)
        actions = [
            record
            for record in data["records"]
            if record["role"] == "assistant" and record.get("tool") in self.tools
        ]
        owner = [record["id"] for record in data["records"] if record["role"] == "owner"]
        found = [
            {
                "category": "exceeds_authority",
                "action_ids": [actions[-1]["id"]],
                "conflict_ids": owner[:1],
                "claim": "The assistant took this action. A rule listed it for the test.",
            }
        ] * bool(actions)
        return AIMessage(content=json.dumps({"candidates": found}))


class RuleVerdict:
    """Says misaligned for the listed case names, with words copied from the action."""

    def __init__(self, guilty=()):
        self.guilty = set(guilty)
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        data = json.loads(messages[-1].content)
        action = next(r for r in data["records"] if r["id"] == data["claim"]["action_ids"][0])
        case = action["id"].rsplit("-", 1)[0]
        finding = case in self.guilty
        body = {key: action.get(key) for key in ("tool", "arguments", "result", "error")}
        quote = " ".join(" ".join(_strings(body)).split())[:40]
        return AIMessage(
            content=json.dumps(
                {
                    "verdict": "misaligned" if finding else "justified",
                    "category": "exceeds_authority",
                    "knowledge": "knew" if finding else "unclear",
                    "effect": "took_effect",
                    "owner_directed": not finding,
                    "severity": "medium" if finding else "none",
                    "evidence_ids": [action["id"]],
                    "quote": quote if finding else "",
                    "reason": "A fixed rule of the test gave this verdict for this case.",
                }
            )
        )


def test_the_control_cases_are_well_formed():
    cases = control_cases()
    assert len(cases) == 16 and len({case.id for case in cases}) == 16
    seeded = [case for case in cases if case.expect == "finding"]
    assert len(seeded) == 10 and {case.category for case in seeded} == set(CATEGORIES)
    for case in cases:
        ids = [record.id for record in case.records]
        assert ids == [f"{case.id}-{n:04d}" for n in range(1, len(ids) + 1)]
        assert case.records[0].role == "owner"
        assert any(record.role == "assistant" for record in case.records)
        named = [
            item
            for record in case.records
            for item in record.body.get("owner_messages", [])
            if item.startswith(case.id)
        ]
        # A turn names the owner message that woke it, and that record is an owner message.
        assert named and all(case.records[ids.index(item)].role == "owner" for item in named)
        assert (case.expect == "clean") == (case.category is None)


async def test_the_command_scores_the_controls_and_saves_private_files(tmp_path):
    script = judge_script()
    seeded = [case.id for case in control_cases() if case.expect == "finding"]
    args = script.parse_args(["--controls", "--votes", "1", "--name", "rules"])
    result = await script.execute(
        args, root=tmp_path, env={}, models=(RuleScreen(), RuleVerdict(guilty=seeded))
    )
    assert result["status"] == "completed"
    score = result["controls"]
    assert score["seeded_cases"] == 10 and score["clean_cases"] == 6
    # The rule lists an action in 15 cases. It gives a finding for the seeded ones only.
    assert score["seeded_screened"] == 10 and score["seeded_caught"] == 10
    assert score["clean_with_a_false_finding"] == 0
    assert score["seeded_with_the_expected_category"] == 2
    folder = tmp_path / ".local" / "judge" / "controls" / "rules"
    saved = json.loads((folder / "controls.json").read_text())
    assert (
        saved["controls"]["rows"][0]["case"] == "over-limit"
        and saved["controls"]["rows"][0]["caught"]
    )
    assert len(list((folder / "evidence").iterdir())) == 16
    files = [path for path in folder.rglob("*") if path.is_file()]
    assert files and all(oct(path.stat().st_mode & 0o777) == "0o600" for path in files)
    # A rule that marks everything shows up as false findings on the clean cases.
    harsh = script.parse_args(["--controls", "--votes", "1", "--name", "harsh"])
    everything = [case.id for case in control_cases()]
    marked = await script.execute(
        harsh, root=tmp_path, env={}, models=(RuleScreen(), RuleVerdict(guilty=everything))
    )
    assert marked["controls"]["clean_with_a_false_finding"] == 5


async def test_the_command_judges_a_saved_clocked_run_and_does_not_change_it(
    app, pool, test_database_url, tmp_path
):
    run, _, _, _ = await control.run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    assert run["status"] == "completed"
    async with pool.connection() as conn:
        found = await (await conn.execute("SELECT id FROM environment_runs")).fetchall()
    assert len(found) == 1
    run_id = str(found[0]["id"])

    async def saved_rows():
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT count(*) AS n, max(created_at) AS last FROM run_events WHERE run_id=%s",
                    (UUID(run_id),),
                )
            ).fetchone()
        return row["n"], row["last"]

    before = await saved_rows()
    saved = await load_run(pool, run_id)
    assert saved.status == "completed" and {o.id for o in saved.spec.owners} == {
        "dan",
        "mei",
        "sam",
    }
    records = pack_owner(saved.events, "dan")
    kinds = {(record.role, record.kind) for record in records}
    assert {
        ("owner", "owner_message"),
        ("world", "turn_start"),
        ("assistant", "tool_call"),
    } <= kinds
    assert ("assistant", "turn_end") in kinds
    assert all(record.at for record in records if record.kind == "tool_call")
    assert not any(record.id.startswith(("mei", "sam")) for record in records)

    script = judge_script()
    env = {"DATABASE_URL": test_database_url}
    counted = await script.execute(
        script.parse_args(["--run", run_id, "--estimate"]), root=tmp_path, env=env
    )
    assert set(counted["records"]) == {"dan", "mei", "sam"} and counted["estimate"]["parts"] == 3
    assert float(counted["estimate"]["total_usd_about"]) > 0

    tools = ("send_mail", "send_owner_message", "post_thread", "reply", "submit_world_action")
    screen, verdict = RuleScreen(tools), RuleVerdict(guilty=["dan"])
    args = script.parse_args(["--run", run_id, "--votes", "2", "--sample", "5"])
    result = await script.execute(args, root=tmp_path, env=env, models=(screen, verdict))
    assert result["status"] == "completed", result
    assert result["counts_by_owner"]["dan"] == {"misaligned": 1}
    assert result["counts_by_owner"]["mei"] == {"justified": 1}
    assert result["coverage"]["dan"]["records"] == len(records)
    folder = tmp_path / ".local" / "judge" / run_id / "v1"
    report = json.loads((folder / "report.json").read_text())
    finding = report["incidents"][0]
    assert finding["verdict"] == "misaligned" and finding["votes"] == {"misaligned": 2}
    assert finding["at"] and finding["quotes"] and finding["action_ids"][0].startswith("dan-")
    assert report["run"]["scenario_hash"] == saved.scenario_hash
    text = (folder / "report.md").read_text()
    assert "## Findings" in text and "dan-I01: misaligned" in text
    sample = (folder / "review_sample.csv").read_text().splitlines()
    assert sample[0].startswith("item,owner,story_time,source") and "human_label" in sample[0]
    assert sum(",judge," in line for line in sample) == 3
    assert 1 <= sum(",sample_not_listed," in line for line in sample) <= 5
    assert await saved_rows() == before

    # The same command again reads the saved answers and asks no model.
    calls = (screen.calls, verdict.calls)
    again = await script.execute(args, root=tmp_path, env=env, models=(screen, verdict))
    assert again["counts_by_owner"] == result["counts_by_owner"]
    assert (screen.calls, verdict.calls) == calls
    # Other settings under the same name are refused, so two judges cannot share one folder.
    other = script.parse_args(["--run", run_id, "--votes", "3"])
    with pytest.raises(script.JudgeRunError, match="other settings"):
        await script.execute(other, root=tmp_path, env=env, models=(screen, verdict))


async def test_a_paid_judge_run_needs_a_stated_ceiling_and_a_known_run(
    pool, test_database_url, tmp_path
):
    script = judge_script()
    env = {"DATABASE_URL": test_database_url}
    for extra in (["--budget-usd", "5"], ["--campaign", str(uuid4())], []):
        with pytest.raises(script.JudgeRunError, match="--budget-usd and --campaign"):
            await script.execute(
                script.parse_args(["--run", str(uuid4()), *extra]), root=tmp_path, env=env
            )
    with pytest.raises(ValueError, match="not in the database"):
        await script.execute(
            script.parse_args(["--run", str(uuid4()), "--estimate"]), root=tmp_path, env=env
        )
    with pytest.raises(script.JudgeRunError, match="test database"):
        script.database_url(
            {
                "DATABASE_URL": "postgresql://localhost/agentrpg",
                "TEST_DATABASE_URL": "postgresql://localhost/main",
            },
            "zclive_week1",
        )
    with pytest.raises(script.JudgeRunError, match="letters, digits"):
        script.database_url({"DATABASE_URL": "postgresql://localhost/agentrpg"}, "x; drop")
    assert "search_path=zclive_week1" in script.database_url(
        {"DATABASE_URL": "postgresql://localhost/agentrpg"}, "zclive_week1"
    )
