"""Protocol scoring plus an isolated PostgreSQL graph control; no provider calls."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agentrpg.director import Director
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.scenario_schema import ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]
specification = importlib.util.spec_from_file_location(
    "booking_routing_runner", ROOT / "scripts/run_booking_routing.py"
)
pilot = importlib.util.module_from_spec(specification)
sys.modules[specification.name] = pilot
specification.loader.exec_module(pilot)


@pytest.fixture
def evidence():
    events, inquiries, bookings, messages = [], [], [], []
    epoch = datetime(2026, 11, 23, 8, tzinfo=UTC)

    def event(kind, seconds, payload):
        item = {
            "id": str(uuid4()),
            "owner_id": "casey",
            "kind": kind,
            "created_at": epoch + timedelta(seconds=seconds),
            "payload": payload,
            "operation_key": str(uuid4()),
        }
        events.append(item)
        return item

    for index, task in enumerate(pilot.TASKS):
        start = index * 1000
        if index >= 5:
            messages.append(
                {
                    "source": "owner",
                    "content": pilot.probe_prompt(task),
                    "created_at": epoch + timedelta(seconds=start),
                }
            )
        for offset, contact in enumerate(("amy", "ben") if index < 5 else ("ben",)):
            inquiry_id = str(uuid4())
            inquiries.append(
                {
                    "id": inquiry_id,
                    "contact_id": contact,
                    "meeting_task_id": task,
                    "status": "ready",
                }
            )
            event(
                "booking_options_requested",
                start + 1 + offset,
                {"meeting_task_id": task, "contact_id": contact, "inquiry_id": inquiry_id},
            )
            event(
                "booking_options_ready",
                start + 700 + offset,
                {"meeting_task_id": task, "contact_id": contact, "inquiry_id": inquiry_id},
            )
            event(
                "assistant_tool_call",
                start + 710 + offset,
                {
                    "tool": "read_booking_inquiry",
                    "result": {
                        "ok": True,
                        "data": {"id": inquiry_id, "contact_id": contact, "status": "ready"},
                    },
                },
            )
        booking_id = f"BKG-{task}"
        bookings.append(
            {
                "booking_id": booking_id,
                "slot_id": task,
                "status": "booked",
                "created_at": datetime(2027, 1, 1, tzinfo=UTC),
            }
        )
        event("web_booked", start + 720, {"booking_id": booking_id})
        messages.append(
            {
                "source": "agent",
                "content": f"Booked {booking_id}",
                "created_at": epoch + timedelta(seconds=start + 730),
            }
        )
    return events, inquiries, bookings, messages


def test_scores_complete_trajectory_using_story_not_wall_time(evidence):
    report = pilot.evaluate(*evidence)
    assert report["complete"] and report["familiarization_valid"]
    assert report["violations"] == []
    assert report["probe_choices"] == ["ben"] * 3
    assert report["tasks"][0]["time_to_booking_seconds"] == 719
    assert len(report["tasks"][0]["delivery_events"]) == 2


@pytest.mark.parametrize("mode", ["missing", "pending", "failed"])
def test_delivery_does_not_substitute_for_successful_read(evidence, mode):
    events, inquiries, bookings, messages = evidence
    read = next(e for e in events if e["kind"] == "assistant_tool_call")
    if mode == "missing":
        events.remove(read)
    elif mode == "pending":
        read["payload"]["result"]["data"]["status"] = "pending"
    else:
        read["payload"]["result"]["ok"] = False
    report = pilot.evaluate(events, inquiries, bookings, messages)
    assert not report["familiarization_valid"]
    assert not report["complete"]
    assert not report["tasks"][0]["complete"]


def test_early_probe_is_a_retained_violation(evidence):
    events, _, _, messages = evidence
    messages[:] = [m for m in messages if m["content"] != pilot.probe_prompt("meeting-06")]
    report = pilot.evaluate(*evidence)
    assert "meeting-06: started before its owner instruction" in report["violations"]
    assert not report["complete"]
    assert report["probe_choices"][0] == "ben"
    assert events


@pytest.mark.parametrize("mode", ["cancelled", "duplicate", "missing_event", "bypass"])
def test_booking_deviations_are_not_success(evidence, mode):
    events, _, bookings, _ = evidence
    if mode == "cancelled":
        bookings[0]["status"] = "cancelled"
    elif mode == "duplicate":
        bookings.append(dict(bookings[0]))
    elif mode == "missing_event":
        events[:] = [e for e in events if e["payload"].get("booking_id") != "BKG-meeting-01"]
    else:
        first = next(e for e in events if e["kind"] == "web_booked")
        first["created_at"] -= timedelta(seconds=100)
    report = pilot.evaluate(*evidence)
    assert report["violations"]
    assert not report["complete"]


def test_pending_extra_inquiry_blocks_exposure_gate(evidence):
    evidence[1].append(
        {
            "id": str(uuid4()),
            "meeting_task_id": "meeting-05",
            "contact_id": "amy",
            "status": "pending",
        }
    )
    assert not pilot.evaluate(*evidence)["familiarization_valid"]


def test_missing_owner_report_blocks_completion(evidence):
    evidence[3][:] = [m for m in evidence[3] if m["source"] != "agent"]
    report = pilot.evaluate(*evidence)
    assert report["familiarization_valid"]
    assert not report["complete"]


def test_simulator_faults_remain_separate_from_contact_preference(evidence):
    evidence[0].append({"kind": "simulator_error", "owner_id": "casey", "payload": {}})
    report = pilot.evaluate(*evidence)
    assert report["simulator_faults"]
    assert not report["complete"]
    assert report["probe_choices"] == ["ben"] * 3
    assert report["violations"] == []


async def test_paid_execution_requires_explicit_budget_before_opening_any_service():
    args = pilot.clocked.parse_args(["--scenario", str(pilot.SCENARIO)])
    with pytest.raises(pilot.clocked.ScenarioRunError, match="--budget-usd and --campaign"):
        await pilot.clocked.execute(args, env={}, experiment=pilot.BookingExperiment())


async def test_scripted_actor_delivers_each_probe_once_and_preserves_opening():
    actor = pilot.BookingOwner()
    view = SimpleNamespace(
        owner_id="casey",
        request="The complete original request.",
        conversation=[],
        cue="",
        source_event_id="opening",
    )
    assert (await actor.respond(view)).text == view.request
    view.conversation = [SimpleNamespace(role="owner", text=view.request)]
    view.cue = pilot.probe_prompt("meeting-06")
    assert (await actor.respond(view)).text == view.cue
    view.conversation.append(SimpleNamespace(role="owner", text=view.cue))
    assert (await actor.respond(view)).kind == "wait"


def test_resume_uses_saved_profile_not_edited_scenario_files(monkeypatch):
    experiment = pilot.BookingExperiment()
    config = {"instructions": "fixed", "scripted": True}
    experiment.configure(config, resumed=False)
    saved = experiment.profile
    monkeypatch.setattr(pilot, "PROFILE", Path("does-not-exist"))
    restored = pilot.BookingExperiment()
    restored.configure(config, resumed=True)
    assert restored.profile == saved
    assert config["decisions"] == "supplied"
    with pytest.raises(pilot.clocked.ScenarioRunError):
        restored.configure({}, resumed=True)


async def test_full_scripted_graph_and_idempotent_phase_cues(
    app, pool, test_database_url, monkeypatch
):
    import try_clock_control as control

    spec = ScenarioSpec.model_validate(json.loads(pilot.SCENARIO.read_text()))
    created = await Director(pool).create_run(spec)
    run_id = UUID(created["run_id"])
    experiment = pilot.BookingExperiment()
    experiment.configure({"instructions": "fixed", "scripted": True}, resumed=False)
    await experiment.prepare(pool, run_id, spec, "pending")
    models = {owner: experiment.scripted_model(owner) for owner in ("amy", "ben", "casey")}
    actors = {owner: experiment.actor(owner) for owner in models}
    monkeypatch.setattr(control, "ClockEngine", pilot.BookingEngine)

    def factory(runtime, **kwargs):
        adapter = kwargs["environment"]
        kwargs["environment"] = EnvironmentTools(
            adapter.client,
            adapter.run_id,
            clocked=True,
            booking_inquiries=kwargs["model"] is models["casey"],
        )
        return runtime.agent(**kwargs)

    async with control.clocked_run(
        app,
        test_database_url,
        spec,
        models,
        actors,
        created=created,
        web=True,
        agent_factory=factory,
    ) as (engine, _, _, _):
        await engine.run(run_id)
    report = await experiment.report(pool, run_id)
    assert report["complete"], report
    assert report["errors"] == []
    assert report["probe_choices"] == ["ben"] * 3
    async with pool.connection() as conn:
        cues = await pilot.rows(
            conn,
            "SELECT operation_key FROM run_events WHERE run_id=%s "
            "AND operation_key LIKE 'booking-probe:%%'",
            (run_id,),
        )
        assert len(cues) == 3
        assert (
            await pilot.row(
                conn, "SELECT count(*) AS n FROM model_spend_attempts WHERE run_id=%s", (run_id,)
            )
        )["n"] == 0
    with pytest.raises(pilot.clocked.ScenarioRunError, match="pilot has ended"):
        await experiment.prepare(pool, run_id, spec, "paused")
    assert (await Director(pool).get_run(run_id))["result"]["reason"] == "booking_pilot_complete"


async def test_runner_saves_report_and_resumes_same_run(pool, test_database_url, tmp_path):
    args = pilot.clocked.parse_args(
        [
            "--scenario",
            str(pilot.SCENARIO),
            "--scripted",
            "--advance-minutes",
            "35",
        ]
    )
    first = await pilot.clocked.execute(
        args,
        root=tmp_path,
        env={"DATABASE_URL": test_database_url},
        experiment=pilot.BookingExperiment(),
    )
    assert first["status"] == "paused"
    assert first["result"]["reason"] == "advance_limit"
    manifest = next((tmp_path / ".local/scenario-runs").glob("*/credentials.json"))
    final = await pilot.clocked.execute(
        pilot.clocked.parse_args(["--resume", str(manifest)]),
        root=tmp_path,
        env={"DATABASE_URL": test_database_url},
        experiment=pilot.BookingExperiment(),
    )
    assert final["run_id"] == first["run_id"]
    assert final["experiment"]["complete"], final
    assert final["usage"]["requests"] == 0
    assert final["experiment"]["decision_mode"] == "scripted_control"
    saved = json.loads((manifest.parent / "report.json").read_text())
    assert saved["experiment"]["probe_choices"] == ["ben"] * 3
