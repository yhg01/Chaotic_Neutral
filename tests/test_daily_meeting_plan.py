"""Simplification preserves the original plan and requires real social decisions."""

import json
import runpy
from argparse import Namespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentrpg.community_experiment import CommunityPlan, DailyMeetingPlan
from agentrpg.simulation_clock import digest


def test_original_four_cue_candidate_keeps_its_frozen_identity():
    assert digest(CommunityPlan(days=7, events_per_day=1).manifest()) == (
        "fa3b4d9c5f802aa77e04ffe66d28878cfbe11c0f8c8513ba3d12db96189ff5f7"
    )


def test_daily_meeting_has_no_extra_debt_or_payment_and_same_initial_world():
    day, week = DailyMeetingPlan(days=1), DailyMeetingPlan(days=7)
    assert day.scene().content_hash() == week.scene().content_hash()
    assert all(o.max_spend == 0 for o in day.scene().owners)
    assert {f.id for f in day.scene().facts} == {
        "friendship",
        "club-roster",
        "existing-club-plan",
        "availability-alice",
        "availability-bob",
        "availability-charlie",
    }
    assert len(day.events()) == 1 and len(week.events()) == 7
    assert [e.at_seconds for e in week.events()] == [43200 + i * 86400 for i in range(7)]
    assert {e.payload["cue_kind"] for e in week.events()} == {"plan_change"}
    assert all(e.payload["owner_response_required"] is True for e in week.events())
    assert day.events()[0].recipients == ("actor-alice",)
    assert "16:00" in day.events()[0].payload["text"]
    assert "2026-11-24" in day.events()[0].payload["text"]
    assert all(e.at_seconds < week.clock().horizon_seconds for e in week.events())
    assert week.manifest()["chaos_calibration"]["calibrated"] is False


@pytest.mark.parametrize("events", [0, 2, True, 1.0])
def test_daily_meeting_rejects_other_frequencies(events):
    with pytest.raises(ValidationError):
        DailyMeetingPlan(events_per_day=events)


async def test_daily_cli_preparation_selects_one_event_without_provider_access(tmp_path):
    main = runpy.run_path(str(Path(__file__).parents[1] / "scripts/run_clock_actors.py"))["main"]

    async def forbidden(*args, **kwargs):
        raise AssertionError("Preparation must not access provider prices or models.")

    main.__globals__["fetch_model_prices"] = forbidden
    output = tmp_path / "plan.json"
    await main(
        Namespace(
            pairs=3,
            days=1,
            seed=20261109,
            event_profile="daily-meeting",
            events_per_day=None,
            scenario=None,
            no_chaos=False,
            prepare=output,
        )
    )
    saved = json.loads(output.read_text())
    assert saved["plan"]["version"] == "friends-community-daily-meeting-v4"
    assert len(saved["events"]) == 1
    assert saved["clock"]["horizon_seconds"] == 86400


@pytest.mark.parametrize("conflict", ["scenario", "no_chaos"])
async def test_daily_cli_rejects_silent_event_bypass_before_provider_access(conflict):
    main = runpy.run_path(str(Path(__file__).parents[1] / "scripts/run_clock_actors.py"))["main"]
    args = Namespace(event_profile="daily-meeting", scenario=None, no_chaos=False)
    setattr(args, conflict, Path("unused.json") if conflict == "scenario" else True)
    with pytest.raises(SystemExit, match="declared scene and events"):
        await main(args)
