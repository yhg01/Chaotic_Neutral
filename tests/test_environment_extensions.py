"""The full Director loop combines private mail, resources, and commerce."""

import asyncio
import json
from uuid import UUID, uuid5

import pytest

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput
from agentrpg.director import Director, row, rows
from agentrpg.inbox import InboxService
from agentrpg.market import MarketService
from agentrpg.resources import ResourceService
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario
from agentrpg.world import WorldError, WorldService


def scenario(count=2):
    data = vehicle_scenario(count).model_dump(mode="json")
    owners = [owner["id"] for owner in data["owners"]]
    data.update(id="extensions", name="Workspace and market integration", max_windows=6)
    data["resources"] = [
        {
            "id": f"note-{i}",
            "owner_id": o,
            "title": f"Notes {i}",
            "content": f"private-note-{i}",
            "agent_writable": False,
        }
        for i, o in enumerate(owners, 1)
    ]
    data["resources"].append(
        {
            "id": "battery",
            "owner_id": "owner-1",
            "title": "Spare battery",
            "kind": "item",
            "quantity": count - 1,
            "content": "private-stock-note",
        }
    )
    data["initial_mail"] = [
        {
            "id": "private-dispatch",
            "recipient_owner_id": "owner-1",
            "subject": "Private dispatch",
            "body": "private-mail-only-owner-1",
        }
    ]
    data["beats"] = [
        {
            "id": "start",
            "trigger": {"kind": "run_started"},
            "recipients": owners,
            "cue": "State your request and update your personal notes.",
        },
        {
            "id": "market",
            "trigger": {"kind": "window_reached", "window": 1},
            "recipients": owners,
            "cue": "Inspect the available listings and decide your purchase limit.",
        },
        {
            "id": "final",
            "trigger": {"kind": "window_reached", "window": 3},
            "recipients": owners,
            "cue": "Give your assistant any final information.",
            "mail": [
                {
                    "id": "final-mail",
                    "recipient_owner_id": "owner-1",
                    "subject": "Final instruction",
                    "body": "The declared deadline has arrived.",
                }
            ],
            "resource_effects": [
                {
                    "owner_id": "owner-1",
                    "operation": {
                        "kind": "update",
                        "resource_id": "note-1",
                        "expected_version": 2,
                        "content": "Director final note",
                    },
                }
            ],
        },
    ]
    return ScenarioSpec.model_validate(data)


class OwnerActor:
    def __init__(self, count):
        self.count = count
        self.views = []

    async def respond(self, view):
        self.views.append(view)
        index = int(view.owner_id.split("-")[-1])
        if view.window == 0:
            consent = None
            if index == 1:
                consent = {
                    "sell_limits": [
                        {"resource_id": "battery", "quantity": self.count - 1, "min_unit_price": 1}
                    ]
                }
            return ActorOutput(
                kind="reply",
                text="Please handle my request within my permissions.",
                source_event_id=view.source_event_id,
                commerce_consent=consent,
                mail_requests=[
                    {
                        "recipient_owner_id": f"owner-{index % self.count + 1}",
                        "subject": "Owner note",
                        "body": "Please keep this message private.",
                    }
                ],
                resource_requests=[
                    {
                        "kind": "update",
                        "resource_id": f"note-{index}",
                        "expected_version": 0,
                        "content": f"Owner note {index}",
                    },
                    {
                        "kind": "share",
                        "resource_id": f"note-{index}",
                        "expected_version": 1,
                        "visibility": "shared",
                        "shared_owner_ids": [f"owner-{index % self.count + 1}"],
                    },
                ],
            )
        if index > 1 and view.window == 1:
            return ActorOutput(
                kind="reply",
                text="I approve one battery within two credits.",
                source_event_id=view.source_event_id,
                commerce_consent={"buy_budget": 2, "buy_items": ["battery"]},
            )
        return ActorOutput(kind="wait", source_event_id=view.source_event_id)


class WorkspaceAgent:
    def __init__(self, client, run, participant, barrier=None):
        self.client = client
        self.run = run
        self.member = participant
        self.barrier = barrier
        self.calls = []

    async def run_activation(self, envelope):
        self.calls.append(envelope)
        if self.barrier and envelope.window_id == 0:
            await self.barrier.wait()
        index = int(self.member["owner_id"].split("-")[-1])
        headers = {"Authorization": f"Bearer {self.member['token']}"}
        prefix = f"/api/v1/runs/{self.run['run_id']}"
        if envelope.window_id == 0:
            response = await self.client.post(
                prefix + "/inbox",
                headers=headers,
                json={
                    "request_id": str(uuid5(envelope.activation_id, "mail")),
                    "recipient_owner_id": f"owner-{index % len(self.run['participants']) + 1}",
                    "subject": "Assistant logistics",
                    "body": f"Agent {index} requests coordination.",
                },
            )
            assert response.status_code == 201, response.text
            if index == 1:
                response = await self.client.post(
                    prefix + "/market/actions",
                    headers=headers,
                    json={
                        "request_id": str(uuid5(envelope.activation_id, "sell")),
                        "kind": "sell",
                        "resource_id": "battery",
                        "quantity": len(self.run["participants"]) - 1,
                        "unit_price": 1,
                    },
                )
                assert response.status_code == 200, response.text
                return ActivationResult(status="waiting", pending_ids=[UUID(response.json()["id"])])
            return ActivationResult(status="waiting")
        if envelope.window_id == 1 and index > 1:
            listings = (await self.client.get(prefix + "/market", headers=headers)).json()[
                "listings"
            ]
            response = await self.client.post(
                prefix + "/market/actions",
                headers=headers,
                json={
                    "request_id": str(uuid5(envelope.activation_id, "buy")),
                    "kind": "buy",
                    "listing_id": listings[0]["id"],
                    "quantity": 1,
                    "expected_version": 0,
                },
            )
            assert response.status_code == 200, response.text
            return ActivationResult(status="waiting", pending_ids=[UUID(response.json()["id"])])
        return ActivationResult(status="finished")


@pytest.mark.parametrize("count", [2, 15])
async def test_loop_delivers_private_records_actor_effects_and_market(client, pool, count):
    director = Director(pool)
    created = await director.create_run(scenario(count))
    run_id = UUID(created["run_id"])
    actors = {p["owner_id"]: OwnerActor(count) for p in created["participants"]}
    barrier = asyncio.Barrier(count)
    agents = {
        p["owner_id"]: WorkspaceAgent(client, created, p, barrier) for p in created["participants"]
    }
    engine = RunEngine(pool, actors=actors, agents=agents)
    result = await engine.run(run_id)
    assert result["status"] == "completed", result
    assert result["current_window"] >= 4, "A future declared beat must execute before early exit."
    market = MarketService(pool)
    ledger = await market.ledger(run_id)
    assert len(ledger) == count - 1
    world = WorldService(pool)
    snapshot = await world.snapshot(run_id)
    assert sum(a["balance"] for a in snapshot["accounts"]) + snapshot["treasury"] == 20 * count
    assert world.replay(await world.journal(run_id)) == snapshot
    resources = ResourceService(pool)
    snapshot = await resources.snapshot(run_id)
    assert sum(r["quantity"] for r in snapshot["resources"]) == count - 1
    assert resources.replay(await resources.journal(run_id)) == snapshot
    assert (await resources.fetch(run_id, "owner-1", "note-1"))["content"] == "Director final note"
    for owner, actor in actors.items():
        first = actor.views[0]
        assert all(r["owner_id"] == owner for r in first.resources["items"])
        if owner != "owner-1":
            assert "private-mail-only-owner-1" not in json.dumps(first.model_dump(mode="json"))
            assert "private-stock-note" not in json.dumps(first.model_dump(mode="json"))
            assert '"private-note-1"' not in json.dumps(first.model_dump(mode="json"))
        assert any(
            o.kind == "inbox_received"
            for activation in agents[owner].calls
            for o in activation.world_observations
        )
        assert any(
            o.kind == "market_settled"
            for activation in agents[owner].calls
            for o in activation.world_observations
        )
    trace = await director.trace(run_id)
    assert len([e for e in trace if e["kind"] == "market_settled"]) == count
    assert any(e["kind"] == "resource_updated" for e in trace)
    async with pool.connection() as conn:
        unreceived = await row(
            conn,
            """SELECT count(*) AS n FROM run_events e
            JOIN run_participants p ON p.run_id=e.run_id
            WHERE e.run_id=%s AND e.kind IN ('inbox_received','market_settled','resource_updated')
            AND (e.owner_id IS NULL OR e.owner_id=p.owner_id)
            AND NOT EXISTS(SELECT 1 FROM event_deliveries d WHERE d.event_id=e.id
                AND d.owner_id=p.owner_id AND d.recipient_kind='agent')""",
            (run_id,),
        )
    assert unreceived["n"] == 0
    mail = await InboxService(pool).list_messages(run_id, "owner-1", limit=100)
    assert any(m["subject"] == "Final instruction" for m in mail["items"])


async def test_saved_actor_effect_bundle_recovers_once(client, pool):
    director = Director(pool)
    created = await director.create_run(scenario())
    run_id = UUID(created["run_id"])
    actors = {p["owner_id"]: OwnerActor(2) for p in created["participants"]}
    agents = {p["owner_id"]: WorkspaceAgent(client, created, p) for p in created["participants"]}
    engine = RunEngine(pool, actors=actors, agents=agents)
    await director.control(run_id, "start")
    async with pool.connection() as conn:
        await conn.execute("""CREATE FUNCTION fail_owner_resource()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.kind='resource_updated' AND NEW.owner_id='owner-1' THEN
                RAISE EXCEPTION 'test actor effect failure'; END IF; RETURN NEW; END $$""")
        await conn.execute("""CREATE TRIGGER fail_owner_resource BEFORE INSERT ON run_events
            FOR EACH ROW EXECUTE FUNCTION fail_owner_resource()""")
    result = await engine.advance(run_id)
    assert result["status"] == "paused" and result["current_window"] == 0
    resource = await ResourceService(pool).fetch(run_id, "owner-1", "note-1")
    assert resource["version"] == 0
    permission = (await MarketService(pool).view(run_id, "owner-1"))["consent"]
    assert permission["version"] == 0
    sent = await InboxService(pool).list_messages(run_id, "owner-1", mailbox="sent")
    assert sent["items"] == []
    async with pool.connection() as conn:
        saved = await row(
            conn,
            "SELECT output,status FROM run_jobs WHERE run_id=%s "
            "AND owner_id='owner-1' AND kind='actor'",
            (run_id,),
        )
        assert saved["output"] is not None and saved["status"] == "saved"
        await conn.execute("DROP TRIGGER fail_owner_resource ON run_events")
    await director.control(run_id, "resume")
    result = await engine.advance(run_id)
    assert result["status"] == "running" and result["current_window"] == 1
    assert len(actors["owner-1"].views) == 1, "Saved output must not call the Actor again."
    assert (await ResourceService(pool).fetch(run_id, "owner-1", "note-1"))["version"] == 2
    permission = (await MarketService(pool).view(run_id, "owner-1"))["consent"]
    assert permission["version"] == 1
    sent = await InboxService(pool).list_messages(run_id, "owner-1", mailbox="sent")
    assert sum(m["sender_source"] == "actor" for m in sent["items"]) == 1
    assert sum(m["sender_source"] == "agent" for m in sent["items"]) == 1


async def test_director_beat_mail_and_resource_effects_share_transaction(pool):
    data = scenario().model_dump(mode="json")
    data["beats"][0]["mail"] = [
        {
            "id": "attempt-mail",
            "recipient_owner_id": "owner-2",
            "subject": "Atomic beat",
            "body": "Must not escape a failed beat.",
        }
    ]
    data["beats"][0]["resource_effects"] = [
        {
            "owner_id": "owner-1",
            "operation": {
                "kind": "update",
                "resource_id": "note-1",
                "expected_version": 99,
                "content": "Failed effect",
            },
        }
    ]
    director = Director(pool)
    created = await director.create_run(ScenarioSpec.model_validate(data))
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    with pytest.raises(WorldError, match="version has changed"):
        await director.fire_beats(run_id)
    inbox = await InboxService(pool).list_messages(run_id, "owner-2")
    assert inbox["items"] == []
    assert (await ResourceService(pool).fetch(run_id, "owner-1", "note-1"))["version"] == 0
    async with pool.connection() as conn:
        beats = await rows(conn, "SELECT * FROM run_beats WHERE run_id=%s", (run_id,))
    assert beats == []
