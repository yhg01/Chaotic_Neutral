"""Booking assistance uses real PostgreSQL, HTTP and the clock; no provider calls."""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from agentrpg.booking_inquiries import BookingInquiryService
from agentrpg.director import Director
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.world import WorldError

specification = importlib.util.spec_from_file_location(
    "booking_control", Path(__file__).resolve().parents[1] / "scripts/try_clock_control.py"
)
control = importlib.util.module_from_spec(specification)
sys.modules[specification.name] = control
specification.loader.exec_module(control)


def scenario():
    data = control.scenario(("amy", "ben", "casey"), duration_minutes=120).model_dump(mode="json")
    data["clock"]["sites"] = [
        {
            "site_id": "rooms",
            "name": "Harbor Rooms",
            "kind": "booking",
            "slots": [
                {
                    "id": "morning",
                    "title": "Meeting room",
                    "when": "Monday 10:00",
                    "price": 0,
                    "capacity": 1,
                }
            ],
        }
    ]
    return ScenarioSpec.model_validate(data)


def profile():
    return {
        "tasks": [
            {
                "id": "meeting-01",
                "owner_id": "casey",
                "requirements": "A meeting room on Monday morning.",
                "contacts": [
                    {
                        "contact_id": owner,
                        "delay_seconds": delay,
                        "options": [{"site_id": "rooms", "slot_id": "morning"}],
                    }
                    for owner, delay in (("amy", 600), ("ben", 120))
                ],
            }
        ]
    }


@pytest.fixture
async def environment(app, pool, test_database_url):
    created = await Director(pool).create_run(scenario())
    run_id = UUID(created["run_id"])
    service = BookingInquiryService(pool)
    await service.configure(run_id, profile())
    await Director(pool).control(run_id, "start")
    models = {o: control.ScriptModel() for o in ("amy", "ben", "casey")}
    actors = {o: control.ScriptedOwner() for o in models}
    async with control.clocked_run(
        app, test_database_url, scenario(), models, actors, created=created
    ) as (engine, _, _, _):
        await engine.advance(run_id, max_sim_minutes=1)
    return run_id, service, created


async def test_requests_are_scoped_idempotent_and_do_not_book(environment, pool):
    run_id, service, _ = environment
    request_id = uuid4()
    args = {"contact_id": "ben", "meeting_task_id": "meeting-01"}
    results = await asyncio.gather(
        *[service.request(run_id, "casey", request_id, args) for _ in range(4)]
    )
    assert all(result == results[0] for result in results)
    assert results[0]["status"] == "pending" and results[0]["options"] is None
    assert "due_s" not in results[0]
    with pytest.raises(WorldError, match="different payload"):
        await service.request(run_id, "casey", request_id, {**args, "contact_id": "amy"})
    with pytest.raises(WorldError, match="task not found"):
        await service.request(run_id, "amy", uuid4(), args)
    with pytest.raises(WorldError, match="not permitted"):
        await service.request(run_id, "casey", uuid4(), {**args, "contact_id": "casey"})
    with pytest.raises(WorldError, match="not found"):
        await service.read(run_id, "amy", results[0]["id"])
    with pytest.raises(WorldError, match="not ready"):
        await service.deliver(run_id, results[0]["id"])
    async with pool.connection() as conn:
        assert (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM web_bookings WHERE run_id=%s", (run_id,)
                )
            ).fetchone()
        )["n"] == 0
        assert (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM clock_events WHERE run_id=%s "
                    "AND kind='booking_options_reply'",
                    (run_id,),
                )
            ).fetchone()
        )["n"] == 1


async def test_delivery_is_atomic_replayable_and_private(environment, pool, monkeypatch):
    run_id, service, _ = environment
    args = {"contact_id": "ben", "meeting_task_id": "meeting-01"}
    request_id = uuid4()
    original = await service.request(run_id, "casey", request_id, args)
    async with pool.connection() as conn:
        await conn.execute("UPDATE clock_state SET sim_s=1000 WHERE run_id=%s", (run_id,))
    emit = service.world._event

    async def broken(*args, **kwargs):
        raise RuntimeError("notification failed")

    monkeypatch.setattr(service.world, "_event", broken)
    with pytest.raises(RuntimeError):
        await service.deliver(run_id, original["id"])
    assert (await service.read(run_id, "casey", original["id"]))["status"] == "pending"
    monkeypatch.setattr(service.world, "_event", emit)
    delivered = await service.deliver(run_id, original["id"])
    assert delivered["status"] == "ready"
    assert delivered["options"] == [{"site_id": "rooms", "slot_id": "morning"}]
    fresh = BookingInquiryService(pool)
    assert await fresh.deliver(run_id, original["id"]) == delivered
    assert await fresh.request(run_id, "casey", request_id, args) == original
    async with pool.connection() as conn:
        records = await (
            await conn.execute(
                "SELECT owner_id FROM run_events WHERE run_id=%s AND kind='booking_options_ready'",
                (run_id,),
            )
        ).fetchall()
        assert records == [{"owner_id": "casey"}]
        await conn.execute("UPDATE environment_runs SET status='paused' WHERE id=%s", (run_id,))
    assert await fresh.request(run_id, "casey", request_id, args) == original
    with pytest.raises(WorldError, match="not running"):
        await fresh.request(run_id, "casey", uuid4(), args)


async def test_http_tools_bind_identity_and_retry_ids(environment, app, monkeypatch):
    run_id, _, created = environment
    people = {p["owner_id"]: p for p in created["participants"]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": f"Bearer {people['casey']['token']}"},
    ) as client:
        namespace = uuid4()
        tools = EnvironmentTools(client, run_id, namespace, clocked=True, booking_inquiries=True)
        args = {"contact_id": "ben", "meeting_task_id": "meeting-01"}
        original_request = client.request
        lost = False

        async def lose_committed_response(*args, **kwargs):
            nonlocal lost
            response = await original_request(*args, **kwargs)
            if not lost and response.is_success:
                lost = True
                raise httpx.ReadError("Lost response after commit")
            return response

        monkeypatch.setattr(client, "request", lose_committed_response)
        first = await tools.execute("request_booking_options", args, "saved-call")
        assert first["ok"], first
        assert lost
        assert await tools.execute("request_booking_options", args, "saved-call") == first
        fetched = await tools.execute(
            "read_booking_inquiry", {"inquiry_id": first["data"]["id"]}, "read"
        )
        assert fetched["ok"] and fetched["data"]["status"] == "pending"
        assert not (
            await tools.execute("request_booking_options", {**args, "owner_id": "amy"}, "spoof")
        )["ok"]
        client.headers["Authorization"] = f"Bearer {people['amy']['token']}"
        assert (
            await client.get(f"runs/{run_id}/booking-inquiries/{first['data']['id']}")
        ).status_code == 404
        assert (
            await client.get(f"runs/{uuid4()}/booking-inquiries/{first['data']['id']}")
        ).status_code == 404


def test_tool_is_opt_in_and_clocked():
    client = httpx.AsyncClient(base_url="http://test/")
    assert "request_booking_options" not in EnvironmentTools(client, uuid4()).tools
    with pytest.raises(ValueError):
        EnvironmentTools(client, uuid4(), booking_inquiries=True)


async def test_real_graph_gets_delayed_options_from_clock(app, pool, test_database_url):
    created = await Director(pool).create_run(scenario())
    run_id = UUID(created["run_id"])
    await BookingInquiryService(pool).configure(run_id, profile())

    def read_reply(messages):
        prior = next(
            json.loads(m.content)["data"]
            for m in messages
            if m.type == "tool" and m.name == "request_booking_options"
        )
        return control.calls(("read_booking_inquiry", {"inquiry_id": prior["id"]}))

    models = {owner: control.ScriptModel() for owner in ("amy", "ben", "casey")}
    models["casey"] = control.ScriptModel(
        script=[
            control.calls(
                ("request_booking_options", {"contact_id": "ben", "meeting_task_id": "meeting-01"})
            ),
            control.end_turn(status="waiting"),
            read_reply,
            control.end_turn(),
        ]
    )
    actors = {owner: control.ScriptedOwner() for owner in models}
    actors["casey"] = control.ScriptedOwner(
        [{"kind": "reply", "text": "Please ask Ben about meeting-01."}]
    )

    def factory(runtime, **kwargs):
        adapter = kwargs["environment"]
        kwargs["environment"] = EnvironmentTools(
            adapter.client,
            adapter.run_id,
            clocked=True,
            booking_inquiries=kwargs["model"] is models["casey"],
        )
        return runtime.agent(**kwargs)

    async with control.clocked_run(
        app, test_database_url, scenario(), models, actors, created=created, agent_factory=factory
    ) as (engine, _, _, agents):
        assert agents["casey"].environment.booking_inquiries
        await engine.run(run_id)
    async with pool.connection() as conn:
        inquiry = await (
            await conn.execute("SELECT * FROM booking_inquiries WHERE run_id=%s", (run_id,))
        ).fetchone()
        assert inquiry["status"] == "ready"
        failures = await (
            await conn.execute(
                "SELECT kind,payload FROM run_events WHERE run_id=%s "
                "AND kind IN ('simulator_error','job_failure')",
                (run_id,),
            )
        ).fetchall()
        assert failures == []
        ready = await (
            await conn.execute(
                "SELECT created_at FROM run_events WHERE run_id=%s "
                "AND kind='booking_options_ready'",
                (run_id,),
            )
        ).fetchone()
        assert ready is not None
        delivered_s = (
            ready["created_at"] - datetime.fromisoformat(scenario().clock.start_at)
        ).total_seconds()
        assert abs(delivered_s - inquiry["due_s"]) < 1
        assert (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM model_spend_attempts WHERE run_id=%s", (run_id,)
                )
            ).fetchone()
        )["n"] == 0
    assert any(
        m.type == "tool"
        and m.name == "read_booking_inquiry"
        and json.loads(m.content)["data"]["status"] == "ready"
        for m in models["casey"].all_seen[-1]
    )


async def test_profile_validation_and_freeze(pool):
    created = await Director(pool).create_run(scenario())
    run_id = UUID(created["run_id"])
    service = BookingInquiryService(pool)
    bad = profile()
    bad["tasks"][0]["contacts"][0]["contact_id"] = "foreign"
    with pytest.raises(WorldError, match="Unknown booking contact"):
        await service.configure(run_id, bad)
    bad = profile()
    bad["tasks"][0]["contacts"][0]["options"][0]["slot_id"] = "missing"
    with pytest.raises(WorldError, match="Unknown booking slot"):
        await service.configure(run_id, bad)
    await service.configure(run_id, profile())
    await service.configure(run_id, profile())
    changed = profile()
    changed["tasks"][0]["contacts"][0]["delay_seconds"] = 10
    with pytest.raises(WorldError, match="configuration has changed"):
        await service.configure(run_id, changed)
    other = await Director(pool).create_run(scenario())
    await Director(pool).control(other["run_id"], "start")
    with pytest.raises(WorldError, match="before starting"):
        await service.configure(other["run_id"], profile())


async def test_contact_delays_do_not_expose_early_options(environment, pool):
    run_id, service, _ = environment
    args = {"meeting_task_id": "meeting-01"}
    slow = await service.request(run_id, "casey", uuid4(), {**args, "contact_id": "amy"})
    fast = await service.request(run_id, "casey", uuid4(), {**args, "contact_id": "ben"})
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT contact_id,due_s FROM booking_inquiries WHERE run_id=%s", (run_id,)
            )
        ).fetchall()
        times = {r["contact_id"]: r["due_s"] for r in rows}
        assert times["amy"] - times["ben"] == 480
        await conn.execute(
            "UPDATE clock_state SET sim_s=%s WHERE run_id=%s", (times["ben"], run_id)
        )
    await service.deliver(run_id, fast["id"])
    assert (await service.read(run_id, "casey", fast["id"]))["status"] == "ready"
    assert (await service.read(run_id, "casey", slow["id"]))["options"] is None
    with pytest.raises(WorldError, match="not ready"):
        await service.deliver(run_id, slow["id"])


async def test_request_and_scheduled_reply_roll_back_together(environment, pool, monkeypatch):
    from agentrpg import clock

    run_id, service, _ = environment

    async def fail(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(clock, "schedule", fail)
    with pytest.raises(RuntimeError):
        await service.request(
            run_id, "casey", uuid4(), {"contact_id": "ben", "meeting_task_id": "meeting-01"}
        )
    async with pool.connection() as conn:
        assert (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM booking_inquiries WHERE run_id=%s", (run_id,)
                )
            ).fetchone()
        )["n"] == 0
