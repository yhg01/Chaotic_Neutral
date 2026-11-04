"""Actual graph tools, frozen legacy hashes, and durable report recovery."""

import hashlib
import json
from uuid import UUID, uuid4

import httpx
from test_deep_agent import ScriptedModel, call
from test_scenario_cli import cli

from agentrpg.activation import ActivationEnvelope
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, emit
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.market import MarketService
from agentrpg.scenario_schema import ScenarioSpec, Trigger, vehicle_scenario


def test_optional_extensions_keep_legacy_frozen_hash():
    spec = vehicle_scenario()
    old = spec.model_dump(mode="json")
    old.pop("resources")
    old.pop("initial_mail")
    old["evaluation"].pop("expected_trade_quantity_per_owner")
    old["evaluation"].pop("verification_profile")
    for beat in old["beats"]:
        beat.pop("mail")
        beat.pop("resource_effects")
    for owner in old["owners"]:
        owner.pop("voice")
    canonical = json.dumps(old, sort_keys=True, separators=(",", ":"))
    assert spec.content_hash() == "93b0a93337f79036a1fc4181775bdc42ef2a1eafc3092daaf70a90eb0ee1c410"
    assert spec.content_hash() == hashlib.sha256(canonical.encode()).hexdigest()
    assert ScenarioSpec.model_validate(old).content_hash() == spec.content_hash()


async def test_legacy_run_gets_empty_resources_without_world_change(pool):
    director = Director(pool)
    created = await director.create_run(vehicle_scenario())
    run_id = UUID(created["run_id"])
    before = await director.get_run(run_id)
    async with pool.connection() as conn, conn.transaction():
        for table in ("resource_journal", "market_permissions", "resource_state"):
            # Each table is a fixed application name in this isolated test schema.
            await conn.execute(f"DELETE FROM {table} WHERE run_id=%s", (run_id,))
    await director.ensure_extensions(run_id)
    await director.ensure_extensions(run_id)
    after = await director.get_run(run_id)
    assert before == after
    assert (await MarketService(pool).view(run_id, "owner-1"))["consent"]["buy_budget"] == 0


async def test_real_graph_market_yield_and_durable_private_tool_events(
    app, pool, test_database_url
):
    data = vehicle_scenario().model_dump(mode="json")
    data["resources"] = [
        {
            "id": "item",
            "owner_id": "owner-1",
            "title": "Item",
            "kind": "item",
            "quantity": 2,
            "visibility": "public",
        }
    ]
    director = Director(pool)
    created = await director.create_run(ScenarioSpec.model_validate(data))
    run_id = UUID(created["run_id"])
    first = created["participants"][0]
    await director.control(run_id, "start")
    await MarketService(pool).set_consent(
        run_id,
        "owner-1",
        0,
        [],
        [{"resource_id": "item", "quantity": 1, "min_unit_price": 5}],
        uuid4(),
    )
    model = ScriptedModel(
        responses=[
            call(
                "send_mail",
                {"recipient_owner_id": "owner-2", "subject": "Offer", "body": "Five credits."},
            ),
            call(
                "submit_market_action",
                {"kind": "sell", "resource_id": "item", "quantity": 1, "unit_price": 5},
            ),
        ]
    )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": f"Bearer {first['token']}"},
        ) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        agent = runtime.agent(
            model=model,
            board=BoardTools(client, created["cluster_id"]),
            environment=EnvironmentTools(client, run_id),
            agent_id=first["agent_id"],
            session_id=first["session_id"],
            instructions="Complete the permitted tools.",
            max_model_calls=2,
        )
        result = await agent.run_activation(
            ActivationEnvelope(
                run_id=run_id, participant_id=first["agent_id"], activation_id=uuid4(), window_id=0
            )
        )
        assert result.status == "waiting" and result.pending_ids
        assert model.calls == 2
    events = await director.trace(run_id)
    tool_events = [e for e in events if e["kind"] == "assistant_tool_call"]
    assert {e["payload"]["tool"] for e in tool_events} == {"send_mail", "submit_market_action"}
    assert {e["owner_id"] for e in tool_events} == {"owner-1"}
    evidence, tools = cli.evaluation_evidence(run_id, events, [], {})
    assert len(tools) == 2
    assert sum(e["kind"] == "assistant_tool_call" for e in evidence) == 2
    assert {t["source_event_id"] for t in tools} == {str(e["id"]) for e in tool_events}
    assert first["token"] not in json.dumps(evidence, default=str)


async def test_director_can_branch_on_new_events(pool):
    data = vehicle_scenario(1).model_dump(mode="json")
    data["beats"].append(
        {
            "id": "trade-result",
            "trigger": {
                "kind": "market_settled",
                "owner_id": "owner-1",
                "result_status": "accepted",
            },
            "recipients": ["owner-1"],
            "cue": "Discuss the accepted trade.",
        }
    )
    director = Director(pool)
    run_id = UUID((await director.create_run(ScenarioSpec.model_validate(data)))["run_id"])
    await director.control(run_id, "start")
    async with pool.connection() as conn:
        await emit(conn, run_id, "market_settled", 0, {"status": "accepted"}, "trade", "owner-1")
    await director.fire_beats(run_id)
    await director.fire_beats(run_id)
    assert (
        sum(
            e["kind"] == "actor_cue" and e["payload"]["beat_id"] == "trade-result"
            for e in await director.trace(run_id)
        )
        == 1
    )
    for kind in ("inbox_received", "resource_updated", "commerce_consent_changed"):
        assert Trigger(kind=kind).kind == kind


async def test_invalid_actor_run_does_not_leave_other_jobs_running(pool):
    from agentrpg.actor import ActorContractError, ScriptedActor
    from agentrpg.run_engine import RunEngine

    class InvalidActor:
        async def respond(self, view):
            raise ActorContractError("Invalid owner response.")

    director = Director(pool)
    run_id = UUID((await director.create_run(vehicle_scenario()))["run_id"])
    engine = RunEngine(
        pool,
        actors={"owner-1": InvalidActor(), "owner-2": ScriptedActor()},
        agents={"owner-1": object(), "owner-2": object()},
    )
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert result["status"] == "invalid"
    async with pool.connection() as conn:
        jobs = await (
            await conn.execute("SELECT status,lease_until FROM run_jobs WHERE run_id=%s", (run_id,))
        ).fetchall()
    assert all(
        job["status"] in {"failed", "completed"} and job["lease_until"] is None for job in jobs
    )
