"""Application browser capability: real Chrome, HTTP, PostgreSQL and Agent graph."""

import asyncio
import json
import socket
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from test_deep_agent import ScriptedModel, call
from test_shop import ready_shop

from agentrpg.activation import ActivationEnvelope, OwnerMessage
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime, AgentRuntimeError, AgentTaskConflictError
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.main import create_app
from agentrpg.market import MarketService
from agentrpg.shop import ShopService
from agentrpg.shop_browser import ShopBrowser

pytest.importorskip("playwright")


@pytest_asyncio.fixture
async def shop_server(settings):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", proxy_headers=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:
            break
        if task.done():
            await task
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("The isolated storefront test server did not start.")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()


def private_client(server, manifest):
    member = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    return httpx.AsyncClient(
        base_url=server + "/api/v1/", headers={"Authorization": "Bearer " + member["token"]}
    )


def require_chrome():
    if not Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists():
        pytest.skip("This browser integration check needs installed Chrome.")


async def test_real_browser_search_review_checkout_and_final_order(shop_server, pool):
    require_chrome()
    manifest, run, task, listing = await ready_shop(pool)
    async with private_client(shop_server, manifest) as client:
        browser = ShopBrowser(client, run)
        try:
            opened = await browser.execute("open_shop", {}, "open")
            assert opened["ok"], (
                opened,
                browser.last_error_kind,
                browser.last_phase,
                browser.page_errors,
                browser.response_statuses,
                browser.console_errors,
            )
            assert any(c["target"] == f"add-{listing['id']}" for c in opened["data"]["controls"])
            search = await browser.execute(
                "shop_fill", {"target": "search", "value": "陶瓷"}, "search"
            )
            assert search["ok"] and "暖白陶瓷杯" in search["data"]["text"]
            assert (await browser.execute("shop_click", {"target": f"add-{listing['id']}"}, "add"))[
                "ok"
            ]
            review = await browser.execute("shop_click", {"target": "review-cart"}, "review")
            assert "确认这次购买" in review["data"]["text"]
            checkout = await browser.execute("shop_click", {"target": "place-order"}, "checkout")
            assert checkout["data"]["pending_ids"] and "处理中" in checkout["data"]["text"]
            await MarketService(pool).settle(run, 1, 42)
            await ShopService(pool).sync(run)
            final = await browser.execute("open_shop", {"view": "orders"}, "receipt")
            assert "已结算" in final["data"]["text"] and not final["data"]["pending_ids"]
            assert (await ShopService(pool).state(run, "alice"))["balance"] == 988
            assert "Bearer " not in json.dumps(final) and manifest["participants"][0][
                "token"
            ] not in json.dumps(final)
        finally:
            await browser.aclose()


def envelope(manifest, task):
    member = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    return ActivationEnvelope(
        run_id=manifest["run_id"],
        participant_id=member["agent_id"],
        activation_id=uuid4(),
        window_id=1,
        source_event_ids=[uuid4()],
        owner_messages=[
            OwnerMessage(
                message_id=task["owner_message_id"],
                content="通过商城网页购买 1 个陶瓷杯，按真实回执汇报。",
            )
        ],
    )


def agent(runtime, client, manifest, model, *, browser=True):
    member = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    return runtime.agent(
        model=model,
        board=BoardTools(client, manifest["cluster_id"]),
        environment=EnvironmentTools(
            client,
            manifest["run_id"],
            shop_browser=ShopBrowser(client, manifest["run_id"]) if browser else None,
        ),
        agent_id=member["agent_id"],
        session_id=member["session_id"],
        instructions="Use the browser storefront and actual receipts. Never repeat a purchase.",
        require_explicit_finish=True,
        max_model_calls=12,
    )


async def test_committed_checkout_survives_browser_and_graph_recreation_without_another_model_call(
    shop_server,
    pool,
    test_database_url,
):
    require_chrome()
    manifest, run, task, listing = await ready_shop(pool)
    activation = envelope(manifest, task)
    model = ScriptedModel(
        responses=[
            call("open_shop", {}, "open"),
            call("shop_click", {"target": f"add-{listing['id']}"}, "add"),
            call("shop_click", {"target": "review-cart"}, "review"),
            call("shop_click", {"target": "place-order"}, "checkout"),
        ]
    )
    async with private_client(shop_server, manifest) as client:
        async with AgentRuntime(test_database_url) as runtime:
            customer = agent(runtime, client, manifest, model)
            customer.environment.shop_browser.fail_after_committed_click = True
            with pytest.raises(AgentRuntimeError):
                await customer.run_activation(activation)
        assert len((await ShopService(pool).state(run, "alice"))["orders"]) == 1
        restored_model = ScriptedModel()
        async with AgentRuntime(test_database_url) as runtime:
            restored = agent(runtime, client, manifest, restored_model)
            result = await restored.run_activation(activation)
            assert result.status == "waiting" and result.pending_ids
            assert restored_model.calls == 0
            assert any(e["result"]["data"].get("replayed_effect") for e in restored.events)
        await MarketService(pool).settle(run, 1, 42)
        await ShopService(pool).sync(run)
        state = await ShopService(pool).state(run, "alice")
        assert len(state["orders"]) == 1 and state["balance"] == 988


async def test_unfinished_purchase_cannot_finish_and_profile_cannot_change_on_resume(
    shop_server,
    pool,
    test_database_url,
):
    manifest, _, task, _ = await ready_shop(pool)
    model = ScriptedModel(
        responses=[
            call("finish_activation", {"status": "finished"}, "wrong-finish"),
            call("wait_for_owner", {"text": "请确认后续安排。"}, "wait"),
        ]
    )
    activation = envelope(manifest, task)
    async with (
        private_client(shop_server, manifest) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        customer = agent(runtime, client, manifest, model)
        assert "submit_market_action" not in customer.environment.tools
        result = await customer.run_activation(activation)
        assert result.status == "waiting" and result.wait_for == ("owner_message",)
        assert model.calls == 2
        changed = agent(runtime, client, manifest, ScriptedModel(), browser=False)
        with pytest.raises(AgentTaskConflictError):
            await changed.run_activation(activation)


async def test_browser_refuses_remote_origins_observer_routes_and_other_run(shop_server, pool):
    manifest, run, _, _ = await ready_shop(pool)
    async with private_client(shop_server, manifest) as client:
        browser = ShopBrowser(client, run)
        assert browser.permitted(f"{shop_server}/shop?run={run}")
        assert not browser.permitted("https://amazon.com/")
        assert not browser.permitted(f"{shop_server}/shop?run={run}&observer=1")
        assert not browser.permitted(f"{shop_server}/api/v1/observer/runs/{run}/shop")
        assert not browser.permitted(f"{shop_server}/api/v1/runs/{uuid4()}/shop/state")
        assert not browser.permitted(f"{shop_server}/api/v1/runs/{run}/shop/state", "POST")


async def test_four_purchase_rounds_keep_receipts_and_owner_reports_across_runtime_recreation(
    shop_server,
    pool,
    test_database_url,
):
    require_chrome()
    from agentrpg.director import Director
    from agentrpg.shop_demo import shopping_scenario
    from agentrpg.shop_runtime import INSTRUCTIONS, run_shop
    from agentrpg.supervision import SupervisionPolicy

    director = Director(pool)
    manifest = await director.create_run(shopping_scenario(4))
    manifest["configuration"] = {
        "rounds": 4,
        "instructions": INSTRUCTIONS,
        "supervision": SupervisionPolicy(
            base_delay_seconds=0.02, max_delay_seconds=0.04
        ).model_dump(),
    }
    await ShopService(pool).enable(UUID(manifest["run_id"]))
    await director.control(UUID(manifest["run_id"]), "start")
    paused = await run_shop(
        pool,
        test_database_url,
        shop_server,
        manifest,
        stop_after_windows=3,
    )
    assert paused["run_status"] == "paused"
    assert paused["stop_reason"] == "invocation_window_limit"
    assert paused["invocation_completed_windows"] == 3
    await director.control(UUID(manifest["run_id"]), "resume")
    report = await run_shop(
        pool, test_database_url, shop_server, manifest, restart_every=4, inject=True
    )
    assert report["passed"], {"checks": report["checks"], "run_result": report["run_result"]}
    assert report["runtime_recreations"] >= 3
    assert report["committed_checkout_recoveries"] == 1
    assert report["supervision"]["recoveries"] == 1
    assert any(e["kind"] == "recovery_scheduled" for e in report["trajectory"])
    assert report["browser_trace_events"] >= 20
