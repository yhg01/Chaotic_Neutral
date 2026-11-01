"""Real PostgreSQL branch checks for domain state, receipts, and private access."""

import json
from uuid import UUID, uuid4, uuid5

import pytest
from psycopg import sql
from test_console_execution import create_run

from agentrpg.console_execution import save_private
from agentrpg.director import Director, fingerprint, row, rows, save_message
from agentrpg.inbox import InboxService
from agentrpg.market import MarketService
from agentrpg.resource_schema import ResourceSpec
from agentrpg.resources import ResourceService
from agentrpg.run_branches import capture_boundary
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.world import WorldError, WorldService


@pytest.fixture
def root(app, tmp_path):
    app.state.console_root = tmp_path
    return tmp_path


def authorization(manifest, index=0):
    return {"Authorization": "Bearer " + manifest["participants"][index]["token"]}


def branch_path(manifest, suffix="branches"):
    return f"/api/v1/console/runs/{manifest['run_id']}/{suffix}"


async def make_environment(pool, root, count=2):
    # Reuse the configured campaign fixture, then create a frozen resource plot.
    configured = await create_run(pool, root, count)
    spec = vehicle_scenario(count).model_copy(
        update={
            "resources": (
                ResourceSpec(
                    id="notes", owner_id="owner-1", title="Private notes", content="Initial."
                ),
                ResourceSpec(
                    id="battery",
                    owner_id="owner-1",
                    title="Spare batteries",
                    kind="item",
                    quantity=3,
                    content="Private stock note.",
                ),
            )
        }
    )
    manifest = await Director(pool).create_run(spec)
    manifest["configuration"] = configured["configuration"]
    save_private(root / ".local/scenario-runs" / manifest["run_id"] / "credentials.json", manifest)
    await Director(pool).control(UUID(manifest["run_id"]), "start")
    return manifest


async def save_boundary(pool, run_id, window=1):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET current_window=%s,phase='actors' WHERE id=%s",
            (window, run_id),
        )
    assert await capture_boundary(pool, run_id) == window


async def branch(client, root, parent, window=1):
    response = await client.post(
        branch_path(parent), json={"request_id": str(uuid4()), "window": window}
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "checkpoint"
    child = json.loads(
        (root / ".local/scenario-runs" / response.json()["run_id"] / "credentials.json").read_text()
    )
    assert child["configuration"]["budget"] == parent["configuration"]["budget"]
    return child


async def test_branch_restores_private_grants_versions_and_wallet_before_future_changes(
    client, pool, root
):
    parent = await make_environment(pool, root, 3)
    run_id = UUID(parent["run_id"])
    resources, world = ResourceService(pool), WorldService(pool)
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {
            "kind": "share",
            "resource_id": "notes",
            "expected_version": 0,
            "visibility": "shared",
            "shared_owner_ids": ["owner-2"],
        },
    )
    await resources.update(run_id, "owner-1", "notes", uuid4(), "Saved boundary text.", 1)
    await world.set_consent(
        run_id, "owner-2", 20, ["taxi-2"], uuid4(), allowed_kinds=["reserve", "cancel"]
    )
    await world.submit(run_id, "owner-2", 0, uuid4(), "reserve", "taxi-2", 0)
    assert (await world.settle(run_id, 0, vehicle_scenario().seed))[0]["status"] == "accepted"
    before = await world.snapshot(run_id)
    await save_boundary(pool, run_id)

    await resources.update(run_id, "owner-1", "notes", uuid4(), "Source future text.", 2)
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {"kind": "share", "resource_id": "notes", "expected_version": 3, "visibility": "public"},
    )
    await world.submit(run_id, "owner-2", 1, uuid4(), "cancel", "taxi-2")
    await world.settle(run_id, 1, vehicle_scenario().seed)
    child = await branch(client, root, parent)
    child_id = UUID(child["run_id"])
    assert await world.snapshot(child_id) == before
    assert world.replay(await world.journal(child_id)) == before
    saved = await resources.fetch(child_id, "owner-2", "notes")
    assert saved["content"] == "Saved boundary text."
    assert saved["version"] == 2 and saved["shared_owner_ids"] == ["owner-2"]
    with pytest.raises(WorldError, match="not found"):
        await resources.fetch(child_id, "owner-3", "notes")
    assert (await resources.fetch(run_id, "owner-3", "notes"))["content"] == "Source future text."
    assert (await world.view(run_id, "owner-2"))["account"]["balance"] == 20
    assert (await world.view(child_id, "owner-2"))["account"]["balance"] == 0


async def test_branch_trade_keeps_listing_stock_and_adds_to_the_same_copied_holding(
    client, pool, root
):
    parent = await make_environment(pool, root)
    run_id = UUID(parent["run_id"])
    market, resources, world = MarketService(pool), ResourceService(pool), WorldService(pool)
    await market.set_consent(
        run_id,
        "owner-1",
        0,
        [],
        [{"resource_id": "battery", "quantity": 3, "min_unit_price": 3}],
        uuid4(),
    )
    sell_id = uuid4()
    await market.submit_current(
        run_id, "owner-1", sell_id, "sell", resource_id="battery", quantity=3, unit_price=3
    )
    listing = (await market.settle(run_id, 0, vehicle_scenario().seed))[0]["listing_id"]
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=1 WHERE id=%s", (run_id,))
    await market.set_consent(run_id, "owner-2", 9, ["battery"], [], uuid4())
    buy_id = uuid4()
    await market.submit_current(run_id, "owner-2", buy_id, "buy", listing_id=listing)
    bought = (await market.settle(run_id, 1, vehicle_scenario().seed))[0]
    assert bought["status"] == "accepted"
    await save_boundary(pool, run_id, 2)
    child = await branch(client, root, parent, 2)
    child_id = UUID(child["run_id"])
    child_listing = str(uuid5(child_id, f"listing:owner-1:{sell_id}"))
    child_holding = str(uuid5(child_id, "holding:owner-2:battery"))
    saved_listing = await resources.fetch(child_id, "owner-2", child_listing)
    saved_holding = await resources.fetch(child_id, "owner-2", child_holding)
    assert saved_listing["quantity"] == 2 and saved_listing["version"] == 1
    assert saved_holding["quantity"] == 1 and saved_holding["version"] == 0
    replayed = await market.submit_current(
        child_id, "owner-2", buy_id, "buy", listing_id=child_listing
    )
    assert replayed["resource_id"] == child_holding and replayed["status"] == "accepted"
    await Director(pool).control(child_id, "resume")
    await market.submit_current(
        child_id, "owner-2", uuid4(), "buy", listing_id=child_listing, expected_version=1
    )
    next_purchase = (await market.settle(child_id, 2, vehicle_scenario().seed))[0]
    assert next_purchase["status"] == "accepted" and next_purchase["resource_id"] == child_holding
    holding = await resources.fetch(child_id, "owner-2", child_holding)
    assert holding["quantity"] == 2 and holding["version"] == 1
    owned = (await resources.view(child_id, "owner-2"))["items"]
    assert len([item for item in owned if item["kind"] == "item"]) == 1
    assert (await world.view(child_id, "owner-2"))["account"]["balance"] == 14
    assert (await world.view(run_id, "owner-2"))["account"]["balance"] == 17
    assert (await resources.fetch(run_id, "owner-2", bought["resource_id"]))["quantity"] == 1


async def test_branch_board_mail_receipts_and_credentials_are_independent(client, pool, root):
    parent = await make_environment(pool, root)
    run_id = UUID(parent["run_id"])
    board_path = f"/api/v1/clusters/{parent['cluster_id']}"
    post_id, reply_id, mail_id = uuid4(), uuid4(), uuid4()
    post = {"request_id": str(post_id), "title": "Saved plan", "body": "Original board text."}
    posted = await client.post(board_path + "/threads", json=post, headers=authorization(parent))
    assert posted.status_code == 201, posted.text
    thread = posted.json()["id"]
    replied = await client.post(
        board_path + f"/threads/{thread}/replies",
        json={"request_id": str(reply_id), "body": "Read the plan."},
        headers=authorization(parent, 1),
    )
    assert replied.status_code == 201
    acknowledged = await client.post(
        board_path + "/acknowledgements",
        json={"message_ids": [thread]},
        headers=authorization(parent, 1),
    )
    assert acknowledged.status_code == 200
    inbox = InboxService(pool)
    mail = await inbox.send(
        run_id,
        "owner-1",
        "owner-2",
        "Private plan",
        f"See thread {thread}",
        mail_id,
    )
    await inbox.send(
        run_id,
        "owner-2",
        "owner-1",
        "Reply",
        "Received.",
        uuid4(),
        in_reply_to=mail["id"],
    )
    await inbox.acknowledge(run_id, "owner-2", [mail["id"]])
    await save_boundary(pool, run_id)
    await inbox.send(run_id, "owner-1", "owner-2", "Future", "Do not copy.", uuid4())
    await client.post(
        board_path + "/threads",
        json={"request_id": str(uuid4()), "title": "Future", "body": "Do not copy."},
        headers=authorization(parent),
    )
    child = await branch(client, root, parent)
    child_id = UUID(child["run_id"])
    child_board = f"/api/v1/clusters/{child['cluster_id']}"
    roots = await client.get(child_board + "/threads", headers=authorization(child, 1))
    assert len(roots.json()["items"]) == 1
    copied_thread = roots.json()["items"][0]["id"]
    assert copied_thread != thread
    detail = await client.get(
        child_board + f"/threads/{copied_thread}", headers=authorization(child, 1)
    )
    assert len(detail.json()["replies"]) == 1
    assert detail.json()["replies"][0]["thread_id"] == copied_thread
    updates = await client.get(child_board + "/updates", headers=authorization(child, 1))
    assert copied_thread not in {item["id"] for item in updates.json()["items"]}
    copied_mail = (await inbox.list_messages(child_id, "owner-2"))["items"]
    assert len(copied_mail) == 1 and copied_mail[0]["read_at"] is not None
    assert copied_mail[0]["body"] == f"See thread {copied_thread}"
    assert not (await inbox.list_messages(child_id, "owner-2", unread_only=True))["items"]
    replies = (await inbox.list_messages(child_id, "owner-1"))["items"]
    assert replies[0]["in_reply_to"] == copied_mail[0]["id"]
    retried = await client.post(child_board + "/threads", json=post, headers=authorization(child))
    assert retried.status_code == 200 and retried.json()["id"] == copied_thread
    retry_mail = await inbox.send(
        child_id,
        "owner-1",
        "owner-2",
        "Private plan",
        f"See thread {copied_thread}",
        mail_id,
    )
    assert retry_mail["id"] == copied_mail[0]["id"]
    for target, credential in ((child, parent), (parent, child)):
        for route in (
            f"/api/v1/clusters/{target['cluster_id']}/threads",
            f"/api/v1/runs/{target['run_id']}/view",
        ):
            assert (await client.get(route, headers=authorization(credential))).status_code == 404
    async with pool.connection() as conn:
        receipts = await rows(
            conn, "SELECT * FROM message_receipts WHERE cluster_id=%s", (child["cluster_id"],)
        )
        assert len(receipts) == 1
        assert str(receipts[0]["agent_id"]) == child["participants"][1]["agent_id"]


async def test_branch_derived_requests_retry_with_remapped_ids_and_hashes(client, pool, root):
    parent = await make_environment(pool, root)
    run_id = UUID(parent["run_id"])
    actor_job = uuid5(run_id, "actor:owner-1:0")
    async with pool.connection() as conn:
        await conn.execute(
            'INSERT INTO run_jobs(id,run_id,owner_id,"window",kind,input,input_hash,status) '
            "VALUES(%s,%s,'owner-1',0,'actor','{}',%s,'completed')",
            (actor_job, run_id, fingerprint({})),
        )
        await save_message(conn, run_id, "owner-1", "owner", "Remember this plan.", actor_job, 0)
    resources, world = ResourceService(pool), WorldService(pool)
    created = await resources.apply_request(
        run_id,
        "owner-1",
        uuid5(actor_job, "resource:0"),
        {"kind": "create", "title": "Café plan", "content": "First version."},
    )
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid5(actor_job, "resource:1"),
        {
            "kind": "update",
            "resource_id": created["id"],
            "expected_version": 0,
            "content": f"Written during {actor_job}.",
        },
    )
    await resources.apply_request(
        run_id,
        "owner-1",
        uuid5(actor_job, "resource:2"),
        {
            "kind": "share",
            "resource_id": created["id"],
            "expected_version": 1,
            "visibility": "shared",
            "shared_owner_ids": ["owner-2"],
        },
    )
    consent = await world.set_consent(
        run_id, "owner-1", 0, ["shared-car"], uuid5(actor_job, "consent"), allowed_kinds=["cancel"]
    )
    action_id = uuid4()
    await world.submit(run_id, "owner-1", 0, action_id, "cancel", "shared-car")
    settled = (await world.settle(run_id, 0, vehicle_scenario().seed))[0]
    await save_boundary(pool, run_id)
    child = await branch(client, root, parent)
    child_id = UUID(child["run_id"])
    child_job = uuid5(child_id, "actor:owner-1:0")
    child_create = uuid5(child_job, "resource:0")
    child_document = str(uuid5(child_id, f"document:owner-1:{child_create}"))
    assert (
        await resources.apply_request(
            child_id,
            "owner-1",
            child_create,
            {"kind": "create", "title": "Café plan", "content": "First version."},
        )
    )["id"] == child_document
    updated = await resources.apply_request(
        child_id,
        "owner-1",
        uuid5(child_job, "resource:1"),
        {
            "kind": "update",
            "resource_id": child_document,
            "expected_version": 0,
            "content": f"Written during {child_job}.",
        },
    )
    assert updated["version"] == 1
    shared = await resources.apply_request(
        child_id,
        "owner-1",
        uuid5(child_job, "resource:2"),
        {
            "kind": "share",
            "resource_id": child_document,
            "expected_version": 1,
            "visibility": "shared",
            "shared_owner_ids": ["owner-2"],
        },
    )
    assert shared["version"] == 2
    repeated_consent = await world.set_consent(
        child_id,
        "owner-1",
        0,
        ["shared-car"],
        uuid5(child_job, "consent"),
        allowed_kinds=["cancel"],
    )
    assert repeated_consent == {**consent, "id": str(uuid5(child_job, "consent"))}
    assert await world.submit(child_id, "owner-1", 0, action_id, "cancel", "shared-car") == settled
    async with pool.connection() as conn:
        repeated_message = await save_message(
            conn, child_id, "owner-1", "owner", "Remember this plan.", child_job, 0
        )
        assert repeated_message["id"] == uuid5(child_id, f"message:owner-1:owner:{child_job}")
    with pytest.raises(WorldError, match="different payload"):
        await resources.apply_request(
            child_id,
            "owner-1",
            child_create,
            {"kind": "create", "title": "Changed", "content": "First version."},
        )


async def test_branch_resource_journal_replay_keeps_canonical_order_after_id_mapping(
    client, pool, root
):
    parent = await make_environment(pool, root)
    run_id = UUID(parent["run_id"])
    requests = [uuid4(), uuid4()]
    labels = [f"document:owner-1:{request}" for request in requests]
    old_ids = [str(uuid5(run_id, label)) for label in labels]
    # Select a branch namespace that reverses the two document IDs. This makes
    # the ordering regression deterministic instead of dependent on random IDs.
    for _ in range(100):
        branch_request = uuid4()
        child_id = uuid5(run_id, f"branch:{branch_request}")
        new_ids = [str(uuid5(child_id, label)) for label in labels]
        if (old_ids[0] < old_ids[1]) != (new_ids[0] < new_ids[1]):
            break
    else:
        pytest.fail("Could not construct the reversed document ordering fixture.")
    resources = ResourceService(pool)
    for index, request in enumerate(requests):
        await resources.create_document(run_id, "owner-1", request, f"Note {index}", "Saved.")
    await save_boundary(pool, run_id)
    response = await client.post(
        branch_path(parent), json={"request_id": str(branch_request), "window": 1}
    )
    assert response.status_code == 200, response.text
    assert resources.replay(await resources.journal(child_id)) == await resources.snapshot(child_id)


@pytest.mark.parametrize("table", ["world_requests", "resource_requests", "market_requests"])
async def test_branch_rejects_legacy_request_payload_without_partial_child(
    client, pool, root, table
):
    parent = await make_environment(pool, root)
    run_id = UUID(parent["run_id"])
    if table == "world_requests":
        await WorldService(pool).set_consent(run_id, "owner-1", 0, [], uuid4())
    elif table == "resource_requests":
        await ResourceService(pool).create_document(run_id, "owner-1", uuid4(), "Notes", "Old")
    else:
        await MarketService(pool).set_consent(run_id, "owner-1", 0, [], [], uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("UPDATE {} SET request_payload=NULL WHERE run_id=%s").format(
                sql.Identifier(table)
            ),
            (run_id,),
        )
    await save_boundary(pool, run_id)
    availability = (await client.get(branch_path(parent, "boundaries"))).json()["items"][1]
    assert not availability["available"] and "input payloads" in availability["reason"]
    request_id = uuid4()
    response = await client.post(
        branch_path(parent), json={"request_id": str(request_id), "window": 1}
    )
    assert response.status_code == 409 and "input payloads" in response.json()["detail"]
    child_id = uuid5(run_id, f"branch:{request_id}")
    async with pool.connection() as conn:
        assert await row(conn, "SELECT id FROM environment_runs WHERE id=%s", (child_id,)) is None
        assert (
            await row(
                conn, "SELECT child_run_id FROM run_branches WHERE parent_run_id=%s", (run_id,)
            )
            is None
        )
    assert not (root / ".local/scenario-runs" / str(child_id) / "credentials.json").exists()
