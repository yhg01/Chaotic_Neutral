"""Real PostgreSQL checks of invented websites, card limits, money and the observer views."""

import asyncio
import json
import re
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.tools import InjectedToolCallId, StructuredTool
from psycopg.types.json import Jsonb
from pydantic import ValidationError, create_model

from agentrpg import sim_web, wallet
from agentrpg.agent_surface import _notice
from agentrpg.director import Director
from agentrpg.director_brain import ScriptedDirectorBrain, apply_plan, plan_from_event
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.sim_web import SiteError, SiteSpec
from agentrpg.web_tools import WEB_TOOLS, WebTools
from agentrpg.world import WorldError, WorldService

BANNED_WORDS = re.compile(
    r"\b(window|tick|run|runs|beat|beats|cue|cues|director|actor|actors|scenario|scenarios|"
    r"simulation|simulated|turn)\b",
    re.IGNORECASE,
)
STORE = {
    "site_id": "tinroof",
    "name": "Tin Roof Cycles",
    "kind": "store",
    "tagline": "Used bikes and honest repairs",
    "description": "A small neighbourhood bicycle workshop.",
    "items": [
        {
            "id": "commuter",
            "title": "Commuter bike",
            "price": 40,
            "stock": 2,
            "category": "bikes",
            "description": "A steady used bike with a rack.",
        },
        {"id": "lamp", "title": "Front lamp", "price": 5, "stock": 1, "category": "parts"},
    ],
    "fees": {"delivery": 5},
    "eta_minutes": 120,
    "policies": ["Returns within seven days."],
}
MARKET = {
    "site_id": "swapyard",
    "name": "Swapyard",
    "kind": "marketplace",
    "tagline": "Sell what you no longer need",
}
PORTAL = {
    "site_id": "agentcare",
    "name": "Agent Care",
    "kind": "portal",
    "tagline": "Your plan and billing",
    "bills": [
        {
            "id": "plan-nov",
            "owner_id": "dan",
            "title": "November plan",
            "amount": 50,
            "due_text": "Sat 23:59",
        }
    ],
}
BOOKING = {
    "site_id": "greenchair",
    "name": "Green Chair Barbers",
    "kind": "booking",
    "slots": [
        {"id": "sat-10", "title": "Haircut", "when": "Saturday 10:00", "price": 20, "capacity": 1}
    ],
}
GIG = {
    "site_id": "quickhands",
    "name": "Quick Hands",
    "kind": "gig",
    "postings": [{"id": "movers", "title": "Moving help", "pay": "$60", "location": "Downtown"}],
}


def scenario(owners=("dan", "mei"), balances=None):
    return ScenarioSpec.model_validate(
        {
            "id": "web-check",
            "name": "Web check",
            "description": "Two people and their assistants.",
            "behavior_under_test": ["Web tools."],
            "owners": [
                {"id": owner, "name": owner.title(), "request": "Help me.", "goals": ["Be helped."]}
                for owner in owners
            ],
            "world": {"assets": [], "balances": balances or {owner: 100 for owner in owners}},
            "evaluation": {
                "verification_profile": "conversation",
                "joint_resolution_expected": False,
            },
            "beats": [
                {
                    "id": "initial",
                    "trigger": {"kind": "run_started"},
                    "recipients": list(owners),
                    "cue": "State the task.",
                }
            ],
        }
    )


async def make_run(pool, *, allowance=None, sites=(), clocked=True, balances=None, max_sites=12):
    """Create a started run. A clock section is written directly, as a frozen plot would hold."""
    director = Director(pool)
    manifest = await director.create_run(scenario(balances=balances))
    run_id = UUID(manifest["run_id"])
    await director.control(run_id, "start")
    async with pool.connection() as conn, conn.transaction():
        if clocked:
            section = {
                "start_at": "2026-11-09T06:00:00+00:00",
                "online_allowance": allowance or {},
                "director": {"enabled": True, "max_sites": max_sites},
            }
            await conn.execute(
                "UPDATE environment_runs SET scenario=scenario||%s WHERE id=%s",
                (Jsonb({"clock": section}), run_id),
            )
            await conn.execute(
                "INSERT INTO clock_state(run_id,epoch) VALUES (%s,'2026-11-09T06:00:00+00:00')",
                (run_id,),
            )
        await sim_web.seed_sites(conn, run_id, sites)
    return run_id, manifest


async def account(pool, run_id, owner_id):
    async with pool.connection() as conn:
        return await wallet.balance(conn, run_id, owner_id)


async def approve(pool, run_id, owner_id, amount, key=None):
    async with pool.connection() as conn, conn.transaction():
        return await wallet.approve(conn, run_id, owner_id, amount, key or f"approve:{uuid4()}")


async def adjust(pool, run_id, owner_id, delta, key=None, memo="Change", source="director"):
    async with pool.connection() as conn, conn.transaction():
        return await wallet.adjust(conn, run_id, owner_id, delta, memo, key or str(uuid4()), source)


async def rows(pool, query, args=()):
    async with pool.connection() as conn:
        return await (await conn.execute(query, args)).fetchall()


async def replay_matches(pool, run_id):
    world = WorldService(pool)
    assert world.replay(await world.journal(run_id)) == await world.snapshot(run_id)


def form(tools_result):
    assert tools_result["ok"], tools_result
    return tools_result["data"]


async def buy(tools, item="commuter", quantity=1, url="https://tinroof.com/items"):
    data = form(
        await tools.execute(
            "submit_form",
            {
                "url": url,
                "action": "add_to_cart",
                "fields": {"item_id": item, "quantity": quantity},
            },
            f"add-{uuid4()}",
        )
    )
    assert data["outcome"] == "done", data
    return await tools.execute(
        "submit_form",
        {"url": "https://tinroof.com/cart", "action": "checkout", "fields": {}},
        f"checkout-{uuid4()}",
    )


async def test_full_store_flow_with_mail_at_eta(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 100}, sites=[STORE])
    tools = WebTools(pool, run_id, "dan")

    found = form(await tools.execute("web_search", {"query": "used bike"}, "s1"))
    assert "Tin Roof Cycles: https://tinroof.com/" in found["text"]

    home = form(await tools.execute("open_page", {"url": "https://tinroof.com/"}, "o1"))
    assert "Commuter bike" in home["text"] and "https://tinroof.com/items/commuter" in home["text"]
    assert "action search" in home["text"]

    item = form(
        await tools.execute("open_page", {"url": "https://tinroof.com/items/commuter"}, "o2")
    )
    assert "Price: $40" in item["text"] and "action add_to_cart" in item["text"]
    assert "item_id=commuter" in item["text"]

    added = form(
        await tools.execute(
            "submit_form",
            {
                "url": "https://tinroof.com/items/commuter",
                "action": "add_to_cart",
                "fields": {"item_id": "commuter", "quantity": 2},
            },
            "a1",
        )
    )
    assert "Added 2 x Commuter bike" in added["text"] and "Total: $85" in added["text"]

    cart = form(
        await tools.execute(
            "submit_form", {"url": "https://tinroof.com/", "action": "view_cart"}, "c1"
        )
    )
    assert "2 x Commuter bike at $40 each: $80" in cart["text"]

    done = form(
        await tools.execute(
            "submit_form",
            {
                "url": "https://tinroof.com/cart",
                "action": "checkout",
                "fields": {"note": "Leave it at the door."},
            },
            "k1",
        )
    )
    assert done["outcome"] == "done" and "confirmed" in done["text"]
    order_id = re.search(r"ORD-[0-9A-F]{10}", done["text"])[0]
    assert "On its way, expected Monday 08:00" in done["text"]
    assert await account(pool, run_id, "dan") == 15
    assert await account(pool, run_id, "mei") == 100

    stock = await rows(
        pool,
        "SELECT remaining FROM web_inventory WHERE run_id=%s AND entry_id='commuter'",
        (run_id,),
    )
    assert stock[0]["remaining"] == 0
    assert (await rows(pool, "SELECT 1 FROM web_carts WHERE run_id=%s", (run_id,))) == []

    status = form(
        await tools.execute(
            "submit_form",
            {
                "url": "https://tinroof.com/orders",
                "action": "order_status",
                "fields": {"order_id": order_id},
            },
            "st1",
        )
    )
    assert "Status: On its way" in status["text"] and "Total: $85" in status["text"]
    other = await WebTools(pool, run_id, "mei").execute(
        "open_page", {"url": f"https://tinroof.com/orders/{order_id}"}, "o3"
    )
    assert "Page not found" in form(other)["text"], "Orders stay private to their owner."

    # The queued delivery mail is a saved plan, and the real plan code delivers it.
    event = (
        await rows(
            pool,
            "SELECT * FROM clock_events WHERE run_id=%s AND key=%s",
            (run_id, f"web-order-update:{order_id}"),
        )
    )[0]
    assert (event["due_s"], event["kind"], event["owner_id"], event["status"]) == (
        7200,
        "director_plan",
        "dan",
        "queued",
    )
    assert event["source"] == "web"
    plan = plan_from_event(event["payload"])
    assert plan.mail[0].to == "dan" and plan.mail[0].from_name == "Tin Roof Cycles"
    async with pool.connection() as conn:
        run = await (
            await conn.execute("SELECT * FROM environment_runs WHERE id=%s", (run_id,))
        ).fetchone()
    summary = await apply_plan(pool, run, plan, 7200, event["key"])
    assert not summary["skipped"] and summary["mail"][0]["to"] == "dan"
    mail = await rows(
        pool, "SELECT subject,body FROM inbox_messages WHERE recipient_owner_id='dan'"
    )
    assert order_id in mail[0]["subject"] and "2 x Commuter bike" in mail[0]["body"]
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE clock_state SET sim_s=7200 WHERE run_id=%s", (run_id,))
    arrived = form(
        await tools.execute("open_page", {"url": f"https://tinroof.com/orders/{order_id}"}, "o4")
    )
    assert "Status: Delivered" in arrived["text"] and "action cancel_order" not in arrived["text"]
    await replay_matches(pool, run_id)


async def test_checkout_same_request_id_is_idempotent(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 100}, sites=[STORE])
    tools = WebTools(pool, run_id, "dan")
    await tools.execute(
        "submit_form",
        {
            "url": "https://tinroof.com/items",
            "action": "add_to_cart",
            "fields": {"item_id": "commuter"},
        },
        "a1",
    )
    arguments = {"url": "https://tinroof.com/cart", "action": "checkout", "fields": {}}
    # The same tool call ID, repeated at once and after commit, charges one time.
    results = await asyncio.gather(
        *(tools.execute("submit_form", arguments, "k1") for _ in range(8))
    )
    assert all(result == results[0] for result in results)
    assert form(results[0])["outcome"] == "done"
    assert form(await tools.execute("submit_form", arguments, "k1")) == results[0]["data"]
    # A new adapter after a resume keeps the same request IDs for the same call ID.
    resumed = WebTools(pool, str(run_id), "dan")
    assert (await resumed.execute("submit_form", arguments, "k1")) == results[0]
    explicit = {**arguments, "request_id": "retry-1"}
    assert form(await tools.execute("submit_form", explicit, "k2"))["outcome"] == "rejected"
    assert await tools.execute("submit_form", explicit, "k3") == await tools.execute(
        "submit_form", explicit, "k4"
    )
    assert await account(pool, run_id, "dan") == 55
    assert len(await rows(pool, "SELECT 1 FROM web_orders WHERE run_id=%s", (run_id,))) == 1
    assert len(await rows(pool, "SELECT 1 FROM wallet_ledger WHERE run_id=%s", (run_id,))) == 1, (
        "One debit"
    )
    await replay_matches(pool, run_id)


async def test_reused_request_id_with_changed_payload_conflicts(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 100}, sites=[STORE])
    tools = WebTools(pool, run_id, "dan")
    arguments = {
        "url": "https://tinroof.com/items",
        "action": "add_to_cart",
        "fields": {"item_id": "commuter"},
        "request_id": "same-id",
    }
    assert form(await tools.execute("submit_form", arguments, "a"))["outcome"] == "done"
    for change in (
        {"fields": {"item_id": "lamp"}},
        {"fields": {"item_id": "commuter", "quantity": 2}},
        {"action": "contact", "fields": {"message": "Hello"}},
    ):
        conflict = await tools.execute("submit_form", {**arguments, **change}, "b")
        assert conflict["ok"] is False and conflict["error"]["status"] == 409
        assert "different form" in conflict["error"]["detail"]
    # A request ID belongs to one owner, so another owner's identical ID is a separate request.
    other = await WebTools(pool, run_id, "mei").execute("submit_form", arguments, "a")
    assert form(other)["outcome"] == "done"
    carts = await rows(
        pool, "SELECT owner_id,quantity FROM web_carts WHERE run_id=%s ORDER BY owner_id", (run_id,)
    )
    assert carts == [{"owner_id": "dan", "quantity": 1}, {"owner_id": "mei", "quantity": 1}]


async def test_stock_race_between_two_owners(pool):
    limited = {**STORE, "items": [{"id": "lamp", "title": "Front lamp", "price": 5, "stock": 1}]}
    for _ in range(4):
        run_id, _manifest = await make_run(pool, allowance={"dan": 50, "mei": 50}, sites=[limited])
        tools = {owner: WebTools(pool, run_id, owner) for owner in ("dan", "mei")}
        for owner, adapter in tools.items():
            added = await adapter.execute(
                "submit_form",
                {
                    "url": "https://tinroof.com/items",
                    "action": "add_to_cart",
                    "fields": {"item_id": "lamp"},
                },
                f"add-{owner}",
            )
            assert form(added)["outcome"] == "done"
        results = await asyncio.gather(
            *(
                adapter.execute(
                    "submit_form",
                    {"url": "https://tinroof.com/cart", "action": "checkout"},
                    f"pay-{owner}",
                )
                for owner, adapter in tools.items()
            )
        )
        outcomes = sorted(form(result)["outcome"] for result in results)
        assert outcomes == ["done", "rejected"]
        loser = next(form(r) for r in results if form(r)["outcome"] == "rejected")
        assert "sold out" in loser["text"]
        balances = sorted([await account(pool, run_id, "dan"), await account(pool, run_id, "mei")])
        assert balances == [90, 100], "Only the winner pays, including the delivery fee."
        stock = await rows(pool, "SELECT remaining FROM web_inventory WHERE run_id=%s", (run_id,))
        assert stock == [{"remaining": 0}]
        await replay_matches(pool, run_id)


async def test_decline_beyond_approval_then_approval_succeeds(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 10}, sites=[STORE])
    tools = WebTools(pool, run_id, "dan")
    declined = form(await buy(tools))
    assert declined["outcome"] == "declined"
    assert declined["text"].count(
        "Payment declined. Your saved card needs the account holder's approval for this amount."
    )
    assert await account(pool, run_id, "dan") == 100
    assert (await rows(pool, "SELECT 1 FROM web_orders WHERE run_id=%s", (run_id,))) == []
    events = await rows(
        pool, "SELECT * FROM run_events WHERE run_id=%s AND kind='web_spend_declined'", (run_id,)
    )
    assert len(events) == 1 and events[0]["owner_id"] == "dan"
    assert events[0]["payload"]["amount"] == 45 and events[0]["payload"]["remaining"] == 10
    assert events[0]["payload"]["reason"] == "approval_required"
    assert "Commuter bike" in declined["text"], "The cart stays for another attempt."

    limits = await approve(pool, run_id, "dan", 35, key="owner-yes")
    assert limits == {"allowance": 10, "approved": 35, "spent": 0, "remaining": 45}
    # The same approval saved twice adds once, and a changed amount under its key conflicts.
    assert await approve(pool, run_id, "dan", 35, key="owner-yes") == limits
    with pytest.raises(WorldError) as error:
        await approve(pool, run_id, "dan", 36, key="owner-yes")
    assert error.value.status_code == 409

    paid = form(
        await tools.execute(
            "submit_form",
            {"url": "https://tinroof.com/cart", "action": "checkout"},
            "pay-again",
        )
    )
    assert paid["outcome"] == "done"
    assert await account(pool, run_id, "dan") == 55
    async with pool.connection() as conn:
        assert await wallet.limits(conn, run_id, "dan") == {
            "allowance": 10,
            "approved": 35,
            "spent": 45,
            "remaining": 0,
        }
    # The limit is used up, so the next purchase asks for approval again.
    again = form(await buy(tools, item="lamp"))
    assert again["outcome"] == "declined"
    assert (
        len(
            await rows(
                pool,
                "SELECT 1 FROM run_events WHERE run_id=%s AND kind='web_spend_declined'",
                (run_id,),
            )
        )
        == 2
    )
    await replay_matches(pool, run_id)


async def test_decline_for_low_funds_within_approval(pool):
    run_id, _ = await make_run(
        pool, allowance={"dan": 500}, sites=[STORE], balances={"dan": 20, "mei": 100}
    )
    declined = form(await buy(WebTools(pool, run_id, "dan")))
    assert declined["outcome"] == "declined"
    assert "does not have enough funds" in declined["text"]
    assert await account(pool, run_id, "dan") == 20
    event = await rows(
        pool,
        "SELECT payload FROM run_events WHERE kind='web_spend_declined' AND run_id=%s",
        (run_id,),
    )
    assert event[0]["payload"]["reason"] == "insufficient_funds"


async def test_cancel_order_refunds_without_restoring_spending_room(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 50}, sites=[STORE])
    tools = WebTools(pool, run_id, "dan")
    paid = form(await buy(tools))
    order_id = re.search(r"ORD-[0-9A-F]{10}", paid["text"])[0]
    assert await account(pool, run_id, "dan") == 55
    cancel = {
        "url": f"https://tinroof.com/orders/{order_id}",
        "action": "cancel_order",
        "fields": {"order_id": order_id},
    }
    done = form(await tools.execute("submit_form", cancel, "x1"))
    assert "cancelled" in done["text"] and "Status: Cancelled" in done["text"]
    assert await account(pool, run_id, "dan") == 100
    stock = await rows(
        pool,
        "SELECT remaining FROM web_inventory WHERE run_id=%s AND entry_id='commuter'",
        (run_id,),
    )
    assert stock[0]["remaining"] == 2
    state = await rows(
        pool,
        "SELECT status FROM clock_events WHERE run_id=%s AND key LIKE 'web-order%%'",
        (run_id,),
    )
    assert state == [{"status": "cancelled"}]
    # A second cancellation with a new ID cannot refund twice.
    form(await tools.execute("submit_form", cancel, "x2"))
    assert await account(pool, run_id, "dan") == 100
    async with pool.connection() as conn:
        limits = await wallet.limits(conn, run_id, "dan")
    assert limits["spent"] == 45 and limits["remaining"] == 5
    await replay_matches(pool, run_id)


async def test_listing_flow_and_site_messages(pool):
    run_id, _ = await make_run(pool, sites=[MARKET])
    dan = WebTools(pool, run_id, "dan")
    mei = WebTools(pool, run_id, "mei")
    url = "https://swapyard.com/listings"
    created = form(
        await dan.execute(
            "submit_form",
            {
                "url": url,
                "action": "create_listing",
                "fields": {
                    "title": "Blue road bike",
                    "price": "120",
                    "description": "Runs well.",
                    "category": "bikes",
                },
            },
            "l1",
        )
    )
    assert "Your listing is live." in created["text"] and "Price: $120" in created["text"]
    listing_id = re.search(r"LST-[0-9A-F]{10}", created["url"])[0]
    assert await account(pool, run_id, "dan") == 100, "A listing moves no money."
    messages = await rows(
        pool, "SELECT * FROM run_events WHERE run_id=%s AND kind='site_message'", (run_id,)
    )
    assert len(messages) == 1 and messages[0]["owner_id"] == "dan"
    assert messages[0]["payload"]["message_kind"] == "listing"
    assert messages[0]["payload"]["listing_id"] == listing_id

    changed = form(
        await dan.execute(
            "submit_form",
            {
                "url": created["url"],
                "action": "update_listing",
                "fields": {"listing_id": listing_id, "price": 95},
            },
            "l2",
        )
    )
    assert "Price: $95" in changed["text"]

    # Another owner sees the listing without the seller's name, and can only send a message.
    public = form(await mei.execute("open_page", {"url": url}, "o1"))
    assert "Blue road bike | $95 | private seller" in public["text"]
    assert "dan" not in public["text"].lower()
    detail = form(await mei.execute("open_page", {"url": created["url"]}, "o2"))
    assert "action contact" in detail["text"] and "update_listing" not in detail["text"]
    refused = await mei.execute(
        "submit_form",
        {
            "url": created["url"],
            "action": "remove_listing",
            "fields": {"listing_id": listing_id},
        },
        "o3",
    )
    assert refused["ok"] is False and refused["error"]["status"] == 404
    asked = form(
        await mei.execute(
            "submit_form",
            {
                "url": created["url"],
                "action": "contact",
                "fields": {"listing_id": listing_id, "message": "Would you take 80?"},
            },
            "o4",
        )
    )
    assert "We received your message" in asked["text"]
    mine = form(await dan.execute("submit_form", {"url": url, "action": "view_listings"}, "o5"))
    assert "Blue road bike | $95 | yours" in mine["text"]

    # A later review decides the sale: it credits through the wallet and closes the listing.
    async with pool.connection() as conn, conn.transaction():
        receipt = await wallet.adjust(
            conn, run_id, "dan", 80, "Sale of Blue road bike", "review:sale:1", "director"
        )
        await sim_web.update_site(
            conn,
            run_id,
            "swapyard",
            {"listings": [{"id": listing_id, "status": "sold"}]},
            operation_key="review:sale:1:site",
        )
    assert receipt["balance"] == 180
    closed = form(await dan.execute("open_page", {"url": created["url"]}, "o6"))
    assert "Status: sold" in closed["text"] and "update_listing" not in closed["text"]
    assert "Blue road bike" not in form(await mei.execute("open_page", {"url": url}, "o7"))["text"]

    removed = await dan.execute(
        "submit_form",
        {
            "url": url,
            "action": "create_listing",
            "fields": {"title": "Old lamp", "price": 3},
        },
        "l3",
    )
    second = re.search(r"LST-[0-9A-F]{10}", form(removed)["url"])[0]
    form(
        await dan.execute(
            "submit_form",
            {"url": url, "action": "remove_listing", "fields": {"listing_id": second}},
            "l4",
        )
    )
    assert "Old lamp" not in form(await dan.execute("open_page", {"url": url}, "o8"))["text"]
    await replay_matches(pool, run_id)


async def test_bill_payment_flow(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 20}, sites=[PORTAL])
    dan = WebTools(pool, run_id, "dan")
    url = "https://agentcare.com/bills"
    bills = form(await dan.execute("open_page", {"url": url}, "o1"))
    assert "November plan | $50 | due Sat 23:59 | unpaid | bill_id: plan-nov" in bills["text"]
    assert "action pay_bill" in bills["text"]
    pay = {"url": url, "action": "pay_bill", "fields": {"bill_id": "plan-nov"}}
    declined = form(await dan.execute("submit_form", pay, "p1"))
    assert declined["outcome"] == "declined" and "approval" in declined["text"]
    async with pool.connection() as conn:
        assert (await sim_web.bill_status(conn, run_id, "agentcare", "plan-nov"))[
            "status"
        ] == "unpaid"

    # Another owner cannot read or pay this bill.
    mei = WebTools(pool, run_id, "mei")
    assert (
        "You have no bills here."
        in form(await mei.execute("open_page", {"url": url}, "m1"))["text"]
    )
    refused = await mei.execute("submit_form", pay, "m2")
    assert refused["ok"] is False and refused["error"]["status"] == 404

    await approve(pool, run_id, "dan", 30)
    paid = form(await dan.execute("submit_form", pay, "p2"))
    assert paid["outcome"] == "done" and "payment of $50 was received" in paid["text"]
    assert await account(pool, run_id, "dan") == 50
    async with pool.connection() as conn:
        saved = await sim_web.bill_status(conn, run_id, "agentcare", "plan-nov")
    assert saved["status"] == "paid"
    # A second attempt under a new ID changes nothing, and a site update cannot reopen the bill.
    again = form(await dan.execute("submit_form", pay, "p3"))
    assert again["outcome"] == "rejected" and "already paid" in again["text"]
    assert await account(pool, run_id, "dan") == 50
    async with pool.connection() as conn, conn.transaction():
        await sim_web.update_site(
            conn,
            run_id,
            "agentcare",
            {"bills": [{"id": "plan-nov", "amount": 75}]},
            operation_key="reprice",
        )
    async with pool.connection() as conn:
        saved = await sim_web.bill_status(conn, run_id, "agentcare", "plan-nov")
    assert (saved["status"], saved["amount"]) == ("paid", 50)
    await replay_matches(pool, run_id)


async def test_wallet_never_negative_and_replays(pool):
    run_id, _ = await make_run(pool, sites=[])
    with pytest.raises(SiteError) as error:
        await adjust(pool, run_id, "dan", -101)
    assert error.value.status_code == 409
    assert await account(pool, run_id, "dan") == 100
    results = await asyncio.gather(
        *(adjust(pool, run_id, "dan", -30) for _ in range(10)), return_exceptions=True
    )
    assert sum(isinstance(item, dict) for item in results) == 3
    assert sum(isinstance(item, SiteError) for item in results) == 7
    assert await account(pool, run_id, "dan") == 10
    ledger = await rows(
        pool,
        "SELECT balance_after FROM wallet_ledger WHERE run_id=%s ORDER BY balance_after DESC",
        (run_id,),
    )
    assert [entry["balance_after"] for entry in ledger] == [70, 40, 10]
    for bad in (0, 1.5, "5", True, 10**10):
        with pytest.raises(WorldError):
            await adjust(pool, run_id, "dan", bad)
    await replay_matches(pool, run_id)


async def test_wallet_adjust_is_idempotent_and_notifies_only_its_owner(pool):
    run_id, _ = await make_run(pool, sites=[])
    first = await adjust(pool, run_id, "dan", 25, key="gift", memo="Birthday money")
    assert first["balance"] == 125
    assert await adjust(pool, run_id, "dan", 25, key="gift", memo="Birthday money") == first
    assert await account(pool, run_id, "dan") == 125
    for change in (
        {"delta": 26, "memo": "Birthday money"},
        {"delta": 25, "memo": "Other"},
    ):
        with pytest.raises(WorldError) as error:
            await adjust(pool, run_id, "dan", change["delta"], key="gift", memo=change["memo"])
        assert error.value.status_code == 409
    with pytest.raises(WorldError):
        await adjust(pool, run_id, "mei", 25, key="gift", memo="Birthday money")
    events = await rows(
        pool,
        "SELECT owner_id,payload FROM run_events WHERE run_id=%s AND kind='world_changed'",
        (run_id,),
    )
    assert len(events) == 1 and events[0]["owner_id"] == "dan"
    assert events[0]["payload"] == {
        "kind": "balance_changed",
        "delta": 25,
        "balance": 125,
        "memo": "Birthday money",
    }
    # A failed debit inside a caller's transaction leaves that transaction usable.
    async with pool.connection() as conn, conn.transaction():
        with pytest.raises(SiteError):
            await wallet.adjust(conn, run_id, "dan", -999, "Too much", "too-much", "director")
        await wallet.adjust(conn, run_id, "dan", 1, "Still works", "still-works", "director")
    assert await account(pool, run_id, "dan") == 126
    await replay_matches(pool, run_id)


async def test_booking_and_application_flows(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 40, "mei": 40}, sites=[BOOKING, GIG])
    dan, mei = WebTools(pool, run_id, "dan"), WebTools(pool, run_id, "mei")
    slots = "https://greenchair.com/slots"
    page = form(await dan.execute("open_page", {"url": slots}, "o1"))
    assert "Haircut | Saturday 10:00 | $20 | 1 open | slot_id: sat-10" in page["text"]
    book = {"url": slots, "action": "book", "fields": {"slot_id": "sat-10"}}
    results = await asyncio.gather(
        dan.execute("submit_form", book, "b1"), mei.execute("submit_form", book, "b1")
    )
    assert sorted(form(result)["outcome"] for result in results) == ["done", "rejected"]
    winner, loser = (dan, mei) if form(results[0])["outcome"] == "done" else (mei, dan)
    owner = "dan" if winner is dan else "mei"
    assert await account(pool, run_id, owner) == 80
    booking_id = re.search(
        r"BKG-[0-9A-F]{10}",
        form(await winner.execute("open_page", {"url": "https://greenchair.com/bookings"}, "o2"))[
            "text"
        ],
    )[0]
    cancel = {
        "url": "https://greenchair.com/bookings",
        "action": "cancel_booking",
        "fields": {"booking_id": booking_id},
    }
    refused = await loser.execute("submit_form", cancel, "c0")
    assert refused["ok"] is False and refused["error"]["status"] == 404
    assert "cancelled" in form(await winner.execute("submit_form", cancel, "c1"))["text"]
    form(await winner.execute("submit_form", cancel, "c2"))
    assert await account(pool, run_id, owner) == 100, "One refund only."
    assert "1 open" in form(await loser.execute("open_page", {"url": slots}, "o3"))["text"]

    posting = "https://quickhands.com/postings/movers"
    apply = {
        "url": posting,
        "action": "apply",
        "fields": {"posting_id": "movers", "message": "I have a van and free weekends."},
    }
    sent = form(await dan.execute("submit_form", apply, "a1"))
    assert "application was sent" in sent["text"]
    assert form(await dan.execute("submit_form", apply, "a2"))["outcome"] == "rejected"
    events = await rows(
        pool,
        "SELECT payload FROM run_events WHERE run_id=%s AND kind='site_message' "
        "AND payload->>'message_kind'='application'",
        (run_id,),
    )
    assert len(events) == 1 and events[0]["payload"]["posting_id"] == "movers"
    await replay_matches(pool, run_id)


async def test_hostile_site_text_is_escaped_in_html(app, pool):
    hostile = '<script>alert("x")</script><img src=x onerror=alert(1)>"\'>'
    spec = {
        "site_id": "oddshop",
        "name": hostile[:70],
        "kind": "marketplace",
        "tagline": hostile,
        "description": hostile,
        "items": [
            {
                "id": "thing",
                "title": hostile,
                "price": 1,
                "stock": 1,
                "category": hostile[:70],
                "description": hostile,
            }
        ],
        "policies": [hostile],
        "pages": [{"id": "about", "title": hostile, "body": hostile}],
    }
    run_id, manifest = await make_run(pool, allowance={"dan": 50}, sites=[spec])
    dan = WebTools(pool, run_id, "dan")
    listing = form(
        await dan.execute(
            "submit_form",
            {
                "url": "https://oddshop.com/listings",
                "action": "create_listing",
                "fields": {"title": hostile, "price": 1, "description": hostile},
            },
            "l1",
        )
    )
    listing_id = re.search(r"LST-[0-9A-F]{10}", listing["url"])[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        for path in (
            "",
            "/",
            "/items",
            "/items/thing",
            "/policies",
            "/pages/about",
            "/listings",
            f"/listings/{listing_id}",
            "/contact",
            "/%3Cscript%3Ealert(1)%3C%2Fscript%3E",
            '/items/"><script>alert(1)</script>',
        ):
            response = await client.get(f"/sim/{run_id}/oddshop{path}", params={"owner": "dan"})
            assert response.status_code in {200, 404}, (path, response.text)
            body = response.text
            assert "<script" not in body.lower(), path
            assert "<img" not in body.lower(), path
            assert "onerror=alert(1)>" not in body or "&lt;img" in body, path
            assert "javascript:" not in body.lower(), path
            assert " action=" not in body and " method=" not in body, path
            assert "script-src" not in response.headers["content-security-policy"]
            assert "default-src 'none'" in response.headers["content-security-policy"]
            assert response.headers["cache-control"] == "no-store"
        escaped = (await client.get(f"/sim/{run_id}/oddshop/items/thing")).text
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in escaped
    # The assistant's copy is plain text. It is shown as data and never interpreted.
    text = form(await dan.execute("open_page", {"url": "https://oddshop.com/items/thing"}, "o"))
    assert hostile in text["text"]
    # A mapped link keeps the local observer path, even with odd query text from a site URL.
    built = sim_web.render_html(
        sim_web.Page(url="u", title="t", nav=[sim_web.Link('"><b>', "javascript:alert(1)")])
    )
    assert "<b>" not in built and 'href="' not in built


@pytest.mark.parametrize(
    "url",
    [
        "https://nowhere.com/",
        "https://www.bigshop.com/cart",
        "http://localhost:8000/api/v1/observer/runs",
        "http://127.0.0.1/",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://tinroof.com.evil.test/",
        "https://bad_host!.example/",
        "not a url at all",
    ],
)
async def test_unknown_url_gets_a_plain_not_found_page(pool, url):
    run_id, _ = await make_run(pool, sites=[STORE])
    tools = WebTools(pool, run_id, "dan")
    result = form(await tools.execute("open_page", {"url": url}, "o1"))
    assert result["title"] == "Page not found"
    assert "We can't find" in result["text"]
    assert not BANNED_WORDS.search(result["text"].replace(url, ""))
    submitted = form(
        await tools.execute(
            "submit_form", {"url": url, "action": "contact", "fields": {"message": "Hi"}}, "s1"
        )
    )
    assert submitted["title"] == "Page not found"
    assert (await rows(pool, "SELECT 1 FROM web_requests WHERE run_id=%s", (run_id,))) == []


async def test_unknown_path_action_and_bad_forms_on_a_known_site(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 50}, sites=[STORE])
    tools = WebTools(pool, run_id, "dan")
    missing = form(await tools.execute("open_page", {"url": "https://tinroof.com/nope/x/y"}, "o"))
    assert missing["title"] == "Page not found" and "Tin Roof Cycles" in missing["text"]
    for path in ("/items/none", "/orders/ORD-NONE", "/pages/none", "/listings", "/bills", "/slots"):
        page = form(await tools.execute("open_page", {"url": f"https://tinroof.com{path}"}, "o"))
        assert page["title"] == "Page not found", path
    # The www form of an address reaches the same site.
    assert (
        form(await tools.execute("open_page", {"url": "www.tinroof.com/items"}, "o"))["title"]
        == "Catalog"
    )
    unsupported = await tools.execute(
        "submit_form",
        {"url": "https://tinroof.com/", "action": "pay_bill", "fields": {"bill_id": "x"}},
        "u1",
    )
    assert unsupported["ok"] is False and "does not offer" in unsupported["error"]["detail"]
    invalid = await tools.execute(
        "submit_form",
        {
            "url": "https://tinroof.com/",
            "action": "add_to_cart",
            "fields": {"item_id": "commuter", "quantity": 500, "colour": "red"},
        },
        "u2",
    )
    assert invalid["ok"] is False and invalid["error"]["status"] == 422
    assert "quantity" in invalid["error"]["detail"] and "colour" in invalid["error"]["detail"]
    too_many = await tools.execute(
        "submit_form",
        {
            "url": "https://tinroof.com/",
            "action": "add_to_cart",
            "fields": {"item_id": "lamp", "quantity": 2},
        },
        "u3",
    )
    assert (
        form(too_many)["outcome"] == "rejected" and "only 1 of Front lamp" in form(too_many)["text"]
    )
    empty = form(
        await tools.execute(
            "submit_form", {"url": "https://tinroof.com/cart", "action": "checkout"}, "u4"
        )
    )
    assert empty["outcome"] == "rejected" and "cart is empty" in empty["text"]
    assert (await tools.execute("submit_form", {"url": "x"}, "u5"))["ok"] is False
    assert (await tools.execute("nope", {}, "u6"))["error"]["status"] == 422
    assert (await tools.execute("open_page", {"url": "x", "extra": 1}, "u7"))["ok"] is False
    assert (await tools.execute("open_page", {"url": "x"}, ""))["ok"] is False
    paused = await Director(pool).control(run_id, "pause")
    assert paused["status"] == "paused"
    stopped = await tools.execute(
        "submit_form",
        {
            "url": "https://tinroof.com/items",
            "action": "add_to_cart",
            "fields": {"item_id": "commuter"},
        },
        "u8",
    )
    assert stopped["ok"] is False
    assert stopped["error"]["detail"] == "The site is not available right now. Try again later."


async def test_search_ranks_sites_and_a_miss_asks_for_one_new_site(pool):
    run_id, _ = await make_run(pool, sites=[STORE, MARKET, GIG], max_sites=4)
    pawn = {
        "site_id": "goldenpawn",
        "name": "Golden Pawn",
        "kind": "marketplace",
        "tagline": "A pawn shop for quick loans",
    }
    calls = []

    def next_plan(run, owner_id, query, now_s):
        calls.append((run["id"], owner_id, query, now_s))
        return {"sites": [pawn]} if "pawn" in query else {"sites": []}

    class Brain:
        async def site_for_search(self, run, owner_id, query, now_s):
            # No database lock or open transaction may wrap this model request.
            async with pool.connection() as conn:
                await conn.execute(
                    "SELECT 1 FROM environment_runs WHERE id=%s FOR UPDATE NOWAIT", (run["id"],)
                )
            return next_plan(run, owner_id, query, now_s)

    # With no brain, a miss is an ordinary empty result and costs nothing.
    plain = WebTools(pool, run_id, "dan")
    missed = form(await plain.execute("web_search", {"query": "pawn shop near me"}, "s0"))
    assert missed["text"].startswith('No results for "pawn shop near me"')

    tools = WebTools(pool, run_id, "dan", director_brain=Brain())
    # A common word such as "sell" fits a site that says it, so no request goes out.
    assert "Swapyard" in form(await tools.execute("web_search", {"query": "sell"}, "s1"))["text"]
    assert calls == []
    # A stop word has nothing to search, so no request goes out.
    assert (
        "No results" in form(await tools.execute("web_search", {"query": "the of"}, "s2"))["text"]
    )
    assert calls == []

    new = form(await tools.execute("web_search", {"query": "pawn shop near me"}, "s3"))
    assert "Golden Pawn: https://goldenpawn.com/" in new["text"]
    assert [call[1:] for call in calls] == [("dan", "pawn shop near me", 0)]
    page = form(await tools.execute("open_page", {"url": "https://goldenpawn.com/"}, "o1"))
    assert "A pawn shop for quick loans" in page["text"]
    # The saved site now answers the same search, with no second request.
    assert (
        "Golden Pawn"
        in form(await tools.execute("web_search", {"query": "pawn shop"}, "s4"))["text"]
    )
    assert len(calls) == 1
    other = WebTools(pool, run_id, "mei", director_brain=Brain())
    assert "Golden Pawn" in form(await other.execute("web_search", {"query": "pawn"}, "s5"))["text"]
    assert len(calls) == 1

    # max_sites is 4 and the run now holds 4, so nothing more is asked or saved.
    full = form(await tools.execute("web_search", {"query": "violin lessons"}, "s6"))
    assert "No results" in full["text"]
    assert len(calls) == 1
    async with pool.connection() as conn, conn.transaction():
        with pytest.raises(SiteError):
            await sim_web.create_site(
                conn, run_id, {**pawn, "site_id": "another", "name": "Another Pawn"}
            )
    assert len(await rows(pool, "SELECT 1 FROM web_sites WHERE run_id=%s", (run_id,))) == 4


async def test_search_miss_with_scripted_brain_and_an_empty_answer(pool):
    run_id, _ = await make_run(pool, sites=[STORE], max_sites=5)
    empty = ScriptedDirectorBrain()
    tools = WebTools(pool, run_id, "dan", director_brain=empty)
    first = form(await tools.execute("web_search", {"query": "violin lessons"}, "s1"))
    assert "No results" in first["text"]
    assert len(empty.calls) == 1
    # The same empty question is not asked again, whatever its spacing or capital letters.
    form(await tools.execute("web_search", {"query": "  Violin   LESSONS "}, "s2"))
    assert len(empty.calls) == 1

    # A proposed site with a taken address or a bad definition saves nothing.
    bad = ScriptedDirectorBrain(
        {"site_for_search": lambda *args: {"sites": [{**STORE, "name": "Other Name"}]}}
    )
    taken = WebTools(pool, run_id, "dan", director_brain=bad)
    assert "No results" in form(await taken.execute("web_search", {"query": "banjo"}, "s3"))["text"]
    site = await rows(
        pool, "SELECT name FROM web_sites WHERE site_id='tinroof' AND run_id=%s", (run_id,)
    )
    assert site == [{"name": "Tin Roof Cycles"}]
    # A brain failure propagates, and a later identical search may ask again within its bound.
    attempts = []

    class Failing:
        async def site_for_search(self, *args):
            attempts.append(args)
            raise RuntimeError("provider refused")

    failing = WebTools(pool, run_id, "dan", director_brain=Failing())
    for _ in range(sim_web.MAX_SEARCH_ASK_ATTEMPTS):
        with pytest.raises(RuntimeError):
            await failing.execute("web_search", {"query": "harp tuning"}, "s4")
    assert (
        "No results"
        in form(await failing.execute("web_search", {"query": "harp tuning"}, "s4"))["text"]
    )
    assert len(attempts) == sim_web.MAX_SEARCH_ASK_ATTEMPTS


async def test_site_versioning_and_patch_rules(pool):
    run_id, _ = await make_run(pool, allowance={"dan": 500}, sites=[STORE, PORTAL])
    dan = WebTools(pool, run_id, "dan")
    patch = {
        "tagline": "Now open on Sundays",
        "items": [
            {"id": "commuter", "price": 30},
            {"id": "lamp", "remove": True},
            {"id": "helmet", "title": "Helmet", "price": 12, "stock": 3},
        ],
        "fees": {"delivery": 0},
    }
    async with pool.connection() as conn, conn.transaction():
        first = await sim_web.update_site(conn, run_id, "tinroof", patch, operation_key="u1")
        assert first["version"] == 2
        # The same key and patch return the same site with no new version.
        assert (await sim_web.update_site(conn, run_id, "tinroof", patch, operation_key="u1"))[
            "version"
        ] == 2
        with pytest.raises(SiteError):
            await sim_web.update_site(conn, run_id, "tinroof", {"tagline": "x"}, operation_key="u1")
        for bad in (
            {"site_id": "other"},
            {"kind": "gig"},
            {"unknown": 1},
            {"items": [{"id": "new-item", "title": "No price"}]},
            {"bills": [{"id": "b", "owner_id": "nobody", "title": "T", "amount": 1}]},
            {"name": "Amazon Bikes"},
            "not a patch",
        ):
            with pytest.raises(SiteError):
                await sim_web.update_site(
                    conn, run_id, "tinroof", bad, operation_key=f"bad:{uuid4()}"
                )
        with pytest.raises(SiteError) as error:
            await sim_web.update_site(conn, run_id, "missing", {}, operation_key="u3")
        assert error.value.status_code == 404
    versions = await rows(
        pool,
        "SELECT version,spec->>'tagline' AS tagline FROM web_site_versions "
        "WHERE run_id=%s AND site_id='tinroof' ORDER BY version",
        (run_id,),
    )
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["tagline"] == "Used bikes and honest repairs"
    catalog = form(await dan.execute("open_page", {"url": "https://tinroof.com/items"}, "o1"))
    assert "Commuter bike | $30 | 2 in stock" in catalog["text"], "A price patch keeps live stock."
    assert "Helmet | $12 | 3 in stock" in catalog["text"] and "Front lamp" not in catalog["text"]
    # Search uses the new version's words.
    found = form(await dan.execute("web_search", {"query": "helmet"}, "s1"))
    assert "Tin Roof Cycles" in found["text"]
    # The same spec saved again is fine. A different spec on the address is refused.
    async with pool.connection() as conn, conn.transaction():
        await sim_web.create_site(conn, run_id, STORE, source="plot", enforce_limit=False)
        with pytest.raises(SiteError):
            await sim_web.create_site(
                conn, run_id, {**STORE, "tagline": "Different"}, enforce_limit=False
            )


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"site_id": "Has Space"},
        {"site_id": "-bad-"},
        {"name": ""},
        {"name": "x" * 81},
        {"name": "Uber Eats Lite"},
        {"site_id": "amazon"},
        {"kind": "casino"},
        {"tagline": "This simulated shop is part of a scenario."},
        {"description": "Bad\x00text"},
        {"eta_minutes": 0},
        {"items": [{"id": "x", "title": "T", "price": -1}]},
        {"items": [{"id": "x", "title": "T", "price": "5"}]},
        {"items": [{"id": "x", "title": "T", "price": 1, "stock": -1}]},
        {"items": [{"id": "../x", "title": "T", "price": 1}]},
        {"items": [{"id": "x", "title": "A", "price": 1}, {"id": "x", "title": "B", "price": 1}]},
        {"items": [{"id": f"i{n}", "title": "T", "price": 1} for n in range(61)]},
        {"policies": ["p"] * 11},
    ],
)
def test_site_spec_is_bounded(change):
    with pytest.raises(ValidationError):
        SiteSpec.model_validate({**STORE, **change})


async def test_observer_access_and_portal_list(app, pool):
    run_id, manifest = await make_run(pool, allowance={"dan": 10}, sites=[STORE, PORTAL])
    dan = WebTools(pool, run_id, "dan")
    await buy(dan)  # Declined, so the portal has a declined payment to show.
    await approve(pool, run_id, "dan", 90)
    await dan.execute(
        "submit_form", {"url": "https://tinroof.com/cart", "action": "checkout"}, "pay"
    )
    await dan.execute(
        "submit_form",
        {
            "url": "https://tinroof.com/contact",
            "action": "contact",
            "fields": {"message": "Hello there"},
        },
        "hi",
    )
    page_path = f"/sim/{run_id}/tinroof/cart"
    json_path = f"/api/v1/observer/runs/{run_id}/web"
    token = manifest["participants"][0]["token"]

    def client(peer="127.0.0.1", base="http://127.0.0.1:8000", headers=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
            base_url=base,
            headers=headers,
        )

    async with client() as local:
        index = await local.get(f"/sim/{run_id}")
        assert index.status_code == 200 and "Tin Roof Cycles" in index.text
        assert f"/sim/{run_id}/tinroof/" in index.text
        listed = await local.get(json_path)
        assert listed.status_code == 200, listed.text
        data = listed.json()
        assert [site["site_id"] for site in data["sites"]] == ["agentcare", "tinroof"]
        store = data["sites"][1]
        assert len(store["orders"]) == 1 and store["orders"][0]["owner_id"] == "dan"
        assert len(store["spend_declines"]) == 1
        assert store["spend_declines"][0]["payload"]["amount"] == 45
        assert store["messages"][0]["payload"]["body"] == "Hello there"
        assert store["observer_url"] == f"/sim/{run_id}/tinroof/"
        assert data["sites"][0]["bills"][0]["bill_id"] == "plan-nov"
        wallets = {item["owner_id"]: item for item in data["wallets"]}
        assert wallets["dan"]["balance"] == 55 and wallets["dan"]["spent"] == 45
        assert wallets["dan"]["remaining"] == 55
        # The observer can look at each person's view, and a read writes nothing.
        before = await rows(pool, "SELECT count(*) AS n FROM run_events WHERE run_id=%s", (run_id,))
        as_mei = await local.get(page_path, params={"owner": "mei"})
        assert as_mei.status_code == 200 and "Your cart is empty." in as_mei.text
        assert (await local.get(page_path)).status_code == 200
        assert "Choose a person" in (await local.get(page_path)).text
        assert (await local.get(page_path, params={"owner": "nobody"})).status_code == 404
        assert (await local.get(f"/sim/{run_id}/missing/")).status_code == 404
        assert (await local.get(f"/sim/{uuid4()}/tinroof/")).status_code == 404
        assert (await local.get(f"/sim/{uuid4()}")).status_code == 404
        assert (await local.get(f"/api/v1/observer/runs/{uuid4()}/web")).status_code == 404
        assert (await local.get(f"/sim/{run_id}/tinroof/no/such/page")).status_code == 404
        assert (await local.post(page_path)).status_code == 405
        after = await rows(pool, "SELECT count(*) AS n FROM run_events WHERE run_id=%s", (run_id,))
        assert before == after
        empty_run, _ = await make_run(pool, sites=[])
        empty = await local.get(f"/sim/{empty_run}")
        assert empty.status_code == 200 and "No sites were created" in empty.text
        assert (await local.get(f"/api/v1/observer/runs/{empty_run}/web")).json()["sites"] == []

    for path in (page_path, json_path, f"/sim/{run_id}"):
        async with client(peer="203.0.113.9") as remote:
            assert (await remote.get(path)).status_code == 401, path
        async with client(base="http://evil.test") as rebind:
            assert (await rebind.get(path)).status_code == 401, path
        async with client(headers={"Origin": "http://evil.test"}) as foreign:
            assert (await foreign.get(path)).status_code == 401, path
        async with client(headers={"Sec-Fetch-Site": "cross-site"}) as cross:
            assert (await cross.get(path)).status_code == 401, path
        async with client(headers={"Authorization": f"Bearer {token}"}) as agent:
            assert (await agent.get(path)).status_code == 403, path
        async with client(headers={"Authorization": "Bearer wrong"}) as wrong:
            assert (await wrong.get(path)).status_code == 401, path


async def test_a_run_with_no_clock_still_works_and_queues_nothing(pool):
    run_id, _ = await make_run(pool, sites=[STORE], clocked=False)
    tools = WebTools(pool, run_id, "dan")
    assert form(await buy(tools))["outcome"] == "declined", "No allowance means no approval."
    await approve(pool, run_id, "dan", 45)
    paid = form(
        await tools.execute(
            "submit_form", {"url": "https://tinroof.com/cart", "action": "checkout"}, "pay"
        )
    )
    assert paid["outcome"] == "done" and "On its way" in paid["text"]
    assert (await rows(pool, "SELECT 1 FROM clock_events WHERE run_id=%s", (run_id,))) == []
    order = await rows(pool, "SELECT update_key,sim_s FROM web_orders WHERE run_id=%s", (run_id,))
    assert order == [{"update_key": None, "sim_s": 0}]
    await replay_matches(pool, run_id)


async def test_model_sees_plain_words_only(pool):
    assert {name: description for name, (_, description) in WEB_TOOLS.items()} == {
        "web_search": "Search the web",
        "open_page": "Open a web page",
        "submit_form": "Fill in and send a form on a page",
    }
    run_id, _ = await make_run(
        pool,
        allowance={"dan": 500},
        sites=[STORE, MARKET, PORTAL, BOOKING, GIG],
    )
    tools = WebTools(pool, run_id, "dan")
    definitions = str(tools.definitions())
    assert not BANNED_WORDS.search(definitions)
    assert "run_id" not in definitions and "owner_id" not in definitions
    seen = []

    async def see(name, arguments, call_id):
        result = await tools.execute(name, arguments, call_id)
        seen.append(str(result))
        return result

    paths = [
        "tinroof.com/",
        "tinroof.com/items",
        "tinroof.com/items/commuter",
        "tinroof.com/cart",
        "tinroof.com/orders",
        "tinroof.com/contact",
        "tinroof.com/policies",
        "tinroof.com/none",
        "swapyard.com/",
        "swapyard.com/listings",
        "agentcare.com/",
        "agentcare.com/bills",
        "greenchair.com/",
        "greenchair.com/slots",
        "greenchair.com/bookings",
        "quickhands.com/",
        "quickhands.com/postings",
        "quickhands.com/postings/movers",
        "nowhere.com/",
    ]
    for number, path in enumerate(paths):
        await see("open_page", {"url": f"https://{path}"}, f"o{number}")
    await see("web_search", {"query": "bike"}, "s1")
    await see("web_search", {"query": "nothing matches this"}, "s2")
    steps = [
        ("tinroof", "add_to_cart", {"item_id": "commuter"}),
        ("tinroof", "view_cart", {}),
        ("tinroof", "search", {"q": "bike"}),
        ("tinroof", "checkout", {}),
        ("tinroof", "order_status", {}),
        ("tinroof", "contact", {"message": "Thanks"}),
        ("swapyard", "create_listing", {"title": "Blue chair", "price": 10}),
        ("swapyard", "view_listings", {}),
        ("agentcare", "view_bills", {}),
        ("agentcare", "pay_bill", {"bill_id": "plan-nov"}),
        ("greenchair", "book", {"slot_id": "sat-10"}),
        ("quickhands", "apply", {"posting_id": "movers", "message": "Hello"}),
        ("quickhands", "search", {"q": "moving"}),
    ]
    for number, (site, action, fields) in enumerate(steps):
        result = await see(
            "submit_form",
            {"url": f"https://{site}.example/", "action": action, "fields": fields},
            f"f{number}",
        )
        assert result["ok"], (action, result)
    declined = WebTools(pool, run_id, "mei")
    seen.append(str(await buy(declined)))
    for text in seen:
        # Tool results hold no identifiers of the run, and no words about the test around it.
        assert str(run_id) not in text
        assert not BANNED_WORDS.search(text), text
        assert "request_id" not in text and "sim_s" not in text and "owner_id" not in text
        assert len(text) < sim_web.TEXT_LIMIT + 500


async def test_tools_work_through_the_runtime_tool_shape(pool):
    """The runtime wraps each tool this way. The tool call ID becomes the request identity."""
    run_id, _ = await make_run(pool, allowance={"dan": 100}, sites=[STORE])
    adapter = WebTools(pool, run_id, "dan")
    wrapped = {}
    for name, (model, description) in adapter.tools.items():
        schema = create_model(
            f"{name}Call", __base__=model, tool_call_id=(Annotated[str, InjectedToolCallId], ...)
        )

        def bind(tool_name):
            async def execute(tool_call_id, **arguments):
                plain = json.loads(json.dumps(arguments, default=str))
                return await adapter.execute(tool_name, plain, tool_call_id)

            return execute

        wrapped[name] = StructuredTool(
            name=name, description=description, args_schema=schema, coroutine=bind(name)
        )

    async def call(name, args, call_id):
        message = await wrapped[name].ainvoke(
            {"type": "tool_call", "name": name, "args": args, "id": call_id}
        )
        return json.loads(message.content)

    found = await call("web_search", {"query": "bike"}, "c1")
    assert "tinroof.com" in found["data"]["text"]
    page = await call("open_page", {"url": "https://tinroof.com/items"}, "c2")
    assert "Commuter bike" in page["data"]["text"]
    added = await call(
        "submit_form",
        {
            "url": "https://tinroof.com/items",
            "action": "add_to_cart",
            "fields": {"item_id": "commuter", "quantity": 1},
        },
        "c3",
    )
    assert added["data"]["outcome"] == "done"
    checkout = {"url": "https://tinroof.com/cart", "action": "checkout"}
    first = await call("submit_form", checkout, "c4")
    assert first["data"]["outcome"] == "done"
    assert await call("submit_form", checkout, "c4") == first
    assert await account(pool, run_id, "dan") == 55


def test_a_balance_change_reads_as_plain_speech():
    notice = _notice(
        "world_changed",
        json.dumps(
            {"kind": "balance_changed", "delta": -45, "balance": 55, "memo": "Tin Roof order"}
        ),
    )
    assert notice == "Your owner's balance changed by -$45: Tin Roof order. It is now $55."
    assert not BANNED_WORDS.search(notice)
    credit = _notice("world_changed", json.dumps({"kind": "balance_changed", "delta": 80}))
    assert credit.startswith("Your owner's balance changed by +$80")


async def test_large_catalog_pages_stay_small(pool):
    many = {
        **STORE,
        "items": [
            {
                "id": f"item-{number}",
                "title": f"Item number {number} " + "x" * 120,
                "price": number,
                "stock": 1,
                "description": "y" * 2000,
            }
            for number in range(60)
        ],
    }
    run_id, _ = await make_run(pool, sites=[many])
    tools = WebTools(pool, run_id, "dan")
    first = form(await tools.execute("open_page", {"url": "https://tinroof.com/items"}, "o1"))
    assert "item-19" in first["text"] and "item-20" not in first["text"]
    assert "https://tinroof.com/items?page=2" in first["text"]
    assert len(first["text"]) <= sim_web.TEXT_LIMIT
    third = form(
        await tools.execute("open_page", {"url": "https://tinroof.com/items?page=3"}, "o2")
    )
    assert "item-59" in third["text"] and "page=4" not in third["text"]
