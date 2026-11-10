"""Public runner, real PostgreSQL/graph/tools, and supplied model responses."""

import json
import runpy
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import pytest
from test_actor import Responses
from test_actor_cue_review import coverage, current_view, stimulus
from test_budget import price
from test_clock_runtime import tool
from test_deep_agent import ScriptedModel

from agentrpg.actor import ActorContractError, ModelActor
from agentrpg.actor_memory_schema import MemoryProfile
from agentrpg.budget import BudgetExceeded, CampaignLedger
from agentrpg.director import rows

ROOT = Path(__file__).parents[1]
FILE = "/plans/afternoon.md"
INITIAL = "14:00-15:00 UTC, Birch Room"
CHANGED = "16:00-17:00 UTC, Willow Room"
DELEGATED = " Also, revise your own private intention in your cognition agenda."
CLAIM = "I have also noted my own private Birch intention in my cognition agenda."
APPROVAL = {"accepted": True, "issues": [], "private_planning_respected": True}


def module():
    return runpy.run_path(str(ROOT / "scripts/run_clock_actors.py"))


def agenda(start, room):
    return {
        "day": "2026-11-10",
        "slots": [{"start_minute": start, "end_minute": start + 60, "activity": f"Read in {room}"}],
    }


def reply(plan, day_plan):
    return {
        "kind": "reply",
        "text": f"Please save {plan} in my private file {FILE} and read it back.",
        "cognition": {"agendas": [day_plan]},
    }


@pytest.mark.parametrize("value", ["0", "25.01", "NaN", "0.0000000000001"])
def test_ceiling_cannot_exceed_existing_authorization(value):
    with pytest.raises(SystemExit, match="USD"):
        module()["runner_options"](Namespace(budget_usd=value), None)


def test_resume_freezes_existing_campaign_budget_and_memory():
    runner = module()
    saved = {
        "campaign_id": str(runner["CAMPAIGN"]),
        "ceiling_usd": "25",
        "actor_memory_profile": MemoryProfile().model_dump(mode="json"),
    }
    assert runner["runner_options"](Namespace(), saved) == (Decimal(25), MemoryProfile())
    with pytest.raises(SystemExit, match="saved Actor memory mode"):
        runner["runner_options"](Namespace(actor_memory=False), saved)
    with pytest.raises(SystemExit, match="saved campaign and budget"):
        runner["runner_options"](Namespace(budget_usd="20"), saved)
    saved["campaign_id"] = "other"
    with pytest.raises(SystemExit, match="saved campaign"):
        runner["runner_options"](Namespace(), saved)


async def test_budget_guard_rechecks_under_reservation_lock(pool):
    campaign = module()["CAMPAIGN"]
    ledger = CampaignLedger(pool, expected_limit_usd=25)
    await ledger.ensure_campaign(campaign, 25)
    historical = await ledger.reserve(campaign, price(), 10, 10, role="actor")
    await ledger.finish(historical, reason="supplied historical uncertainty")
    before = await ledger.summary(campaign)
    async with pool.connection() as conn:
        await conn.execute("UPDATE model_campaigns SET limit_usd=26 WHERE id=%s", (campaign,))
    with pytest.raises(BudgetExceeded, match="ceiling changed"):
        await ledger.reserve(campaign, price(), 10, 10, role="assistant")
    after = await ledger.summary(campaign)
    assert after["attempts"] == before["attempts"] == 1
    assert after["uncertain_usd"] == before["uncertain_usd"] > 0


@pytest.mark.parametrize(
    "bad", ["missing", "old_slot", "delegated", "review_miss", "review_reject"]
)
async def test_required_agenda_and_review_cannot_be_replaced_by_speech(bad):
    view = current_view(required=True).model_copy(
        update={
            "actor_memory": {
                "protocol": MemoryProfile().protocol,
                "allowed_evidence": [],
                "memories": [],
                "planning_days": ["2026-11-10"],
                "required_agendas": [{"cue_id": "change-2", "agenda": agenda(960, "Willow")}],
            }
        }
    )
    candidate = reply(CHANGED, agenda(960, "Willow"))
    verdict = {**coverage(), "private_planning_respected": True}
    if bad == "missing":
        del candidate["cognition"]
    elif bad == "old_slot":
        candidate["cognition"]["agendas"] = [agenda(840, "Birch")]
    elif bad == "delegated":
        candidate["text"] += DELEGATED
    elif bad == "review_miss":
        del verdict["private_planning_respected"]
    else:
        verdict["private_planning_respected"] = False
    reviewer = Responses([stimulus(), verdict])
    actor = ModelActor(
        Responses([candidate]),
        reviewer=reviewer,
        require_current_cue_review=True,
        max_repairs=0,
    )
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view)
    assert not actor.last_attempts[-1]["accepted"]
    assert "actor_memory" not in json.loads(reviewer.calls[0][1].content)["view"]
    if bad in {"missing", "old_slot", "delegated"}:
        assert len(reviewer.calls) == 1
    else:
        request = json.loads(reviewer.calls[1][1].content)
        assert request["candidate"]["cognition"] == {
            "agendas": [agenda(960, "Willow")],
            "thoughts": [],
        }
        assert request["view"]["actor_memory"]["required_agendas"]


class LocalResponses(Responses):
    async def aclose(self):
        pass


class LocalAgent(ScriptedModel):
    async def aclose(self):
        pass


async def test_public_runner_cue_agenda_file_closeout_and_resume(
    pool, settings, tmp_path, monkeypatch
):
    runner = module()
    main = runner["main"]
    local = main.__globals__
    monkeypatch.setitem(local, "ROOT", tmp_path)
    monkeypatch.setitem(local, "Settings", lambda: settings)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/fixture.py").write_text("pass\n")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=fixture-key\n")
    (tmp_path / ".env.personal-agent.local").write_text(
        "PERSONAL_AGENT_API_ENABLED=true\nPERSONAL_AGENT_PROVIDER=openrouter\n"
        "PERSONAL_AGENT_BASE_URL=https://openrouter.ai/api/v1\n"
        "PERSONAL_AGENT_MODEL=deepseek/deepseek-v3.2\nPERSONAL_AGENT_API_KEY=fixture-key\n"
    )
    protocol = json.loads(
        (ROOT / "docs/experiments/actor-agent-memory-probe-protocol.json").read_text()
    )["definition"]
    scenario, cues, baseline = [tmp_path / f"{name}.json" for name in ("scene", "cues", "baseline")]
    scenario.write_text(json.dumps(protocol["scenario"]))
    cue_source = ROOT / "docs/experiments/actor-agent-memory-repair-cues.json"
    cues.write_bytes(cue_source.read_bytes())
    baseline.write_text(
        json.dumps(
            {
                "configuration": {
                    "actor_model": "openai/gpt-4.1",
                    "actor_prompt": "Supplied owner generation.",
                    "actor_review_prompt": "Supplied independent review.",
                    "instructions": "Handle actual workspace requests.",
                }
            }
        )
    )
    args = Namespace(
        pairs=2,
        days=1,
        seed=20261109,
        events_per_day=None,
        event_profile="mixed",
        scenario=scenario,
        private_cues=cues,
        prepare=tmp_path / "prepared.json",
        preflight=None,
        resume=None,
        readiness=None,
        no_chaos=False,
        baseline_manifest=baseline,
        max_actions=300,
        budget_usd="25",
        actor_memory=True,
    )
    catalogs = []

    async def catalog(models):
        catalogs.append(models)
        return {name: price(model=name) for name in models}

    monkeypatch.setitem(local, "fetch_model_prices", catalog)
    await main(args)
    prepared = json.loads(args.prepare.read_text())
    assert prepared["ceiling_usd"] == "25" and prepared["actor_memory_profile"]
    assert prepared["events"][0]["payload"]["private_agenda"] == agenda(840, "Birch")
    assert catalogs == []
    mixed = json.loads(cue_source.read_text())
    mixed[0]["payload"]["text"] += DELEGATED
    cues.write_text(json.dumps(mixed))
    with pytest.raises(SystemExit, match="Invalid --private-cues"):
        await main(args)
    assert catalogs == []
    cues.write_bytes(cue_source.read_bytes())
    args.prepare = None
    args.preflight = tmp_path / "preflight.json"
    ledger = CampaignLedger(pool)
    await ledger.ensure_campaign(runner["CAMPAIGN"], 25)
    args.budget_usd = "20"
    with pytest.raises(SystemExit, match="does not match --budget-usd"):
        await main(args)
    assert catalogs == []
    args.budget_usd = "25"
    await main(args)
    preflight = json.loads(args.preflight.read_text())
    assert preflight["campaign"]["limit_usd"] == "25" and preflight["provider_calls"] == 0
    assert preflight["candidate"]["events"] == prepared["events"]
    args.preflight = None

    initial = reply(INITIAL, agenda(840, "Birch"))
    changed = reply(CHANGED, agenda(960, "Willow"))
    leaky = {**changed, "text": changed["text"] + DELEGATED}
    wait = {"kind": "wait", "text": ""}
    silent_review = {**APPROVAL, "owner_dialogue_emitted": False}
    alice_actor = LocalResponses(
        [
            {"kind": "reply", "text": initial["text"]},
            initial,
            wait,
            leaky,
            changed,
            wait,
        ]
    )
    alice_review = LocalResponses(
        [
            stimulus(cue_id="initial-plan"),
            {**APPROVAL, **coverage(cue_id="initial-plan"), "owner_dialogue_emitted": True},
            silent_review,
            stimulus(cue_id="changed-plan"),
            {**APPROVAL, **coverage(cue_id="changed-plan"), "owner_dialogue_emitted": True},
            silent_review,
        ]
    )
    alice_agent = LocalAgent(
        responses=[
            tool("write_file", {"file_path": FILE, "content": INITIAL}, "write"),
            tool("read_file", {"file_path": FILE}, "initial-read"),
            tool("send_owner_message", {"content": CLAIM}, "bad-message"),
            tool("finish_activation", {"status": "finished", "text": CLAIM}, "bad-finish"),
            tool(
                "finish_activation",
                {"status": "finished", "text": f"Verified {INITIAL}."},
                "finish",
            ),
            tool(
                "edit_file",
                {"file_path": FILE, "old_string": INITIAL, "new_string": CHANGED},
                "edit",
            ),
            tool("read_file", {"file_path": FILE}, "changed-read"),
            tool(
                "finish_activation", {"status": "finished", "text": f"Verified {CHANGED}."}, "done"
            ),
        ]
    )
    roles = [
        (alice_agent, alice_actor, alice_review),
        (LocalAgent(responses=[]), LocalResponses([wait]), LocalResponses([silent_review])),
    ]
    factories = []

    def clients(personal, actor_reviewer, binding):
        assert personal.model == "deepseek/deepseek-v3.2"
        assert actor_reviewer.model == "openai/gpt-4.1"
        assert binding.campaign_id == runner["CAMPAIGN"]
        assert binding.ledger.expected_limit_usd == Decimal(25)
        result = roles[len(factories) % 2]
        factories.append(result)
        return result

    monkeypatch.setitem(local, "create_role_models", clients)
    await main(args)
    (directory,) = (tmp_path / ".local/clock-model-runs").iterdir()
    credentials = directory / "credentials.json"
    frozen = credentials.read_bytes()
    assert json.loads(frozen)["actor_memory_profile"] == MemoryProfile().model_dump(mode="json")
    (invocation,) = [p for p in directory.iterdir() if p.is_dir()]
    original_trajectory = (invocation / "trajectory.json").read_bytes()
    trajectory = json.loads(original_trajectory)
    (graph,) = [
        s["values"]
        for s in trajectory["latest_graph_states"].values()
        if FILE in (s["values"].get("files") or {})
    ]
    file_record = json.dumps(graph["files"][FILE])
    assert CHANGED in file_record and INITIAL not in file_record
    assert (
        sum(
            "unsupported_private_planning_claim" in json.dumps(m["data"]["content"])
            for m in graph["messages"]
            if m["type"] == "tool"
        )
        == 2
    )
    assert all(
        "required_agendas" not in json.dumps(m["data"]["content"])
        for m in graph["messages"]
        if m["type"] == "human"
    )
    async with pool.connection() as conn:
        plans = await rows(conn, "SELECT slots FROM actor_agendas ORDER BY created_seconds")
        messages = await rows(conn, "SELECT source,content FROM owner_messages ORDER BY created_at")
        assert (
            await rows(conn, "SELECT * FROM clock_runtime_activations WHERE status<>'done'") == []
        )
    assert [p["slots"] for p in plans] == [
        agenda(840, "Birch")["slots"],
        agenda(960, "Willow")["slots"],
    ]
    assert [m["content"] for m in messages if m["source"] == "agent"] == [
        f"Verified {INITIAL}.",
        f"Verified {CHANGED}.",
    ]
    assert [m["content"] for m in messages if m["source"] == "owner"] == [
        initial["text"],
        changed["text"],
    ]
    for generation_index, verified in [(2, INITIAL), (5, CHANGED)]:
        actor_input = json.loads(alice_actor.calls[generation_index][1].content)["view"]
        assert any(
            m["role"] == "assistant" and f"Verified {verified}." in m["text"]
            for m in actor_input["conversation"]
        )
    assert not (await ledger.summary(runner["CAMPAIGN"]))["attempts"]
    args.resume, args.private_cues, args.scenario = credentials, None, None
    args.actor_memory, args.budget_usd = None, None
    await main(args)
    assert credentials.read_bytes() == frozen
    assert (invocation / "trajectory.json").read_bytes() == original_trajectory
    assert len(alice_actor.calls) == 6
