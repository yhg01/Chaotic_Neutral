"""Owner proposal rejection can revoke consent before assistant settlement."""

from uuid import UUID, uuid4

import pytest

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput
from agentrpg.director import Director, save_message
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.world import WorldService


@pytest.mark.parametrize("decision", ["reject", "counter"])
async def test_proposal_withdrawal_reaches_world_and_blocks_new_reservation(pool, decision):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario(1))
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    world = WorldService(pool)
    await world.set_consent(run_id, "owner-1", 20, ["taxi-1"], uuid4())
    async with pool.connection() as connection:
        async with connection.transaction():
            proposal = await save_message(
                connection,
                run_id,
                "owner-1",
                "agent",
                "May I reserve taxi-1 for 20 credits?",
                uuid4(),
                0,
            )

    class WithdrawActor:
        async def respond(self, view):
            return ActorOutput(
                kind="decide_proposal",
                text="I reject that option and withdraw all consent.",
                source_event_id=view.source_event_id,
                proposal_id=str(proposal["id"]),
                decision=decision,
                authorized_amount=0,
                allowed_assets=(),
                allowed_kinds=(),
            )

    class ReservationAttempt:
        async def run_activation(self, envelope):
            await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "taxi-1")
            return ActivationResult(status="finished", text="Reservation request submitted.")

    result = await RunEngine(
        pool, actors={"owner-1": WithdrawActor()}, agents={"owner-1": ReservationAttempt()}
    ).advance(run_id)
    assert result["status"] != "invalid"
    snapshot = await world.snapshot(run_id)
    account = snapshot["accounts"][0]
    assert account["consent_budget"] == 0
    assert account["consent_assets"] == account["consent_kinds"] == []
    assert account["balance"] == 20 and snapshot["treasury"] == 0
    assert not any(item["asset_id"] == "taxi-1" for item in snapshot["reservations"])
    trace = await director.trace(run_id)
    settlement = next(item for item in trace if item["kind"] == "reservation_settled")
    assert settlement["payload"]["status"] == "rejected"
    assert settlement["payload"]["reason"] == "consent_required"
