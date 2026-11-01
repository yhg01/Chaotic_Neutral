"""The grief scenario keeps private evidence and leaves contact decisions open."""

from pathlib import Path

import pytest

from agentrpg.actor import build_actor_view
from agentrpg.director import Director
from agentrpg.resources import ResourceError, ResourceService
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.world import WorldService


@pytest.fixture
def verdict_spec():
    return ScenarioSpec.model_validate_json(Path("scenarios/after_the_verdict.json").read_text())


def test_private_perspectives_do_not_reveal_peer_beliefs_or_future_cues(verdict_spec):
    spec = verdict_spec
    assert spec.evaluation.verification_profile == "conversation"
    assert not spec.world.assets
    assert not spec.evaluation.joint_resolution_expected
    for owner in spec.owners:
        view = build_actor_view(spec, owner.id, "Give your initial request.")
        visible_ids = {fact.id for fact in view.known_facts}
        for fact in spec.facts:
            if fact.visibility == "private" and owner.id not in fact.owner_ids:
                assert fact.id not in visible_ids
                assert fact.text not in view.model_dump_json()
        for beat in spec.beats:
            if beat.trigger.kind == "window_reached":
                assert beat.cue not in view.model_dump_json()
        assert owner.beliefs


async def test_memory_and_letter_start_private_in_postgresql(pool, verdict_spec):
    created = await Director(pool).create_run(verdict_spec)
    run_id = created["run_id"]
    resources = ResourceService(pool)
    for record in verdict_spec.resources:
        assert record.visibility == "private"
        assert not record.shared_owner_ids
        own = await resources.fetch(run_id, record.owner_id, record.id)
        assert own["content"] == record.content
        for owner in verdict_spec.owners:
            if owner.id == record.owner_id:
                continue
            with pytest.raises(ResourceError) as error:
                await resources.fetch(run_id, owner.id, record.id)
            assert error.value.status_code == 404
            found = await resources.search(run_id, owner.id, record.title)
            assert record.id not in {item["id"] for item in found["items"]}
    world = await WorldService(pool).snapshot(run_id)
    assert not world["reservations"]
    assert not world["assets"]


async def test_clock_cues_do_not_force_contact_or_sharing(pool, verdict_spec):
    director = Director(pool)
    created = await director.create_run(verdict_spec)
    run_id = created["run_id"]
    resources = ResourceService(pool)
    before = await resources.snapshot(run_id)
    await director.control(run_id, "start")
    for window in range(verdict_spec.max_windows):
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE environment_runs SET current_window=%s WHERE id=%s", (window, run_id)
            )
        await director.fire_beats(run_id)
        await director.fire_beats(run_id)
    trace = await director.trace(run_id)
    cues = [event for event in trace if event["kind"] == "actor_cue"]
    assert len(cues) == sum(len(beat.recipients) for beat in verdict_spec.beats)
    assert len({event["operation_key"] for event in cues}) == len(cues)
    assert await resources.snapshot(run_id) == before
    assert not any(event["kind"] == "market_settled" for event in trace)
    assert all(not beat.mail and not beat.resource_effects for beat in verdict_spec.beats)
