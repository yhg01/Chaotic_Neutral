"""Real PostgreSQL timed payments, authority, rollback, receipts and scoped HTTP tools."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

import httpx
import psycopg
import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from test_deep_agent import ScriptedModel, call

from agentrpg.activation import ActivationEnvelope
from agentrpg.agent_tools import BoardTools
from agentrpg.clock_transfers import TimedActorOutput, TimedTransfers
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, row
from agentrpg.director_clock import DirectorClock
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxService
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.simulation_clock import (
    ClockConflict,
    ClockContract,
    Control,
    ProcessSpec,
    Schedule,
    Sleep,
)
from agentrpg.transfer_demo import scenario
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.world import WorldError, WorldService


async def setup(pool, *, horizon=100, balance=100, enabled=True, limits=None):
    spec = scenario()
    data = spec.model_dump(mode="json")
    data["world"]["balances"]["alice"] = balance
    spec = type(spec).model_validate(data)
    manifest = await Director(pool).create_run(spec)
    run = UUID(manifest["run_id"])
    director = DirectorClock(pool)
    transfers = TimedTransfers(pool)
    contract = ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=horizon,
        output_tokens_per_minute=600,
        tool_seconds={"transfer_credits": 7},
        processes=tuple(
            ProcessSpec(id=f"{role}-{o.id}", owner_id=o.id, role=role)
            for o in spec.owners
            for role in ["actor", "agent"]
        ),
    )
    clock = await director.bind(run, contract)
    if enabled:
        await transfers.enable(run, **(limits or {}))
    for p in contract.processes:
        if p.id != "actor-alice":
            await director.control(
                run,
                uuid4(),
                Sleep(
                    process_id=p.id,
                    until_seconds=horizon + 20,
                    interrupt_on=(
                        "owner_message",
                        "assistant_reply",
                        "inbox_received",
                        "transfer_completed",
                    ),
                ),
            )
    return director, transfers, run, clock, manifest


async def grant(pool, **kwargs):
    director, transfers, run, clock, manifest = await setup(pool, **kwargs)
    task = await director.prepare(run, "actor-alice", uuid4())

    async def owner(packet):
        return TimedActorOutput(
            text="I authorize one 10-credit repayment to Bob for lunch.",
            output_tokens=10,
            until_seconds=200,
            transfer_consent={"recipient_owner_id": "bob", "amount": 10, "memo": "Lunch repayment"},
        )

    await director.dispatch(run, task["id"], owner)
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM transfer_grants"))["n"] == 0
    await director.step(run, uuid4())
    await director.step(run, uuid4())
    arguments = TransferArguments(
        authorization_id=uuid5(UUID(task["id"]), "clock-transfer-consent"),
        recipient_owner_id="bob",
        amount=10,
        memo="Lunch repayment",
    )
    return director, transfers, run, clock, manifest, arguments


async def counts(pool, run):
    async with pool.connection() as conn:
        result = {
            t: (await row(conn, f"SELECT count(*) AS n FROM {t} WHERE run_id=%s", (run,)))["n"]
            for t in [
                "credit_transfers",
                "inbox_messages",
                "clock_transfer_operations",
                "clock_transfer_calls",
            ]
        }
        result["balances"] = {
            r["owner_id"]: r["balance"]
            for r in await (
                await conn.execute(
                    "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s", (run,)
                )
            ).fetchall()
        }
    return result


async def test_not_early_then_one_native_receipt_mail_and_conserved_ledger(pool):
    director, transfers, run, clock, manifest, arguments = await grant(pool)
    pending = await transfers.schedule(run, "agent-alice", uuid4(), arguments)
    assert pending["status"] == "pending" and pending["due_seconds"] == 8
    assert (await counts(pool, run))["balances"] == {"alice": 100, "bob": 20}
    await director.control(
        run,
        uuid4(),
        Schedule(event_key="early-probe", at_seconds=7, kind="probe", recipients=("actor-bob",)),
    )
    assert (await director.step(run, uuid4()))["now_seconds"] == 7
    assert (await counts(pool, run))["credit_transfers"] == 0
    step = await director.step(run, uuid4())
    receipt = step["transfer"]["result"]
    assert step["now_seconds"] == 8 and receipt["status"] == "accepted"
    assert receipt["request_id"] == pending["id"] and receipt["simulated_seconds"] == 8
    state = await counts(pool, run)
    assert (
        state["balances"] == {"alice": 90, "bob": 30}
        and state["credit_transfers"] == state["inbox_messages"] == 1
    )
    assert (await transfers.operation(run, "agent-alice", pending["id"]))["result"] == receipt
    service = WorldService(pool)
    assert service.replay(await service.journal(run)) == await service.snapshot(run)
    follow = await director.prepare(run, "agent-alice", uuid4())
    assert follow["input"]["transfer_account"]["transfers"][0]["request_id"] == pending["id"]


async def test_repeated_same_and_new_call_ids_share_one_operation_even_after_payment(pool):
    director, transfers, run, clock, manifest, args = await grant(pool)
    request = uuid4()
    admitted = await asyncio.gather(
        *(
            transfers.schedule(run, "agent-alice", request if i < 5 else uuid4(), args)
            for i in range(12)
        )
    )
    assert all(a == admitted[0] for a in admitted)
    step_id = uuid4()
    paid = await director.step(run, step_id)
    assert await DirectorClock(pool).step(run, step_id) == paid
    await director.control(run, uuid4(), Control(operation="pause"))
    assert await transfers.schedule(run, "agent-alice", request, args) == admitted[0]
    assert await transfers.schedule(run, "agent-alice", uuid4(), args) == admitted[0]
    assert (await counts(pool, run))["credit_transfers"] == 1


@pytest.mark.parametrize(
    "process,change",
    [
        ("actor-alice", {}),
        ("agent-bob", {}),
        ("agent-alice", {"amount": 11}),
        ("agent-alice", {"recipient_owner_id": "alice"}),
        ("agent-alice", {"memo": "Other purpose"}),
        ("agent-alice", {"authorization_id": uuid4()}),
    ],
)
async def test_wrong_role_grant_direction_amount_or_memo_never_queue(pool, process, change):
    director, transfers, run, clock, manifest, args = await grant(pool)
    changed = TransferArguments.model_validate({**args.model_dump(), **change})
    with pytest.raises(WorldError):
        await transfers.schedule(run, process, uuid4(), changed)
    assert (await counts(pool, run))["clock_transfer_operations"] == 0


async def test_changed_call_payload_conflicts_and_foreign_read_is_hidden(pool):
    director, transfers, run, clock, manifest, args = await grant(pool)
    request = uuid4()
    pending = await transfers.schedule(run, "agent-alice", request, args)
    changed = args.model_copy(update={"memo": "changed"})
    with pytest.raises(ClockConflict):
        await transfers.schedule(run, "agent-alice", request, changed)
    with pytest.raises(WorldError) as error:
        await transfers.operation(run, "agent-bob", pending["id"])
    assert error.value.status_code == 404


async def test_insufficient_funds_is_terminal_rejection_without_bank_success(pool):
    director, transfers, run, clock, manifest, args = await grant(pool, balance=5)
    pending = await transfers.schedule(run, "agent-alice", uuid4(), args)
    result = await director.step(run, uuid4())
    assert result["transfer"]["result"]["status"] == "rejected"
    assert (await transfers.operation(run, "agent-alice", pending["id"]))["status"] == "rejected"
    state = await counts(pool, run)
    assert (
        state["balances"] == {"alice": 5, "bob": 20}
        and state["credit_transfers"] == state["inbox_messages"] == 0
    )
    assert await transfers.schedule(run, "agent-alice", uuid4(), args) == pending


async def test_pause_and_horizon_do_not_debit_or_publish_early_receipts(pool):
    director, transfers, run, clock, manifest, args = await grant(pool, horizon=5)
    pending = await transfers.schedule(run, "agent-alice", uuid4(), args)
    await director.control(run, uuid4(), Control(operation="pause"))
    assert (await director.step(run, uuid4()))["reason"] == "paused"
    await director.control(run, uuid4(), Control(operation="resume"))
    assert (await director.step(run, uuid4()))["reason"] == "horizon_cutoff"
    assert (await transfers.operation(run, "agent-alice", pending["id"]))["status"] == "pending"
    assert (await counts(pool, run))["balances"] == {"alice": 100, "bob": 20}


async def failure_trigger(pool, table, when=""):
    async with pool.connection() as conn:
        await conn.execute("""CREATE FUNCTION fail_timed_transfer() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'injected timed transfer storage failure'; END;
            $$ LANGUAGE plpgsql""")
        await conn.execute(
            f"CREATE TRIGGER fail_timed_transfer BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW {when} EXECUTE FUNCTION fail_timed_transfer()"
        )


async def remove_trigger(pool, table):
    async with pool.connection() as conn:
        await conn.execute(f"DROP TRIGGER fail_timed_transfer ON {table}")


async def test_failed_actor_grant_rolls_back_owner_message_clock_and_authority(pool):
    director, transfers, run, clock, manifest = await setup(pool)
    task = await director.prepare(run, "actor-alice", uuid4())

    async def owner(packet):
        return TimedActorOutput(
            text="Pay Bob 10 once.",
            output_tokens=10,
            until_seconds=100,
            transfer_consent={"recipient_owner_id": "bob", "amount": 10, "memo": "Lunch repayment"},
        )

    await director.dispatch(run, task["id"], owner)
    before = await director.clock.snapshot(clock)
    await failure_trigger(pool, "transfer_grants")
    request = uuid4()
    with pytest.raises(psycopg.Error):
        await director.step(run, request)
    assert (
        await director.clock.snapshot(clock) == before
        and await Director(pool).messages(run, "alice") == []
    )
    await remove_trigger(pool, "transfer_grants")
    await director.step(run, request)
    assert len((await TransferService(pool).account(run, "alice"))["authorizations"]) == 1


async def test_queue_admission_failure_has_no_orphan_busy_lane_or_operation(pool):
    director, transfers, run, clock, manifest, args = await grant(pool)
    before = await director.clock.snapshot(clock)
    await failure_trigger(pool, "clock_transfer_calls")
    request = uuid4()
    with pytest.raises(psycopg.Error):
        await transfers.schedule(run, "agent-alice", request, args)
    assert await director.clock.snapshot(clock) == before
    assert (await counts(pool, run))["clock_transfer_operations"] == 0
    await remove_trigger(pool, "clock_transfer_calls")
    await transfers.schedule(run, "agent-alice", request, args)


async def test_failure_after_debit_rolls_back_clock_money_bank_notice_and_receipt(pool):
    director, transfers, run, clock, manifest, args = await grant(pool)
    pending = await transfers.schedule(run, "agent-alice", uuid4(), args)
    before = await director.clock.snapshot(clock)
    await failure_trigger(pool, "clock_transfer_operations", "WHEN (NEW.status='applied')")
    request = uuid4()
    with pytest.raises(psycopg.Error):
        await director.step(run, request)
    assert await director.clock.snapshot(clock) == before
    state = await counts(pool, run)
    assert state["credit_transfers"] == state["inbox_messages"] == 0 and state["balances"] == {
        "alice": 100,
        "bob": 20,
    }
    await remove_trigger(pool, "clock_transfer_operations")
    await director.step(run, request)
    assert (await transfers.operation(run, "agent-alice", pending["id"]))["status"] == "applied"
    assert (await counts(pool, run))["credit_transfers"] == 1


async def test_notice_infrastructure_failure_is_not_a_false_business_rejection(pool, monkeypatch):
    director, transfers, run, clock, manifest, args = await grant(pool)
    await transfers.schedule(run, "agent-alice", uuid4(), args)
    original = InboxService.send

    async def broken(*args, **kwargs):
        raise RuntimeError("injected bank notice fault")

    monkeypatch.setattr(InboxService, "send", broken)
    request = uuid4()
    with pytest.raises(RuntimeError):
        await director.step(run, request)
    assert (await counts(pool, run))["credit_transfers"] == 0
    monkeypatch.setattr(InboxService, "send", original)
    await director.step(run, request)
    assert (await counts(pool, run))["credit_transfers"] == 1


async def test_native_legacy_and_forged_context_paths_cannot_pay_before_due(pool):
    director, transfers, run, clock, manifest, args = await grant(pool)
    pending = await transfers.schedule(run, "agent-alice", uuid4(), args)
    with pytest.raises(WorldError):
        await TransferService(pool).transfer(run, "alice", uuid4(), args)
    async with pool.connection() as conn, conn.transaction():
        with pytest.raises(WorldError):
            await TransferService(pool).transfer(
                run,
                "alice",
                UUID(pending["id"]),
                args,
                connection=conn,
                clock_operation_id=UUID(pending["id"]),
            )
    assert (await counts(pool, run))["credit_transfers"] == 0


async def test_profile_is_frozen_and_agents_cannot_supply_owner_consent(pool):
    director, transfers, run, clock, manifest, args = await grant(pool)
    with pytest.raises(ClockConflict):
        await transfers.enable(run, max_operations=1)
    task = await director.prepare(run, "agent-alice", uuid4())

    async def bad_agent(packet):
        return TimedActorOutput(
            text="I approve",
            output_tokens=1,
            until_seconds=100,
            transfer_consent={"recipient_owner_id": "bob", "amount": 1, "memo": "x"},
        )

    with pytest.raises(ValidationError):
        await director.dispatch(run, task["id"], bad_agent)
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM transfer_grants"))["n"] == 1


async def test_authenticated_existing_transfer_tool_queues_and_owner_read_is_scoped(app, pool):
    director, transfers, run, clock, manifest, args = await grant(pool)
    owners = {p["owner_id"]: p for p in manifest["participants"]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + owners["alice"]["token"]},
    ) as client:
        tools = EnvironmentTools(client, run, timed_transfers=True)
        result = await tools.execute("transfer_credits", args.model_dump(mode="json"), "pay")
        assert result["ok"] and result["data"]["status"] == "pending"
        op = result["data"]["id"]
        assert (await counts(pool, run))["credit_transfers"] == 0
        assert (
            await tools.execute("transfer_credits", args.model_dump(mode="json"), "pay") == result
        )
        await director.step(run, uuid4())
        actual = await tools.execute("read_transfer_operation", {"operation_id": op}, "receipt")
        assert actual["ok"] and actual["data"]["result"]["request_id"] == op
        final = await tools.execute(
            "transfer_credits", args.model_dump(mode="json"), "fresh-after-commit"
        )
        assert (
            final["ok"]
            and final["data"]["status"] == "accepted"
            and final["data"]["request_id"] == op
        )
        other = await client.get(
            f"runs/{run}/transfer-operations/{op}",
            headers={"Authorization": "Bearer " + owners["bob"]["token"]},
        )
        assert other.status_code == 404


async def test_real_agent_graph_with_supplied_responses_yields_on_pending_without_paying_early(
    app, pool, test_database_url
):
    director, transfers, run, clock, manifest, args = await grant(pool)
    alice = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    model = ScriptedModel(
        responses=[call("transfer_credits", args.model_dump(mode="json"), "timed-pay")]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + alice["token"]},
    ) as client:
        async with AgentRuntime(test_database_url) as runtime:
            agent = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, timed_transfers=True),
                agent_id=alice["agent_id"],
                session_id=alice["session_id"],
                instructions="Use only actual committed payment receipts.",
            )
            envelope = ActivationEnvelope(
                run_id=run, participant_id=alice["agent_id"], activation_id=uuid4(), window_id=0
            )
            first = await agent.run_activation(envelope)
            assert first.status == "waiting" and len(first.pending_ids) == 1 and first.text == ""
            count = model.calls
            assert await agent.run_activation(envelope) == first and model.calls == count
            assert agent.environment.timed_transfers
    assert (await counts(pool, run))["credit_transfers"] == 0
    await director.step(run, uuid4())
    assert (await transfers.operation(run, "agent-alice", first.pending_ids[0]))["result"][
        "status"
    ] == "accepted"
    completed = ScriptedModel(
        responses=[
            call(
                "read_transfer_operation",
                {"operation_id": str(first.pending_ids[0])},
                "actual-receipt",
            ),
            AIMessage(content="Verified committed payment receipt " + str(first.pending_ids[0])),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + alice["token"]},
    ) as client:
        async with AgentRuntime(test_database_url) as runtime:
            agent = runtime.agent(
                model=completed,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, timed_transfers=True),
                agent_id=alice["agent_id"],
                session_id=alice["session_id"],
                instructions="Use only actual committed payment receipts.",
            )
            envelope = ActivationEnvelope(
                run_id=run, participant_id=alice["agent_id"], activation_id=uuid4(), window_id=0
            )
            result = await agent.run_activation(envelope)
            assert result.status != "waiting" and str(first.pending_ids[0]) in result.text
            calls = completed.calls
            assert await agent.run_activation(envelope) == result and completed.calls == calls


async def test_fifteen_owner_ring_uses_same_authority_and_payment_path_without_duplicates(pool):
    spec = vehicle_scenario(15)
    run = UUID((await Director(pool).create_run(spec))["run_id"])
    director = DirectorClock(pool)
    transfers = TimedTransfers(pool)
    contract = ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=1000,
        output_tokens_per_minute=600,
        tool_seconds={"transfer_credits": 7},
        processes=tuple(
            ProcessSpec(id=f"{role}-{i}", owner_id=owner.id, role=role)
            for i, owner in enumerate(spec.owners)
            for role in ["actor", "agent"]
        ),
    )
    await director.bind(run, contract)
    await transfers.enable(run)
    grants = []
    for i, _owner in enumerate(spec.owners):
        recipient = spec.owners[(i + 1) % 15].id
        await director.control(
            run,
            uuid4(),
            Sleep(process_id=f"agent-{i}", until_seconds=1000, interrupt_on=("owner_message",)),
        )
        task = await director.prepare(run, f"actor-{i}", uuid4())

        async def consent(packet, target=recipient):
            return TimedActorOutput(
                text="I authorize one credit to " + target,
                output_tokens=10,
                until_seconds=1000,
                transfer_consent={
                    "recipient_owner_id": target,
                    "amount": 1,
                    "memo": "Ring control",
                },
            )

        await director.dispatch(run, task["id"], consent)
        grants.append(
            TransferArguments(
                authorization_id=uuid5(UUID(task["id"]), "clock-transfer-consent"),
                recipient_owner_id=recipient,
                amount=1,
                memo="Ring control",
            )
        )
    for _ in range(30):
        await director.step(run, uuid4())
    pending = await asyncio.gather(
        *(transfers.schedule(run, f"agent-{i}", uuid4(), grant) for i, grant in enumerate(grants))
    )
    for _ in range(15):
        await director.step(run, uuid4())
    again = await asyncio.gather(
        *(transfers.schedule(run, f"agent-{i}", uuid4(), grant) for i, grant in enumerate(grants))
    )
    assert again == pending
    state = await counts(pool, run)
    assert (
        state["balances"] == spec.world.balances
        and state["credit_transfers"] == state["inbox_messages"] == 15
    )
