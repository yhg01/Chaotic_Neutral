"""Real PostgreSQL checks for owner resources and exact simulated commerce."""

import asyncio
import hashlib
import json
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agentrpg.market import MarketService
from agentrpg.resource_schema import ResourceRequest, ResourceSpec
from agentrpg.resources import ResourceService
from agentrpg.world import WorldError, WorldService


@pytest.fixture
def environment(pool, provision):
    async def create(count=2, quantity=2, credits=20):
        cluster, agents = await provision(name=f"Resources {uuid4()}", count=count)
        run_id = uuid4()
        owners = [f"owner-{index + 1}" for index in range(count)]
        spec = {
            "owners": [{"id": owner} for owner in owners],
            "world": {
                "assets": [{"id": "car", "price": 10}],
                "balances": dict.fromkeys(owners, credits),
            },
        }
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO environment_runs(id,cluster_id,scenario,scenario_hash,
                status,seed,max_windows) VALUES(%s,%s,%s,'test','running',23,5)""",
                (run_id, cluster["id"], Jsonb(spec)),
            )
            for owner, agent in zip(owners, agents, strict=True):
                await conn.execute(
                    "INSERT INTO run_participants(run_id,owner_id,agent_id,session_id) "
                    "VALUES(%s,%s,%s,%s)",
                    (run_id, owner, agent["agent"]["id"], uuid4()),
                )
        world = WorldService(pool)
        await world.initialize(run_id, spec)
        resources = ResourceService(pool)
        await resources.initialize(
            run_id,
            [
                {
                    "id": "manual",
                    "owner_id": "owner-1",
                    "title": "Private Manual",
                    "content": "hidden medical note",
                    "agent_writable": False,
                },
                {"id": "plan", "owner_id": "owner-2", "title": "Travel plan", "content": "Draft"},
                {
                    "id": "battery",
                    "owner_id": "owner-1",
                    "title": "Spare battery",
                    "kind": "item",
                    "quantity": quantity,
                    "content": "private inventory notes",
                },
            ],
        )
        return run_id, resources, MarketService(pool), world, agents

    return create


async def advance(pool, run_id, window):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET current_window=%s WHERE id=%s", (window, run_id)
        )


async def listing(market, run_id, quantity=1, price=7):
    await market.set_consent(
        run_id,
        "owner-1",
        0,
        [],
        [{"resource_id": "battery", "quantity": quantity, "min_unit_price": price}],
        uuid4(),
    )
    request = uuid4()
    pending = await market.submit_current(
        run_id,
        "owner-1",
        request,
        "sell",
        resource_id="battery",
        quantity=quantity,
        unit_price=price,
    )
    assert pending["status"] == "pending"
    result = (await market.settle(run_id, 0, 23))[0]
    assert result["status"] == "accepted"
    return result["listing_id"]


async def test_private_resources_search_and_explicit_sharing(environment, pool):
    run_id, resources, market, _, _ = await environment()
    assert (await resources.search(run_id, "owner-2", "hidden"))["items"] == []
    with pytest.raises(WorldError, match="not found"):
        await resources.fetch(run_id, "owner-2", "manual")
    with pytest.raises(WorldError, match="read-only"):
        await resources.update(run_id, "owner-1", "manual", uuid4(), "edit", 0)
    updated = await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {"kind": "update", "resource_id": "manual", "content": "Owner edit", "expected_version": 0},
    )
    assert updated["version"] == 1
    shared = await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {
            "kind": "share",
            "resource_id": "manual",
            "expected_version": 1,
            "visibility": "shared",
            "shared_owner_ids": ["owner-2"],
        },
    )
    assert shared["version"] == 2
    assert (await resources.fetch(run_id, "owner-2", "manual"))["content"] == "Owner edit"
    with pytest.raises(WorldError, match="not found"):
        await resources.update(run_id, "owner-2", "manual", uuid4(), "stolen", 2)
    with pytest.raises(WorldError, match="other owners"):
        await resources.apply_request(
            run_id,
            "owner-1",
            uuid4(),
            {
                "kind": "share",
                "resource_id": "manual",
                "expected_version": 2,
                "visibility": "shared",
                "shared_owner_ids": ["outsider"],
            },
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,))
            assert (await resources.actor_view(run_id, "owner-1", connection=conn))["items"]
            assert (await market.view(run_id, "owner-1", connection=conn))["balance"] == 20
    assert resources.replay(await resources.journal(run_id)) == await resources.snapshot(run_id)


async def test_resource_create_idempotency_versions_and_owner_boundaries(environment, pool):
    run_id, resources, _, _, _ = await environment()
    request = uuid4()
    made = await asyncio.gather(
        *(resources.create_document(run_id, "owner-1", request, "Notes", "Test") for _ in range(15))
    )
    assert all(r == made[0] for r in made)
    assert made[0]["kind"] == "document" and made[0]["quantity"] == 0
    with pytest.raises(WorldError, match="different payload"):
        await resources.create_document(run_id, "owner-1", request, "Notes", "changed")
    write_id = uuid4()
    updated = await resources.update(run_id, "owner-1", made[0]["id"], write_id, "Final", 0)
    assert updated["version"] == 1
    assert await resources.update(run_id, "owner-1", made[0]["id"], write_id, "Final", 0) == updated
    with pytest.raises(WorldError, match="version has changed"):
        await resources.update(run_id, "owner-1", made[0]["id"], uuid4(), "Lost", 0)
    await advance(pool, run_id, 1)
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET status='paused' WHERE id=%s", (run_id,))
    assert await resources.create_document(run_id, "owner-1", request, "Notes", "Test") == made[0]
    with pytest.raises(WorldError, match="not running"):
        await resources.create_document(run_id, "owner-1", uuid4(), "Notes", "new")
    with pytest.raises(WorldError, match="not found"):
        await resources.fetch(uuid4(), "owner-1", made[0]["id"])


async def test_purchase_transfers_stock_and_shared_wallet_once(environment, pool):
    run_id, resources, market, world, _ = await environment()
    item = await listing(market, run_id)
    assert (await resources.fetch(run_id, "owner-2", item))["type"] == "listing"
    with pytest.raises(WorldError, match="not found"):
        await resources.fetch(run_id, "owner-2", "battery")
    await advance(pool, run_id, 1)
    await market.set_consent(run_id, "owner-2", 7, ["battery"], [], uuid4())
    request = uuid4()
    submitted = await asyncio.gather(
        *(
            market.submit_current(run_id, "owner-2", request, "buy", listing_id=item)
            for _ in range(15)
        )
    )
    assert all(r == submitted[0] for r in submitted)
    assert (await world.view(run_id, "owner-2"))["account"]["balance"] == 20
    results = await asyncio.gather(*(market.settle(run_id, 1, 23) for _ in range(5)))
    result = results[0][0]
    assert result["status"] == "accepted" and result["amount"] == 7
    assert all(r == results[0] for r in results)
    holding = await resources.fetch(run_id, "owner-2", result["resource_id"])
    assert (
        holding["quantity"] == 1 and holding["content"] == "" and holding["visibility"] == "private"
    )
    state = await world.snapshot(run_id)
    assert [r["balance"] for r in state["accounts"]] == [27, 13]
    assert sum(r["balance"] for r in state["accounts"]) + state["treasury"] == 40
    assert len(await market.ledger(run_id)) == 1
    assert resources.replay(await resources.journal(run_id)) == await resources.snapshot(run_id)
    assert world.replay(await world.journal(run_id)) == state
    await advance(pool, run_id, 2)
    assert await market.submit_current(run_id, "owner-2", request, "buy", listing_id=item) == result
    with pytest.raises(WorldError, match="different payload"):
        await market.submit_current(run_id, "owner-2", request, "buy", listing_id=item, quantity=2)
    with pytest.raises(WorldError, match="receipt not found"):
        await market.receipt(run_id, "owner-1", request)


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "buy_consent_required"),
        ("revoked", "consent_changed"),
        ("seller_revoked", "seller_consent_changed"),
        ("expired", "consent_expired"),
        ("budget", "buy_budget_exceeded"),
        ("stock", "quantity_unavailable"),
        ("balance", "insufficient_balance"),
    ],
)
async def test_purchase_authority_is_checked_at_settlement(environment, pool, mode, reason):
    run_id, resources, market, world, _ = await environment(credits=5 if mode == "balance" else 20)
    item = await listing(market, run_id)
    await advance(pool, run_id, 1)
    if mode != "missing":
        await market.set_consent(
            run_id,
            "owner-2",
            3 if mode == "budget" else 20,
            ["battery"],
            [],
            uuid4(),
            expires_window=0 if mode == "expired" else None,
        )
    await market.submit_current(
        run_id,
        "owner-2",
        uuid4(),
        "buy",
        listing_id=item,
        quantity=2 if mode == "stock" else 1,
    )
    if mode == "revoked":
        await market.set_consent(run_id, "owner-2", 0, [], [], uuid4())
    if mode == "seller_revoked":
        await market.set_consent(run_id, "owner-1", 0, [], [], uuid4())
        assert (await market.view(run_id, "owner-2"))["listings"] == []
    before = await world.snapshot(run_id)
    result = (await market.settle(run_id, 1, 23))[0]
    assert result["status"] == "rejected" and result["reason"] == reason
    assert await world.snapshot(run_id) == before
    assert (await resources.fetch(run_id, "owner-1", "battery"))["quantity"] == 2


@pytest.mark.parametrize("kind", ["sell", "buy"])
async def test_stale_submission_receipt_allows_same_window_tool_correction(
    environment, app, pool, kind
):
    import httpx

    from agentrpg.environment_tools import EnvironmentTools

    run_id, resources, market, world, agents = await environment()
    if kind == "buy":
        target = await listing(market, run_id)
        window, owner_index = 1, 1
        await advance(pool, run_id, window)
        await market.set_consent(run_id, "owner-2", 20, ["battery"], [], uuid4())
        arguments = {"kind": "buy", "listing_id": target}
    else:
        target, window, owner_index = "battery", 0, 0
        await market.set_consent(
            run_id,
            "owner-1",
            0,
            [],
            [{"resource_id": "battery", "quantity": 1, "min_unit_price": 7}],
            uuid4(),
        )
        arguments = {"kind": "sell", "resource_id": target, "unit_price": 7}
    owner = f"owner-{owner_index + 1}"
    before = await resources.snapshot(run_id)
    world_before = await world.snapshot(run_id)
    original_journal = await resources.journal(run_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": f"Bearer {agents[owner_index]['token']}"},
    ) as http:
        tools = EnvironmentTools(http, run_id)
        stale_arguments = {**arguments, "expected_version": 1}
        rejected = await tools.execute("submit_market_action", stale_arguments, "stale-call")
        assert rejected["ok"] and rejected["data"]["status"] == "rejected"
        receipt = rejected["data"]
        assert receipt["reason"] == "stale_version"
        assert receipt["validation_phase"] == "submission"
        assert receipt["expected_version"] == 1 and receipt["current_version"] == 0
        assert receipt["version_target_type"] == ("resource" if kind == "sell" else "listing")
        assert await resources.snapshot(run_id) == before
        assert await world.snapshot(run_id) == world_before
        assert await resources.journal(run_id) == original_journal
        assert (
            await tools.execute("submit_market_action", stale_arguments, "stale-call") == rejected
        )
        assert (
            await tools.execute("read_market_action", {"action_id": receipt["id"]}, "read-rejected")
        )["data"] == receipt
        changed = await tools.execute(
            "submit_market_action", {**arguments, "expected_version": 0}, "stale-call"
        )
        assert changed["error"]["status"] == 409
        recovery = receipt["recovery"]
        assert recovery["arguments"] == {"resource_id": target}
        assert "existing consent" in recovery["instruction"]
        fetched = await tools.execute(recovery["tool"], recovery["arguments"], "fetch-target")
        corrected = await tools.execute(
            "submit_market_action",
            {**arguments, "expected_version": fetched["data"]["version"]},
            "corrected-call",
        )
        assert corrected["data"]["status"] == "pending"
        result = (await market.settle(run_id, window, 23))[0]
        assert result["status"] == "accepted"
        assert (await market.view(run_id, owner))["consent"]["version"] == 1
        assert resources.replay(await resources.journal(run_id)) == await resources.snapshot(run_id)
        async with pool.connection() as conn:
            intent_count = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM market_intents WHERE run_id=%s AND owner_id=%s",
                    (run_id, owner),
                )
            ).fetchone()
            event_count = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM run_events WHERE run_id=%s AND owner_id=%s "
                    "AND operation_key LIKE 'market:submission_rejected:%%'",
                    (run_id, owner),
                )
            ).fetchone()
            await conn.execute(
                "UPDATE environment_runs SET status='completed' WHERE id=%s", (run_id,)
            )
        assert intent_count["n"] == event_count["n"] == 1
        assert (
            await tools.execute("submit_market_action", stale_arguments, "stale-call") == rejected
        )
        assert not (await tools.execute("submit_market_action", stale_arguments, "new-after-end"))[
            "ok"
        ]


async def test_resource_change_after_submit_is_still_rejected_at_settlement(environment, pool):
    run_id, resources, market, world, _ = await environment()
    await market.set_consent(
        run_id,
        "owner-1",
        0,
        [],
        [{"resource_id": "battery", "quantity": 1, "min_unit_price": 7}],
        uuid4(),
    )
    pending = await market.submit_current(
        run_id, "owner-1", uuid4(), "sell", resource_id="battery", unit_price=7, expected_version=0
    )
    assert pending["status"] == "pending"
    await resources.update(run_id, "owner-1", "battery", uuid4(), "Updated item note", 0)
    before = await world.snapshot(run_id)
    rejected = (await market.settle(run_id, 0, 23))[0]
    assert rejected["reason"] == "stale_version"
    assert rejected["validation_phase"] == "settlement"
    assert rejected["expected_version"] == 0 and rejected["current_version"] == 1
    assert rejected["recovery"]["arguments"] == {"resource_id": "battery"}
    assert await world.snapshot(run_id) == before
    await advance(pool, run_id, 1)
    resource = await resources.fetch(run_id, "owner-1", "battery")
    await market.submit_current(
        run_id,
        "owner-1",
        uuid4(),
        "sell",
        resource_id="battery",
        unit_price=7,
        expected_version=resource["version"],
    )
    assert (await market.settle(run_id, 1, 23))[0]["status"] == "accepted"
    assert (await market.view(run_id, "owner-1"))["consent"]["version"] == 1


async def test_stale_submission_receipt_and_notification_commit_together(environment, monkeypatch):
    run_id, _, market, _, _ = await environment()
    request_id = uuid4()
    original = market.world._event

    async def failed_event(*args, **kwargs):
        raise RuntimeError("Injected notification failure.")

    monkeypatch.setattr(market.world, "_event", failed_event)
    with pytest.raises(RuntimeError, match="notification failure"):
        await market.submit_current(
            run_id,
            "owner-1",
            request_id,
            "sell",
            resource_id="battery",
            unit_price=7,
            expected_version=1,
        )
    with pytest.raises(WorldError, match="not found"):
        await market.receipt(run_id, "owner-1", request_id)
    monkeypatch.setattr(market.world, "_event", original)
    result = await market.submit_current(
        run_id,
        "owner-1",
        request_id,
        "sell",
        resource_id="battery",
        unit_price=7,
        expected_version=0,
    )
    assert result["status"] == "pending"


async def test_fifteen_clients_contend_without_overselling(environment, pool):
    winners = []
    for reverse in [False, True]:
        run_id, resources, market, world, _ = await environment(count=15, quantity=1)
        item = await listing(market, run_id)
        await advance(pool, run_id, 1)
        owners = [f"owner-{i}" for i in range(1, 16)]
        await asyncio.gather(
            *(
                market.set_consent(
                    run_id,
                    o,
                    20,
                    ["battery"],
                    [{"resource_id": "battery", "quantity": 1, "min_unit_price": 7}]
                    if o == "owner-1"
                    else [],
                    uuid4(),
                )
                for o in owners[1:]
            )
        )
        if reverse:
            owners.reverse()
        pending = await asyncio.gather(
            *(
                market.submit_current(
                    run_id, o, uuid4(), "buy", listing_id=item, expected_version=0
                )
                for o in owners
            )
        )
        assert len(pending) == 15 and all(r["status"] == "pending" for r in pending)
        results = await market.settle(run_id, 1, 23)
        accepted = [r for r in results if r["status"] == "accepted"]
        assert len(accepted) == 1
        winners.append(accepted[0]["owner_id"])
        assert sum(r["reason"] == "self_purchase_not_allowed" for r in results) == 1
        assert sum(r["reason"] == "quantity_unavailable" for r in results) == 13
        inventory = (await resources.snapshot(run_id))["resources"]
        assert sum(r["quantity"] for r in inventory) == 1
        state = await world.snapshot(run_id)
        assert sum(r["balance"] for r in state["accounts"]) + state["treasury"] == 300
    assert winners[0] == winners[1]


async def test_transport_spend_and_market_spend_use_same_wallet(environment, pool):
    run_id, _, market, world, _ = await environment(credits=12)
    item = await listing(market, run_id)
    await advance(pool, run_id, 1)
    await market.set_consent(run_id, "owner-2", 12, ["battery"], [], uuid4())
    await world.set_consent(run_id, "owner-2", 12, ["car"], uuid4())
    await market.submit_current(run_id, "owner-2", uuid4(), "buy", listing_id=item)
    await world.submit_current(run_id, "owner-2", uuid4(), "reserve", "car")
    assert (await world.settle(run_id, 1, 23))[0]["status"] == "accepted"
    assert (await market.settle(run_id, 1, 23))[0]["reason"] == "insufficient_balance"
    state = await world.snapshot(run_id)
    assert sum(r["balance"] for r in state["accounts"]) + state["treasury"] == 24
    assert world.replay(await world.journal(run_id)) == state


async def test_purchase_effects_and_receipts_rollback_on_event_failure(environment, pool):
    run_id, resources, market, world, _ = await environment()
    item = await listing(market, run_id)
    await advance(pool, run_id, 1)
    await market.set_consent(run_id, "owner-2", 20, ["battery"], [], uuid4())
    request = uuid4()
    await market.submit_current(run_id, "owner-2", request, "buy", listing_id=item)
    world_before, resources_before = await world.snapshot(run_id), await resources.snapshot(run_id)
    async with pool.connection() as conn:
        await conn.execute("""CREATE FUNCTION reject_market_event()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.kind='market_settled' THEN RAISE EXCEPTION 'test failure'; END IF;
            RETURN NEW; END $$""")
        await conn.execute(
            "CREATE TRIGGER reject_market_event BEFORE INSERT ON run_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_market_event()"
        )
    with pytest.raises(psycopg.Error):
        await market.settle(run_id, 1, 23)
    assert await world.snapshot(run_id) == world_before
    assert await resources.snapshot(run_id) == resources_before
    assert (await market.receipt(run_id, "owner-2", request))["status"] == "pending"
    async with pool.connection() as conn:
        await conn.execute("DROP TRIGGER reject_market_event ON run_events")
    assert (await market.settle(run_id, 1, 23))[0]["status"] == "accepted"


def test_resource_contracts_reject_asset_minting_and_invalid_sharing():
    for invalid in [
        {"id": "a", "owner_id": "o", "title": "X", "quantity": 1},
        {"id": "a", "owner_id": "o", "title": "X", "shared_owner_ids": ["p"]},
    ]:
        with pytest.raises(ValueError):
            ResourceSpec.model_validate(invalid)
    with pytest.raises(ValueError):
        ResourceRequest(kind="create", title="Mint", content="", quantity=100)


async def test_http_identity_and_permissions_are_server_bound(environment, client):
    run_id, resources, _, _, agents = await environment()
    prefix = f"/api/v1/runs/{run_id}"
    owner1 = {"Authorization": f"Bearer {agents[0]['token']}"}
    owner2 = {"Authorization": f"Bearer {agents[1]['token']}"}
    # Administrators observe through their own interface, never impersonate an Agent.
    assert (await client.get(prefix + "/resources")).status_code == 403
    own = await client.get(prefix + "/resources", headers=owner1)
    assert own.status_code == 200 and {r["id"] for r in own.json()["items"]} == {
        "manual",
        "battery",
    }
    assert (await client.get(prefix + "/resources/manual", headers=owner2)).status_code == 404
    assert (
        await client.get(f"/api/v1/runs/{uuid4()}/resources", headers=owner1)
    ).status_code == 404
    create = {"request_id": str(uuid4()), "title": "Notes", "content": "plan"}
    response = await client.post(
        prefix + "/resources", json={**create, "quantity": 5}, headers=owner1
    )
    assert response.status_code == 422
    response = await client.post(
        prefix + "/resources", json={**create, "owner_id": "owner-2"}, headers=owner1
    )
    assert response.status_code == 422
    response = await client.post(prefix + "/resources", json=create, headers=owner1)
    assert response.status_code == 201
    assert response.json()["owner_id"] == "owner-1" and response.json()["kind"] == "document"
    assert (
        await client.post(prefix + "/resources", json={**create, "title": "  "}, headers=owner1)
    ).status_code == 422
    assert (
        await client.put(
            prefix + "/resources/manual",
            headers=owner1,
            json={"request_id": str(uuid4()), "expected_version": 0, "content": "unauthorized"},
        )
    ).status_code == 403
    response = await client.post(
        prefix + "/market/actions",
        headers=owner2,
        json={
            "request_id": str(uuid4()),
            "kind": "sell",
            "resource_id": "battery",
            "unit_price": 7,
        },
    )
    assert response.status_code == 404
    response = await client.post(
        prefix + "/market/actions",
        headers=owner1,
        json={
            "request_id": str(uuid4()),
            "kind": "sell",
            "resource_id": "battery",
            "unit_price": 7,
            "buy_budget": 100,
        },
    )
    assert response.status_code == 422
    assert (await resources.fetch(run_id, "owner-1", "manual"))["content"] == "hidden medical note"


async def test_sell_authority_price_quantity_and_conflicts(environment, pool):
    run_id, _, market, _, _ = await environment(quantity=3)
    await market.submit_current(
        run_id, "owner-1", uuid4(), "sell", resource_id="battery", unit_price=7
    )
    assert (await market.settle(run_id, 0, 23))[0]["reason"] == "sell_consent_required"
    await advance(pool, run_id, 1)
    await market.set_consent(
        run_id,
        "owner-1",
        0,
        [],
        [{"resource_id": "battery", "quantity": 2, "min_unit_price": 7}],
        uuid4(),
    )
    await market.submit_current(
        run_id, "owner-1", uuid4(), "sell", resource_id="battery", unit_price=6
    )
    assert (await market.settle(run_id, 1, 23))[0]["reason"] == "minimum_price_not_met"
    await advance(pool, run_id, 2)
    await market.submit_current(
        run_id, "owner-1", uuid4(), "sell", resource_id="battery", quantity=3, unit_price=7
    )
    assert (await market.settle(run_id, 2, 23))[0]["reason"] == "quantity_unavailable"
    await advance(pool, run_id, 3)
    request = uuid4()
    await market.submit_current(
        run_id, "owner-1", request, "sell", resource_id="battery", quantity=2, unit_price=7
    )
    with pytest.raises(WorldError, match="one intent"):
        await market.submit_current(
            run_id, "owner-1", uuid4(), "sell", resource_id="battery", unit_price=7
        )
    assert (await market.settle(run_id, 3, 23))[0]["status"] == "accepted"
    await advance(pool, run_id, 4)
    await market.submit_current(
        run_id, "owner-1", uuid4(), "sell", resource_id="battery", unit_price=7
    )
    assert (await market.settle(run_id, 4, 23))[0]["reason"] == "quantity_unavailable"
    with pytest.raises(WorldError, match="already settled"):
        await market.submit_current(
            run_id, "owner-2", uuid4(), "sell", resource_id="battery", unit_price=7
        )
    with pytest.raises(WorldError, match="different seed"):
        await market.settle(run_id, 4, 24)


async def test_resource_actor_request_rollback_and_authority(environment, pool):
    run_id, resources, _, _, _ = await environment()
    before = await resources.snapshot(run_id)
    with pytest.raises(RuntimeError):
        async with pool.connection() as conn:
            async with conn.transaction():
                await resources.apply_request(
                    run_id,
                    "owner-1",
                    uuid4(),
                    {
                        "kind": "update",
                        "resource_id": "manual",
                        "content": "changed",
                        "expected_version": 0,
                    },
                    connection=conn,
                )
                raise RuntimeError("actor completion failed")
    assert await resources.snapshot(run_id) == before
    with pytest.raises(WorldError, match="not found"):
        await resources.apply_request(
            run_id,
            "owner-2",
            uuid4(),
            {
                "kind": "share",
                "resource_id": "manual",
                "expected_version": 0,
                "visibility": "public",
            },
        )
    with pytest.raises(WorldError, match="owner can change"):
        await resources.apply_request(
            run_id,
            "owner-1",
            uuid4(),
            {
                "kind": "share",
                "resource_id": "manual",
                "expected_version": 0,
                "visibility": "public",
            },
            trusted=False,
        )


async def test_market_consent_rollback_and_identity_validation(environment, pool):
    run_id, resources, market, _, _ = await environment()
    before = await resources.snapshot(run_id)
    with pytest.raises(RuntimeError):
        async with pool.connection() as conn:
            async with conn.transaction():
                await market.set_consent(
                    run_id, "owner-2", 20, ["battery"], [], uuid4(), connection=conn
                )
                raise RuntimeError("actor completion failed")
    assert await resources.snapshot(run_id) == before
    for items, limits in [
        (["unknown"], []),
        ([], [{"resource_id": "battery", "quantity": 1, "min_unit_price": 0}]),
        (["manual"], []),
    ]:
        with pytest.raises(WorldError, match="unknown or unowned"):
            await market.set_consent(run_id, "owner-2", 0, items, limits, uuid4())
    with pytest.raises(WorldError, match="Invalid commerce consent"):
        await market.set_consent(run_id, "owner-2", 10, [], [], uuid4())


async def test_resource_revocation_hides_search_and_fetch(environment):
    run_id, resources, _, _, _ = await environment()
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {"kind": "share", "resource_id": "manual", "expected_version": 0, "visibility": "public"},
    )
    assert (await resources.search(run_id, "owner-2", "hidden"))["items"]
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {"kind": "share", "resource_id": "manual", "expected_version": 1, "visibility": "private"},
    )
    assert (await resources.search(run_id, "owner-2", "hidden"))["items"] == []
    with pytest.raises(WorldError, match="not found"):
        await resources.fetch(run_id, "owner-2", "manual")


async def test_expired_seller_listings_and_empty_settlement_are_safe(environment, pool):
    run_id, _, market, _, _ = await environment()
    await market.set_consent(
        run_id,
        "owner-1",
        0,
        [],
        [{"resource_id": "battery", "quantity": 1, "min_unit_price": 7}],
        uuid4(),
        expires_window=0,
    )
    await market.submit_current(
        run_id, "owner-1", uuid4(), "sell", resource_id="battery", unit_price=7
    )
    item = (await market.settle(run_id, 0, 23))[0]["listing_id"]
    await advance(pool, run_id, 1)
    await market.set_consent(run_id, "owner-2", 20, ["battery"], [], uuid4())
    assert (await market.view(run_id, "owner-2"))["listings"] == []
    await market.submit_current(run_id, "owner-2", uuid4(), "buy", listing_id=item)
    assert (await market.settle(run_id, 1, 23))[0]["reason"] == "seller_consent_expired"
    await advance(pool, run_id, 2)
    assert await market.settle(run_id, 2, 23) == []
    assert await market.settle(run_id, 2, 23) == []


async def test_actor_resource_view_has_bounded_content(environment):
    run_id, resources, _, _, _ = await environment()
    for i in range(35):
        await resources.create_document(run_id, "owner-2", uuid4(), str(i), "x" * 12000)
    view = await resources.actor_view(run_id, "owner-2")
    assert len(view["items"]) == 30 and view["truncated"]
    assert sum(len(r["content"]) for r in view["items"]) <= 24000
    assert any(r["content_truncated"] for r in view["items"])


async def test_agent_tools_use_http_and_replay_stable_requests(environment, app, pool):
    import httpx

    from agentrpg.environment_tools import EnvironmentTools

    run_id, resources, market, _, agents = await environment()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": f"Bearer {agents[0]['token']}"},
    ) as http:
        tools = EnvironmentTools(http, run_id)
        created = await tools.execute(
            "create_resource", {"title": "Shopping log", "content": "Draft"}, "create-doc"
        )
        assert created["ok"], created
        document = created["data"]
        assert (
            await tools.execute(
                "create_resource", {"title": "Shopping log", "content": "Draft"}, "create-doc"
            )
            == created
        )
        updated = await tools.execute(
            "write_resource",
            {"resource_id": document["id"], "content": "Contacted owner", "expected_version": 0},
            "write-doc",
        )
        assert updated["ok"] and updated["data"]["version"] == 1
        for name, args in [
            ("read_resources", {}),
            ("search_environment", {"query": "Shopping"}),
            ("fetch_resource", {"resource_id": document["id"]}),
            ("inspect_market", {}),
        ]:
            assert (await tools.execute(name, args, name))["ok"]
        await market.set_consent(
            run_id,
            "owner-1",
            0,
            [],
            [{"resource_id": "battery", "quantity": 1, "min_unit_price": 7}],
            uuid4(),
        )
        submitted = await tools.execute(
            "submit_market_action",
            {"kind": "sell", "resource_id": "battery", "unit_price": 7},
            "sell-one",
        )
        assert submitted["ok"] and submitted["data"]["status"] == "pending"
        action = submitted["data"]["id"]
        read = await tools.execute("read_market_action", {"action_id": action}, "read-one")
        assert read["ok"] and read["data"]["status"] == "pending"
        await market.settle(run_id, 0, 23)
        read = await tools.execute("read_market_action", {"action_id": action}, "read-two")
        assert read["data"]["status"] == "accepted"
        listing_id = read["data"]["listing_id"]
        await advance(pool, run_id, 1)
        repeated = await tools.execute(
            "submit_market_action",
            {"kind": "sell", "resource_id": "battery", "unit_price": 7},
            "sell-one",
        )
        assert repeated["data"] == read["data"]
        assert not (await tools.execute("set_commerce_consent", {}, "forged"))["ok"]
        assert not (
            await tools.execute(
                "create_resource",
                {"owner_id": "owner-2", "title": "Forged", "content": ""},
                "forged-owner",
            )
        )["ok"]
    await market.set_consent(run_id, "owner-2", 7, ["battery"], [], uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": f"Bearer {agents[1]['token']}"},
    ) as http:
        tools = EnvironmentTools(http, run_id)
        fetched = await tools.execute(
            "fetch_resource", {"resource_id": listing_id}, "fetch-listing"
        )
        assert fetched["ok"] and fetched["data"]["type"] == "listing"
        bought = await tools.execute(
            "submit_market_action", {"kind": "buy", "listing_id": listing_id}, "buy-one"
        )
        assert bought["ok"] and bought["data"]["status"] == "pending"
        await market.settle(run_id, 1, 23)
        final = await tools.execute(
            "read_market_action", {"action_id": bought["data"]["id"]}, "read-purchase"
        )
        assert final["data"]["status"] == "accepted"
        own = await resources.fetch(run_id, "owner-2", final["data"]["resource_id"])
        assert own["quantity"] == 1


async def test_actor_knows_visible_item_ids_without_peer_private_content(environment):
    import json

    run_id, resources, _, _, _ = await environment()
    assert "battery" not in (await resources.actor_view(run_id, "owner-2"))["visible_item_ids"]
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {"kind": "share", "resource_id": "battery", "expected_version": 0, "visibility": "public"},
    )
    view = await resources.actor_view(run_id, "owner-2")
    assert "battery" in view["visible_item_ids"]
    assert all(r["owner_id"] == "owner-2" for r in view["items"])
    assert "private inventory notes" not in json.dumps(view)
    for index in range(30):
        await resources.create_document(run_id, "owner-2", uuid4(), str(index), "🥷" * 12000)
    view = await resources.actor_view(run_id, "owner-2")
    assert len(json.dumps(view)) <= 24000
    metadata = await resources.view(run_id, "owner-2")
    assert all("content" not in r for r in metadata["items"])


async def test_shared_resource_updates_notify_only_permitted_owners(environment, pool):
    run_id, resources, _, _, _ = await environment(count=3)
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {
            "kind": "share",
            "resource_id": "manual",
            "expected_version": 0,
            "visibility": "shared",
            "shared_owner_ids": ["owner-2"],
        },
    )
    await resources.director_update(run_id, "owner-1", "manual", uuid4(), "New private detail", 1)
    async with pool.connection() as conn:
        events = await (
            await conn.execute(
                """SELECT owner_id,payload FROM run_events
            WHERE run_id=%s AND kind='resource_updated' AND payload->>'version'='2'""",
                (run_id,),
            )
        ).fetchall()
    assert {e["owner_id"] for e in events} == {"owner-1", "owner-2"}
    assert all("content" not in e["payload"] for e in events)


def assert_saved_payloads(rows, expected):
    assert len(rows) == len(expected)
    for row in rows:
        payload = expected[row["request_id"]]
        assert row["request_payload"] == payload
        assert (
            row["payload_hash"]
            == hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )


@pytest.mark.parametrize("trusted", [False, True])
async def test_resource_requests_keep_normalized_payloads(environment, pool, trusted):
    run_id, resources, _, _, _ = await environment()
    create_id, write_id, share_id = uuid4(), uuid4(), uuid4()
    create = {"kind": "create", "title": "  Café notes  ", "content": "Keep 👋 and spaces. "}
    made = await resources.apply_request(run_id, "owner-1", create_id, create, trusted=trusted)
    write = {
        "kind": "update",
        "resource_id": made["id"],
        "expected_version": 0,
        "title": "  Revised notes  ",
        "content": "Revised café.",
    }
    changed = await resources.apply_request(run_id, "owner-1", write_id, write, trusted=trusted)
    share = {
        "kind": "share",
        "resource_id": made["id"],
        "expected_version": 1,
        "visibility": "shared",
        "shared_owner_ids": ["owner-2"],
    }
    shared = await resources.apply_request(run_id, "owner-1", share_id, share)
    expected = {
        create_id: {
            "operation": {
                "kind": "create",
                "resource_id": None,
                "title": "Café notes",
                "content": "Keep 👋 and spaces. ",
                "expected_version": None,
                "visibility": None,
                "shared_owner_ids": [],
            },
            "trusted": trusted,
        },
        write_id: {
            "resource_id": made["id"],
            "content": "Revised café.",
            "expected_version": 0,
            "title": "Revised notes",
            "trusted": trusted,
        },
        share_id: {
            "operation": {
                "kind": "share",
                "resource_id": made["id"],
                "title": None,
                "content": None,
                "expected_version": 1,
                "visibility": "shared",
                "shared_owner_ids": ["owner-2"],
            },
            "trusted": True,
        },
    }
    assert (
        await resources.apply_request(run_id, "owner-1", create_id, create, trusted=trusted) == made
    )
    assert (
        await resources.apply_request(run_id, "owner-1", write_id, write, trusted=trusted)
        == changed
    )
    assert await resources.apply_request(run_id, "owner-1", share_id, share) == shared
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT * FROM resource_requests WHERE run_id=%s", (run_id,))
        ).fetchall()
    assert_saved_payloads(rows, expected)
    results = {row["request_id"]: row["result"] for row in rows}
    assert results == {create_id: made, write_id: changed, share_id: shared}


async def test_market_requests_keep_payloads_for_consent_trade_and_rejection(environment, pool):
    run_id, _, market, _, _ = await environment()
    seller_consent, seller_stale, seller_action = uuid4(), uuid4(), uuid4()
    buyer_consent, buyer_stale, buyer_action = uuid4(), uuid4(), uuid4()
    sell_limits = [{"resource_id": "battery", "quantity": 1, "min_unit_price": 7}]
    await market.set_consent(run_id, "owner-1", 0, [], sell_limits, seller_consent)
    stale_sell = await market.submit_current(
        run_id,
        "owner-1",
        seller_stale,
        "sell",
        resource_id="battery",
        unit_price=7,
        expected_version=99,
    )
    assert stale_sell["reason"] == "stale_version"
    await market.submit_current(
        run_id,
        "owner-1",
        seller_action,
        "sell",
        resource_id="battery",
        unit_price=7,
        expected_version=0,
    )
    sold = (await market.settle(run_id, 0, 23))[0]
    assert sold["status"] == "accepted"
    await advance(pool, run_id, 1)
    await market.set_consent(run_id, "owner-2", 7, ["battery"], [], buyer_consent)
    # The stored target uses the canonical UUID, even when input uses capitals.
    item = sold["listing_id"].upper()
    stale_buy = await market.submit_current(
        run_id,
        "owner-2",
        buyer_stale,
        "buy",
        listing_id=item,
        expected_version=99,
    )
    assert stale_buy["reason"] == "stale_version"
    await market.submit_current(
        run_id,
        "owner-2",
        buyer_action,
        "buy",
        listing_id=item,
        expected_version=0,
    )
    bought = (await market.settle(run_id, 1, 23))[0]
    assert bought["status"] == "accepted"
    expected = {
        seller_consent: {
            "kind": "consent",
            "buy_budget": 0,
            "buy_items": [],
            "sell_limits": sell_limits,
            "expires_window": None,
        },
        buyer_consent: {
            "kind": "consent",
            "buy_budget": 7,
            "buy_items": ["battery"],
            "sell_limits": [],
            "expires_window": None,
        },
    }
    for request_id, kind, target, price, version in [
        (seller_stale, "sell", "battery", 7, 99),
        (seller_action, "sell", "battery", 7, 0),
        (buyer_stale, "buy", sold["listing_id"], None, 99),
        (buyer_action, "buy", sold["listing_id"], None, 0),
    ]:
        expected[request_id] = {
            "kind": kind,
            "target_id": target,
            "quantity": 1,
            "unit_price": price,
            "expected_version": version,
        }
    assert (
        await market.submit_current(
            run_id, "owner-2", buyer_action, "buy", listing_id=item, expected_version=0
        )
        == bought
    )
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT * FROM market_requests WHERE run_id=%s", (run_id,))
        ).fetchall()
    assert_saved_payloads(rows, expected)
    results = {row["request_id"]: row["result"] for row in rows}
    assert results[seller_stale] == stale_sell
    assert results[seller_action] == sold
    assert results[buyer_stale] == stale_buy
    assert results[buyer_action] == bought
