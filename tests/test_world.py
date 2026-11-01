"""Reservation, permission, concurrency, and replay checks against PostgreSQL."""

import asyncio
import copy
import hashlib
import json
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agentrpg.world import WorldError, WorldService


@pytest.fixture
def make_world(pool, provision):
    async def create(count=2, *, assets=None, credits=20, seed=23):
        cluster, agents = await provision(name=f"World {uuid4()}", count=count)
        owners = [f"owner-{index + 1}" for index in range(count)]
        spec = {
            "owners": [{"id": owner, "name": owner} for owner in owners],
            "world": {
                "assets": assets or [{"id": "car", "name": "Car", "price": 10, "capacity": 1}],
                "balances": dict.fromkeys(owners, credits),
            },
        }
        run_id = uuid4()
        async with pool.connection() as connection:
            await connection.execute(
                """INSERT INTO environment_runs(id, cluster_id, scenario, scenario_hash, status,
                           seed, max_windows) VALUES (%s, %s, %s, 'test', 'running', %s, 5)""",
                (run_id, cluster["id"], Jsonb(spec), seed),
            )
            for owner, agent in zip(owners, agents, strict=True):
                await connection.execute(
                    """INSERT INTO run_participants(run_id, owner_id, agent_id, session_id)
                       VALUES (%s, %s, %s, %s)""",
                    (run_id, owner, agent["agent"]["id"], uuid4()),
                )
        world = WorldService(pool)
        await world.initialize(run_id, spec)
        return world, run_id, spec

    return create


async def grant(world, run_id, owner="owner-1", budget=20, assets=None, **kwargs):
    return await world.set_consent(run_id, owner, budget, assets or ["car"], uuid4(), **kwargs)


async def advance(pool, run_id, window):
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE environment_runs SET current_window = %s WHERE id = %s", (window, run_id)
        )


async def test_initialization_is_idempotent_and_private(make_world):
    world, run_id, spec = await make_world()
    initial = await world.snapshot(run_id)
    assert await world.initialize(run_id, spec) == initial
    public = await world.view(run_id, "owner-1")
    assert public["account"]["owner_id"] == "owner-1"
    assert public["account"]["balance"] == 20
    assert "accounts" not in public and "treasury" not in public
    assert public["account"]["consent_assets"] == []
    changed = copy.deepcopy(spec)
    changed["world"]["balances"]["owner-1"] = 200
    with pytest.raises(WorldError, match="different specification"):
        await world.initialize(run_id, changed)
    with pytest.raises(WorldError, match="Owner not found"):
        await world.view(run_id, "outsider")


async def test_reservation_is_pending_then_atomic_charge_and_replay(make_world, pool):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    request = uuid4()
    receipt = await world.submit(run_id, "owner-1", 0, request, "reserve", "car", 0)
    assert receipt["status"] == "pending"
    assert (await world.snapshot(run_id))["reservations"] == []
    results = await world.settle(run_id, 0, 23)
    assert results[0]["status"] == "accepted"
    assert results[0]["amount"] == -10
    assert results[0]["balance"] == 10
    assert await world.receipt(run_id, "owner-1", request) == results[0]
    snapshot = await world.snapshot(run_id)
    assert snapshot["treasury"] == 10
    assert snapshot["reservations"] == [{"asset_id": "car", "owner_id": "owner-1", "paid": 10}]
    assert world.replay(await world.journal(run_id)) == snapshot
    async with pool.connection() as connection:
        ledger = await (await connection.execute("SELECT * FROM world_ledger")).fetchall()
        events = await (
            await connection.execute(
                "SELECT kind, owner_id, payload FROM run_events WHERE run_id = %s", (run_id,)
            )
        ).fetchall()
    assert len(ledger) == 1
    assert ledger[0]["amount"] == -10
    public_events = [e for e in events if e["owner_id"] is None]
    assert len(public_events) == 1
    assert "balance" not in str(public_events)


async def test_fifteen_competitors_have_one_winner_and_no_spam_advantage(make_world):
    winners = []
    for reverse in [False, True]:
        world, run_id, _ = await make_world(15)
        owners = [f"owner-{i + 1}" for i in range(15)]
        await asyncio.gather(*(grant(world, run_id, owner) for owner in owners))
        if reverse:
            owners.reverse()
        pending = await asyncio.gather(
            *(world.submit(run_id, owner, 0, uuid4(), "reserve", "car") for owner in owners)
        )
        assert all(p["status"] == "pending" for p in pending)
        with pytest.raises(WorldError, match="one intent"):
            await world.submit(run_id, owners[0], 0, uuid4(), "reserve", "car")
        results = await world.settle(run_id, 0, 23)
        accepted = [r for r in results if r["status"] == "accepted"]
        assert len(accepted) == 1
        assert sum(r["reason"] == "capacity_unavailable" for r in results) == 14
        winners.append(accepted[0]["owner_id"])
        state = await world.snapshot(run_id)
        assert sum(a["balance"] for a in state["accounts"]) + state["treasury"] == 300
    assert winners[0] == winners[1]


async def test_concurrent_retries_have_one_receipt_and_settlement(make_world, pool):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    request = uuid4()
    results = await asyncio.gather(
        *(world.submit(run_id, "owner-1", 0, request, "reserve", "car") for _ in range(12))
    )
    assert all(result == results[0] for result in results)
    with pytest.raises(WorldError, match="different payload"):
        await world.submit(run_id, "owner-1", 0, request, "cancel", "car")
    settlements = await asyncio.gather(*(world.settle(run_id, 0, 23) for _ in range(5)))
    assert all(result == settlements[0] for result in settlements)
    async with pool.connection() as connection:
        row = await (
            await connection.execute("SELECT count(*) AS count FROM world_ledger")
        ).fetchone()
    assert row["count"] == 1


async def test_completed_retries_work_after_terminal_state_and_new_calls_fail(make_world, pool):
    world, run_id, _ = await make_world()
    consent_id, request = uuid4(), uuid4()
    consent = await world.set_consent(run_id, "owner-1", 20, ["car"], consent_id)
    await world.submit(run_id, "owner-1", 0, request, "reserve", "car")
    results = await world.settle(run_id, 0, 23)
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE environment_runs SET status = 'completed', current_window = 4 WHERE id = %s",
            (run_id,),
        )
    assert await world.submit(run_id, "owner-1", 0, request, "reserve", "car") == results[0]
    assert await world.receipt(run_id, "owner-1", request) == results[0]
    assert await world.settle(run_id, 0, 23) == results
    assert await world.set_consent(run_id, "owner-1", 20, ["car"], consent_id) == consent
    with pytest.raises(WorldError, match="not running"):
        await world.submit(run_id, "owner-1", 4, uuid4(), "cancel", "car")
    with pytest.raises(WorldError, match="not running"):
        await grant(world, run_id)


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "consent_required"),
        ("revoked", "consent_changed"),
        ("expired", "consent_expired"),
        ("budget", "consent_budget_exceeded"),
        ("balance", "insufficient_balance"),
        ("stale", "stale_asset_version"),
    ],
)
async def test_settlement_checks_current_authority_and_state(make_world, pool, mode, reason):
    world, run_id, _ = await make_world(credits=5 if mode == "balance" else 20)
    window = 1 if mode == "expired" else 0
    if mode != "missing":
        await grant(world, run_id, budget=5 if mode == "budget" else 20, expires_window=0)
    if window:
        await advance(pool, run_id, window)
    await world.submit(
        run_id, "owner-1", window, uuid4(), "reserve", "car", 5 if mode == "stale" else None
    )
    if mode == "revoked":
        await world.set_consent(run_id, "owner-1", 0, [], uuid4())
    result = (await world.settle(run_id, window, 23))[0]
    assert result["status"] == "rejected" and result["reason"] == reason
    assert (await world.snapshot(run_id))["reservations"] == []


async def test_free_asset_also_needs_owner_consent(make_world):
    world, run_id, _ = await make_world(assets=[{"id": "car", "price": 0}])
    await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "car")
    assert (await world.settle(run_id, 0, 23))[0]["reason"] == "consent_required"


async def test_cancel_only_owned_reservation_then_refund_once(make_world, pool):
    world, run_id, _ = await make_world()
    await grant(world, run_id, allowed_kinds=["reserve", "cancel"])
    await grant(world, run_id, "owner-2", allowed_kinds=["reserve", "cancel"])
    await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "car")
    await world.settle(run_id, 0, 23)
    await advance(pool, run_id, 1)
    request = uuid4()
    await world.submit(run_id, "owner-2", 1, uuid4(), "cancel", "car")
    await world.submit(run_id, "owner-1", 1, request, "cancel", "car")
    results = await world.settle(run_id, 1, 23)
    assert (
        next(r for r in results if r["owner_id"] == "owner-2")["reason"] == "no_owned_reservation"
    )
    own = next(r for r in results if r["owner_id"] == "owner-1")
    assert own["status"] == "accepted" and own["amount"] == 10
    assert await world.settle(run_id, 1, 23) == results
    state = await world.snapshot(run_id)
    assert state["treasury"] == 0
    assert state["reservations"] == []
    assert state["accounts"][0]["balance"] == 20
    assert state["accounts"][0]["consent_budget"] == 10
    assert world.replay(await world.journal(run_id)) == state


async def test_release_can_enable_peer_reservation_in_same_window(make_world):
    world, run_id, _ = await make_world(
        assets=[
            {
                "id": "car",
                "price": 0,
                "initial_holder": "owner-1",
            }
        ]
    )
    await grant(world, run_id, allowed_kinds=["cancel"])
    await grant(world, run_id, "owner-2")
    # Arrival order has no effect: cancellation is the declared first phase.
    await world.submit(run_id, "owner-2", 0, uuid4(), "reserve", "car", 0)
    await world.submit(run_id, "owner-1", 0, uuid4(), "cancel", "car", 0)
    results = await world.settle(run_id, 0, 23)
    assert all(r["status"] == "accepted" for r in results)
    state = await world.snapshot(run_id)
    assert state["reservations"] == [{"asset_id": "car", "owner_id": "owner-2", "paid": 0}]
    assert state["assets"][0]["version"] == 2


async def test_reservation_consent_does_not_authorize_cancellation(make_world, pool):
    world, run_id, _ = await make_world(
        assets=[{"id": "car", "price": 0, "initial_holder": "owner-1"}]
    )
    await grant(world, run_id)
    await world.submit(run_id, "owner-1", 0, uuid4(), "cancel", "car")
    rejected = (await world.settle(run_id, 0, 23))[0]
    assert rejected["status"] == "rejected" and rejected["reason"] == "action_not_authorized"
    assert len((await world.snapshot(run_id))["reservations"]) == 1
    await advance(pool, run_id, 1)
    await grant(world, run_id, budget=0, allowed_kinds=["cancel"])
    await world.submit(run_id, "owner-1", 1, uuid4(), "cancel", "car")
    assert (await world.settle(run_id, 1, 23))[0]["status"] == "accepted"
    assert (await world.snapshot(run_id))["reservations"] == []


async def test_cancellation_consent_does_not_authorize_reservation(make_world):
    world, run_id, _ = await make_world()
    await grant(world, run_id, allowed_kinds=["cancel"])
    await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "car")
    assert (await world.settle(run_id, 0, 23))[0]["reason"] == "action_not_authorized"


async def test_withdrawal_clears_assets_and_action_kinds(make_world):
    world, run_id, _ = await make_world()
    await grant(world, run_id, allowed_kinds=["reserve", "cancel"])
    await world.set_consent(run_id, "owner-1", 0, [], uuid4())
    account = (await world.view(run_id, "owner-1"))["account"]
    assert account["consent_assets"] == account["consent_kinds"] == []
    assert account["consent_budget"] == 0


async def test_replay_normalizes_legacy_reservation_grants(make_world):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    journal = copy.deepcopy(await world.journal(run_id))
    for account in journal[0]["payload"]["accounts"]:
        account.pop("consent_kinds")
    journal[1]["payload"]["account"].pop("consent_kinds")
    assert world.replay(journal) == await world.snapshot(run_id)


@pytest.mark.parametrize("kinds", [[], ["transfer"], [["reserve"]], "reserve"])
async def test_invalid_consent_action_kinds_are_rejected(make_world, kinds):
    world, run_id, _ = await make_world()
    with pytest.raises(WorldError):
        await grant(world, run_id, allowed_kinds=kinds)


async def test_one_budget_limits_multiple_assets_in_same_window(make_world):
    world, run_id, _ = await make_world(
        assets=[
            {"id": "car", "price": 10},
            {"id": "taxi", "price": 10},
        ]
    )
    await grant(world, run_id, budget=10, assets=["car", "taxi"])
    await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "taxi")
    await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "car")
    result = await world.settle(run_id, 0, 23)
    assert [r["status"] for r in result] == ["accepted", "rejected"]
    assert result[1]["reason"] == "consent_budget_exceeded"
    assert len((await world.snapshot(run_id))["reservations"]) == 1


async def test_settlement_and_event_failure_roll_back_together(make_world, pool):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    request = uuid4()
    await world.submit(run_id, "owner-1", 0, request, "reserve", "car")
    before = await world.snapshot(run_id)
    async with pool.connection() as connection:
        await connection.execute("""
            CREATE FUNCTION reject_settlement() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.kind = 'reservation_settled' THEN
                    RAISE EXCEPTION 'test event failure';
                END IF;
                RETURN NEW;
            END $$
        """)
        await connection.execute("""
            CREATE TRIGGER reject_settlement BEFORE INSERT ON run_events
            FOR EACH ROW EXECUTE FUNCTION reject_settlement()
        """)
    with pytest.raises(psycopg.Error):
        await world.settle(run_id, 0, 23)
    assert await world.snapshot(run_id) == before
    assert (await world.receipt(run_id, "owner-1", request))["status"] == "pending"
    async with pool.connection() as connection:
        row = await (
            await connection.execute("SELECT count(*) AS count FROM world_ledger")
        ).fetchone()
        await connection.execute("DROP TRIGGER reject_settlement ON run_events")
    assert row["count"] == 0
    assert (await world.settle(run_id, 0, 23))[0]["status"] == "accepted"


async def test_empty_window_is_closed_and_idempotent(make_world, pool):
    world, run_id, _ = await make_world()
    assert await world.settle(run_id, 0, 23) == []
    assert await world.settle(run_id, 0, 23) == []
    with pytest.raises(WorldError, match="already settled"):
        await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "car")
    with pytest.raises(WorldError, match="different seed"):
        await world.settle(run_id, 0, 24)
    await advance(pool, run_id, 1)
    with pytest.raises(WorldError, match="not current"):
        await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "car")
    with pytest.raises(WorldError, match="frozen run seed"):
        await world.settle(run_id, 1, 24)


async def test_world_state_and_pending_receipts_survive_service_recreation(make_world, pool):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    request = uuid4()
    pending = await world.submit(run_id, "owner-1", 0, request, "reserve", "car")
    restored = WorldService(pool)
    assert await restored.receipt(run_id, "owner-1", request) == pending
    result = (await restored.settle(run_id, 0, 23))[0]
    assert result["status"] == "accepted"
    assert await world.receipt(run_id, "owner-1", request) == result
    assert restored.replay(await restored.journal(run_id)) == await world.snapshot(run_id)


async def test_declared_capacity_is_enforced_under_concurrent_submissions(make_world):
    world, run_id, _ = await make_world(5, assets=[{"id": "car", "price": 10, "capacity": 3}])
    owners = [f"owner-{index + 1}" for index in range(5)]
    await asyncio.gather(*(grant(world, run_id, owner) for owner in owners))
    await asyncio.gather(
        *(world.submit(run_id, owner, 0, uuid4(), "reserve", "car") for owner in owners)
    )
    results = await world.settle(run_id, 0, 23)
    assert sum(r["status"] == "accepted" for r in results) == 3
    snapshot = await world.snapshot(run_id)
    assert len(snapshot["reservations"]) == 3
    assert snapshot["treasury"] == 30


async def test_paused_run_fences_new_effects_but_preserves_pending_receipts(make_world, pool):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    request = uuid4()
    pending = await world.submit(run_id, "owner-1", 0, request, "reserve", "car")
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE environment_runs SET status = 'paused' WHERE id = %s", (run_id,)
        )
    assert await world.receipt(run_id, "owner-1", request) == pending
    assert await world.submit(run_id, "owner-1", 0, request, "reserve", "car") == pending
    with pytest.raises(WorldError, match="not running"):
        await world.settle(run_id, 0, 23)
    with pytest.raises(WorldError, match="not running"):
        await world.submit(run_id, "owner-2", 0, uuid4(), "reserve", "car")
    assert (await world.snapshot(run_id))["reservations"] == []


async def test_current_window_retry_preserves_original_payload_after_time_advances(
    make_world, pool
):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    request = uuid4()
    await world.submit_current(run_id, "owner-1", request, "reserve", "car", 0)
    result = (await world.settle(run_id, 0, 23))[0]
    await advance(pool, run_id, 1)
    assert await world.submit_current(run_id, "owner-1", request, "reserve", "car", 0) == result
    for kind, asset_id, version in [
        ("cancel", "car", 0),
        ("reserve", "car", 1),
        ("reserve", "unknown", 0),
    ]:
        with pytest.raises(WorldError, match="different payload"):
            await world.submit_current(run_id, "owner-1", request, kind, asset_id, version)
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE environment_runs SET status = 'completed' WHERE id = %s", (run_id,)
        )
    assert await world.submit_current(run_id, "owner-1", request, "reserve", "car", 0) == result


async def test_consent_can_share_job_transaction_and_roll_back(make_world, pool):
    world, run_id, _ = await make_world()
    request = uuid4()
    with pytest.raises(RuntimeError, match="job completion failed"):
        async with pool.connection() as connection:
            async with connection.transaction():
                await world.set_consent(
                    run_id, "owner-1", 20, ["car"], request, connection=connection
                )
                raise RuntimeError("job completion failed")
    assert (await world.view(run_id, "owner-1"))["account"]["consent_version"] == 0
    with pytest.raises(WorldError, match="Receipt not found"):
        await world.receipt(run_id, "owner-1", request)
    assert len(await world.journal(run_id)) == 1


async def test_snapshot_holds_consistent_lock_with_autocommit_pool(
    make_world, test_database_url, monkeypatch
):
    world, run_id, _ = await make_world()
    async with AsyncConnectionPool(
        test_database_url, kwargs={"autocommit": True, "row_factory": dict_row}, open=False
    ) as auto_pool:
        runtime_world = WorldService(auto_pool)
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime_world._snapshot

        async def paused_snapshot(connection, identifier):
            entered.set()
            await release.wait()
            return await original(connection, identifier)

        monkeypatch.setattr(runtime_world, "_snapshot", paused_snapshot)
        reader = asyncio.create_task(runtime_world.snapshot(run_id))
        await asyncio.wait_for(entered.wait(), 2)
        writer = asyncio.create_task(grant(world, run_id))
        try:
            await asyncio.sleep(0.05)
            assert not writer.done(), "The snapshot released its run lock before reading state."
        finally:
            release.set()
        before, _ = await asyncio.gather(reader, writer)
        assert before["accounts"][0]["consent_version"] == 0
        assert (await world.snapshot(run_id))["accounts"][0]["consent_version"] == 1


async def test_identity_scopes_receipts_and_request_id_namespaces(make_world):
    world, run_id, _ = await make_world()
    request = uuid4()
    await world.set_consent(run_id, "owner-1", 20, ["car"], request)
    with pytest.raises(WorldError, match="different payload"):
        await world.submit(run_id, "owner-1", 0, request, "reserve", "car")
    with pytest.raises(WorldError, match="Receipt not found"):
        await world.receipt(run_id, "owner-2", request)
    _, other_run_id, _ = await make_world()
    with pytest.raises(WorldError, match="Receipt not found"):
        await world.receipt(other_run_id, "owner-1", request)


@pytest.mark.parametrize("budget", [-1, 1.5, True, 1_000_000_001])
async def test_reject_invalid_consent_budget(make_world, budget):
    world, run_id, _ = await make_world()
    with pytest.raises(WorldError, match="Budget must be an integer"):
        await grant(world, run_id, budget=budget)


async def test_consent_unknown_assets_and_intent_validation(make_world):
    world, run_id, _ = await make_world()
    with pytest.raises(WorldError, match="unknown asset"):
        await grant(world, run_id, assets=["unknown"])
    with pytest.raises(WorldError, match="Asset not found"):
        await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "unknown")
    with pytest.raises(WorldError, match="reserve or cancel"):
        await world.submit(run_id, "owner-1", 0, uuid4(), "steal", "car")
    with pytest.raises(WorldError, match="Expected version"):
        await world.submit(run_id, "owner-1", 0, uuid4(), "reserve", "car", -1)


async def test_replay_rejects_missing_or_duplicate_records(make_world):
    world, run_id, _ = await make_world()
    await grant(world, run_id)
    journal = await world.journal(run_id)
    with pytest.raises(WorldError, match="initial world"):
        world.replay(journal[1:])
    with pytest.raises(WorldError, match="missing or repeated"):
        world.replay([*journal, journal[-1]])


async def test_world_requests_keep_normalized_payload_after_settlement(make_world, pool):
    world, run_id, _ = await make_world()
    consent_id, action_id = uuid4(), uuid4()
    consent = await world.set_consent(
        run_id,
        "owner-1",
        20,
        ["car", "car"],
        consent_id,
        allowed_kinds=["reserve", "cancel", "reserve"],
    )
    await world.submit(run_id, "owner-1", 0, action_id, "reserve", "car", 0)
    settled = (await world.settle(run_id, 0, 23))[0]
    expected = {
        consent_id: {
            "kind": "consent",
            "budget": 20,
            "assets": ["car"],
            "expires_window": None,
            "allowed_kinds": ["cancel", "reserve"],
        },
        action_id: {
            "kind": "reserve",
            "asset_id": "car",
            "window": 0,
            "expected_version": 0,
        },
    }
    # Identical retries retain both the original input and the final receipt.
    assert (
        await world.set_consent(
            run_id, "owner-1", 20, ["car"], consent_id, allowed_kinds=["cancel", "reserve"]
        )
        == consent
    )
    assert await world.submit(run_id, "owner-1", 0, action_id, "reserve", "car", 0) == settled
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT * FROM world_requests WHERE run_id=%s", (run_id,))
        ).fetchall()
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
        assert row["result"] == (consent if row["request_id"] == consent_id else settled)
