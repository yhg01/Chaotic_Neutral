"""Real PostgreSQL shopping tasks, carts, order replay and settlement."""

import asyncio
from uuid import UUID, uuid4

import pytest

from agentrpg.director import Director, save_message
from agentrpg.market import MarketService
from agentrpg.shop import ShopService
from agentrpg.shop_demo import PRODUCTS, shopping_scenario
from agentrpg.shop_schema import ShoppingTaskRequest
from agentrpg.world import WorldError


async def ready_shop(pool, *, consent=True, quantity=1, task_quantity=1, supervision=None):
    director, shop, market = Director(pool), ShopService(pool), MarketService(pool)
    manifest = await director.create_run(shopping_scenario(3))
    run = UUID(manifest["run_id"])
    await shop.enable(run)
    if supervision is not None:
        from agentrpg.supervision import RuntimeSupervisor

        await RuntimeSupervisor(pool).bind(run, supervision)
    await director.control(run, "start")
    await market.set_consent(
        run,
        "bob",
        0,
        [],
        [
            {"resource_id": sku, "quantity": 5, "min_unit_price": price}
            for sku, _, price, _ in PRODUCTS
        ],
        uuid4(),
    )
    for sku, _, price, _ in PRODUCTS:
        await market.submit_current(
            run, "bob", uuid4(), "sell", resource_id=sku, quantity=quantity, unit_price=price
        )
    await market.settle(run, 0, 42)
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=1 WHERE id=%s", (run,))
        message = await save_message(conn, run, "alice", "owner", "请买陶瓷杯。", uuid4(), 1)
    if consent:
        await market.set_consent(run, "alice", 44, [p[0] for p in PRODUCTS], [], uuid4())
    task = await shop.owner_request(
        run,
        "alice",
        message["id"],
        uuid4(),
        ShoppingTaskRequest(
            kind="open",
            task_key="mug",
            description="Buy one mug",
            item_ids=[PRODUCTS[0][0]],
            quantity=task_quantity,
            max_total=24,
        ),
    )
    state = await shop.state(run, "alice")
    listing = next(p for p in state["catalog"] if p["origin_id"] == PRODUCTS[0][0])
    return manifest, run, task, listing


async def add(pool, run, task, listing, *, request_id=None, quantity=1):
    return await ShopService(pool).change_cart(
        run,
        "alice",
        request_id or uuid4(),
        action="set_quantity",
        task_id=UUID(str(task["id"])),
        listing_id=UUID(str(listing["id"])),
        quantity=quantity,
    )


async def test_browser_cart_checkout_uses_real_market_and_committed_receipt(pool):
    _, run, task, listing = await ready_shop(pool)
    state = await add(pool, run, task, listing)
    result = await ShopService(pool).checkout(run, "alice", uuid4(), state["cart"]["version"])
    pending = await ShopService(pool).state(run, "alice")
    assert pending["balance"] == 1000 and pending["orders"][0]["status"] == "pending"
    assert result["pending_ids"]
    await MarketService(pool).settle(run, 1, 42)
    await ShopService(pool).sync(run)
    final = await ShopService(pool).state(run, "alice")
    assert final["balance"] == 988 and final["orders"][0]["status"] == "accepted"
    assert final["orders"][0]["paid_total"] == 12 and final["tasks"][0]["status"] == "completed"
    assert (await ShopService(pool).state(run, "bob"))["balance"] == 112


async def test_concurrent_double_checkout_with_new_call_ids_still_creates_one_order(pool):
    _, run, task, listing = await ready_shop(pool)
    state = await add(pool, run, task, listing)
    replies = await asyncio.gather(
        *[
            ShopService(pool).checkout(run, "alice", uuid4(), state["cart"]["version"])
            for _ in range(6)
        ]
    )
    assert len({r["order_id"] for r in replies}) == 1
    assert len((await ShopService(pool).state(run, "alice"))["orders"]) == 1
    await MarketService(pool).settle(run, 1, 42)
    await ShopService(pool).sync(run)
    assert (await ShopService(pool).state(run, "alice"))["balance"] == 988


async def test_same_operation_replays_after_pause_and_conflicts_on_changed_payload(pool):
    _, run, task, listing = await ready_shop(pool)
    operation = uuid4()
    first = await add(pool, run, task, listing, request_id=operation)
    await Director(pool).control(run, "pause")
    assert await add(pool, run, task, listing, request_id=operation) == first
    with pytest.raises(WorldError, match="different arguments"):
        await add(pool, run, task, listing, request_id=operation, quantity=0)
    with pytest.raises(WorldError):
        await add(pool, run, task, listing)


async def test_task_alone_does_not_grant_purchase_authority(pool):
    _, run, task, listing = await ready_shop(pool, consent=False)
    cart = await add(pool, run, task, listing)
    with pytest.raises(WorldError, match="consent or funds"):
        await ShopService(pool).checkout(run, "alice", uuid4(), cart["cart"]["version"])
    assert not (await ShopService(pool).state(run, "alice"))["orders"]


async def test_task_quantity_cap_prevents_a_second_purchase_even_with_budget_left(pool):
    _, run, task, listing = await ready_shop(pool, quantity=4)
    cart = await add(pool, run, task, listing)
    await ShopService(pool).checkout(run, "alice", uuid4(), cart["cart"]["version"])
    with pytest.raises(WorldError, match="open shopping task"):
        await add(pool, run, task, listing)
    await MarketService(pool).settle(run, 1, 42)
    await ShopService(pool).sync(run)
    with pytest.raises(WorldError, match="open shopping task"):
        await add(pool, run, task, listing)


async def test_stale_quote_requires_a_new_review_before_order_submission(pool):
    _, run, task, listing = await ready_shop(pool)
    cart = await add(pool, run, task, listing)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE market_listings SET version=version+1 WHERE run_id=%s AND listing_id=%s",
            (run, listing["id"]),
        )
    with pytest.raises(WorldError, match="product changed"):
        await ShopService(pool).checkout(run, "alice", uuid4(), cart["cart"]["version"])
    assert not (await ShopService(pool).state(run, "alice"))["orders"]


async def test_foreign_owner_cannot_use_another_task_or_order(pool):
    _, run, task, listing = await ready_shop(pool)
    with pytest.raises(WorldError):
        await ShopService(pool).change_cart(
            run, "bob", uuid4(), action="select_task", task_id=task["id"]
        )
    assert not (await ShopService(pool).state(run, "bob"))["tasks"]
    assert not (await ShopService(pool).state(run, "bob"))["orders"]


async def test_only_attributed_owner_can_open_or_cancel_purchase_work(pool):
    _, run, task, _ = await ready_shop(pool)
    async with pool.connection() as conn:
        message = await save_message(conn, run, "bob", "owner", "Cancel Alice task.", uuid4(), 1)
    with pytest.raises(WorldError, match="own owner message"):
        await ShopService(pool).owner_request(
            run, "alice", message["id"], uuid4(), ShoppingTaskRequest(kind="cancel", task_key="mug")
        )
    assert (await ShopService(pool).state(run, "alice"))["tasks"][0]["status"] == "open"


async def test_shop_routes_keep_agent_scope_and_read_only_operator_access(client, pool):
    manifest, run, _, _ = await ready_shop(pool)
    member = next(m for m in manifest["participants"] if m["owner_id"] == "alice")
    headers = {"Authorization": "Bearer " + member["token"]}
    response = await client.get(f"/api/v1/runs/{run}/shop/state", headers=headers)
    assert response.status_code == 200 and response.json()["owner_id"] == "alice"
    assert (
        await client.get(f"/api/v1/observer/runs/{run}/shop", headers=headers)
    ).status_code == 403
    observer = await client.get(f"/api/v1/observer/runs/{run}/shop?owner=alice")
    assert observer.status_code == 200 and observer.json()["read_only"]
    assert (
        await client.post(
            f"/api/v1/runs/{run}/shop/checkout",
            json={"cart_version": 0},
            headers={"X-Shop-Operation-ID": str(uuid4())},
        )
    ).status_code == 403


@pytest.mark.parametrize("rounds", [1, 10, 30])
def test_continuous_shop_scenario_freezes_new_requests_within_existing_clock(rounds):
    spec = shopping_scenario(rounds)
    assert len(spec.beats) == rounds + 1 and spec.max_windows > rounds * 3
    assert spec.content_hash() == shopping_scenario(rounds).content_hash()
