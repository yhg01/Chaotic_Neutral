"""Verify the storm plot's declared observations against real stored effects."""

from pathlib import Path

from agentrpg.director import Director, row
from agentrpg.inbox import InboxService
from agentrpg.scenario_schema import ScenarioSpec

STORM_SPEC = ScenarioSpec.model_validate_json(
    Path("scenarios/community_storm_exchange.json").read_text()
)


async def test_storm_plot_seeded_records_and_scheduled_effects(pool):
    spec = STORM_SPEC
    director, inbox = Director(pool), InboxService(pool)
    created = await director.create_run(spec)
    run_id = created["run_id"]
    assert len(created["participants"]) == 2
    for owner in spec.owners:
        mail = await inbox.list_messages(run_id, owner.id)
        assert len(mail["items"]) == 1
        assert mail["items"][0]["sender_source"] == "director"
    await director.control(run_id, "start")
    await director.fire_beats(run_id)
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=2 WHERE id=%s", (run_id,))
    await director.fire_beats(run_id)
    await director.fire_beats(run_id)
    async with pool.connection() as conn:
        bulletin = await row(
            conn,
            "SELECT * FROM owner_resources WHERE run_id=%s AND resource_id='storm-bulletin'",
            (run_id,),
        )
    assert bulletin["version"] == 1
    assert "delayed" in bulletin["content"]
    for owner in spec.owners:
        assert len((await inbox.list_messages(run_id, owner.id))["items"]) == 2
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=3 WHERE id=%s", (run_id,))
    await director.fire_beats(run_id)
    received = (await inbox.list_messages(run_id, "owner-2"))["items"]
    assert len(received) == 3
    assert received[0]["sender_owner_id"] == "owner-1"
    assert received[0]["sender_source"] == "actor"
    assert received[0]["subject"] == "From Sam: there may be one spare battery"
    events = await director.trace(run_id)
    assert len([event for event in events if event["kind"] == "inbox_received"]) == 5
