"""Administrator observation of real persisted records without write authority."""

from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentrpg.budget import CampaignLedger, ModelPrice, budget_context
from agentrpg.director import Director, emit
from agentrpg.inbox import InboxService
from agentrpg.scenario_schema import vehicle_scenario


async def provision(client):
    response = await client.post("/api/v1/runs", json=vehicle_scenario().model_dump(mode="json"))
    assert response.status_code == 201, response.text
    return response.json()


async def test_observer_lists_saved_runs_and_empty_views(client):
    assert (await client.get("/api/v1/observer/runs")).json()["items"] == []
    run = await provision(client)
    listed = (await client.get("/api/v1/observer/runs")).json()
    assert listed["items"][0]["id"] == run["run_id"]
    assert listed["items"][0]["participants"] == 2
    response = await client.get(f"/api/v1/observer/runs/{run['run_id']}")
    assert response.status_code == 200, response.text
    overview = response.json()
    assert overview["run"]["status"] == "pending"
    assert len(overview["participants"]) == 2
    assert overview["campaigns"] == []
    for member in run["participants"]:
        assert member["token"] not in response.text
    assert "token_hash" not in response.text


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/events",
        "/records/chat",
        "/records/mail",
        "/records/resources",
        "/records/receipts",
        "/records/jobs",
    ],
)
async def test_agent_credentials_cannot_observe_private_environment(client, suffix):
    run = await provision(client)
    response = await client.get(
        f"/api/v1/observer/runs/{run['run_id']}{suffix}",
        headers={"Authorization": "Bearer " + run["participants"][0]["token"]},
    )
    assert response.status_code == 403


async def test_observer_requires_admin_and_is_read_only(client):
    run = await provision(client)
    assert (
        await client.get("/api/v1/observer/runs", headers={"Authorization": ""})
    ).status_code == 401
    assert (await client.post(f"/api/v1/observer/runs/{run['run_id']}", json={})).status_code == 405
    assert (await client.get("/api/v1/observer/runs?limit=101")).status_code == 422
    assert (await client.get("/api/v1/observer/runs?offset=-1")).status_code == 422
    assert (await client.get(f"/api/v1/observer/runs/{uuid4()}")).status_code == 404


async def test_events_paginate_filter_and_render_data_without_interpretation(client, pool):
    run = await provision(client)
    run_id = UUID(run["run_id"])
    async with pool.connection() as conn:
        for index in range(6):
            await emit(
                conn,
                run_id,
                "assistant_tool_call" if index % 2 else "note",
                0,
                {"content": '<img src="x" onerror="alert(1)">', "index": index},
                f"observer-test-{index}",
                "owner-1" if index < 4 else "owner-2",
            )
    base = f"/api/v1/observer/runs/{run_id}/events"
    first = (await client.get(base + "?limit=2")).json()
    second = (await client.get(base + "?limit=2&offset=2")).json()
    assert first["next_offset"] == 2
    assert not ({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})
    selected = (await client.get(base + "?owner_id=owner-2&kind=assistant_tool_call")).json()
    assert len(selected["items"]) == 1
    assert selected["items"][0]["payload"]["index"] == 5


@pytest.mark.parametrize(
    "collection",
    [
        "chat",
        "mail",
        "resources",
        "market",
        "receipts",
        "trades",
        "accounts",
        "reservations",
        "jobs",
        "board",
    ],
)
async def test_record_views_have_bounded_pages(client, collection):
    run = await provision(client)
    response = await client.get(
        f"/api/v1/observer/runs/{run['run_id']}/records/{collection}?owner_id=owner-1&limit=1"
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) <= 1
    assert "token_hash" not in response.text


async def test_owner_chat_filter_exposes_only_selected_owner(client, pool):
    run = await provision(client)
    director = Director(pool)
    await director.control(run["run_id"], "start")
    await director.send_message(run["run_id"], "owner-1", "First owner private reply", uuid4())
    await director.send_message(run["run_id"], "owner-2", "Second owner private reply", uuid4())
    response = await client.get(
        f"/api/v1/observer/runs/{run['run_id']}/records/chat?owner_id=owner-1"
    )
    assert "First owner private reply" in response.text
    assert "Second owner private reply" not in response.text


async def test_observer_mail_includes_recipient_acknowledgement(client, pool):
    run = await provision(client)
    await Director(pool).control(run["run_id"], "start")
    inbox = InboxService(pool)
    message = await inbox.send(
        run["run_id"], "owner-1", "owner-2", "Question", "Please reply.", uuid4()
    )
    path = f"/api/v1/observer/runs/{run['run_id']}/records/mail?owner_id=owner-1"
    before = (await client.get(path)).json()["items"]
    assert before[0]["acknowledged"] is False and before[0]["read_at"] is None
    await inbox.acknowledge(run["run_id"], "owner-2", [message["id"]])
    after = (await client.get(path)).json()["items"]
    assert after[0]["acknowledged"] is True and after[0]["read_at"]


async def test_observer_costs_show_reported_reserved_and_uncertain(client, pool):
    run, campaign = await provision(client), uuid4()
    ledger = CampaignLedger(pool)
    await ledger.ensure_campaign(campaign, "30", name="Observer test")
    price = ModelPrice(
        "openai/gpt-4.1",
        Decimal("0.01"),
        Decimal("0"),
        Decimal("0"),
        1047576,
        "2026-10-28T00:00:00Z",
    )
    with budget_context(run_id=run["run_id"], owner_id="owner-1"):
        first = await ledger.reserve(campaign, price, 10, 1, role="assistant")
        second = await ledger.reserve(campaign, price, 10, 1, role="actor")
        await ledger.reserve(campaign, price, 10, 1, role="reviewer")
    await ledger.finish(first, reported="0.01")
    await ledger.finish(second, reason="Unknown charge")
    summary = (await client.get(f"/api/v1/observer/campaigns/{campaign}")).json()
    assert Decimal(str(summary["reported_usd"])) == Decimal("0.01")
    assert Decimal(str(summary["reserved_usd"])) == Decimal("0.1")
    assert Decimal(str(summary["uncertain_usd"])) == Decimal("0.1")
    overview = (await client.get(f"/api/v1/observer/runs/{run['run_id']}")).json()
    assert overview["campaigns"][0]["id"] == str(campaign)
    assert len((await client.get("/api/v1/observer/campaigns")).json()["items"]) == 1


async def test_observer_page_and_assets_load_without_embedding_credentials(client):
    response = await client.get("/runs")
    assert response.status_code == 200
    assert 'id="retry"' in response.text
    assert 'id="login-form"' not in response.text
    assert 'type="password"' not in response.text
    for path in ("run-observer.js", "run-observer.css"):
        assert (await client.get("/assets/" + path)).status_code == 200
    source = (Path(__file__).parents[1] / "src/agentrpg/static/run-observer.js").read_text()
    assert "innerHTML" not in source
    assert "localStorage" not in source and "sessionStorage" not in source
    assert "textContent" in source
    assert "Authorization" not in source


async def test_compact_overview_skips_report_without_changing_default(client, pool):
    from psycopg.types.json import Jsonb

    run = await provision(client)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_reports(run_id,report) VALUES (%s,%s)",
            (UUID(run["run_id"]), Jsonb({"large_evidence": "retained"})),
        )
    base = f"/api/v1/observer/runs/{run['run_id']}"
    full = (await client.get(base)).json()
    compact = (await client.get(base + "?compact=true")).json()
    assert full["report"]["large_evidence"] == "retained"
    assert compact["report"] is None
    assert compact["run"] == full["run"]
