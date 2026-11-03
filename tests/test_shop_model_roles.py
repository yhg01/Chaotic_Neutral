"""All-model role binding, declared owner terms and safe provider failure."""

import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_actor_mail_intent import Responses, review_schema
from test_deep_agent import ScriptedModel, board_client, make_agent

from agentrpg.actor import ActorContractError, ActorOutput, ModelActor, build_actor_view
from agentrpg.budget import BudgetExceeded
from agentrpg.deep_agent import AgentBudgetError, AgentRuntime
from agentrpg.director import Director, emit
from agentrpg.shop import ShopService
from agentrpg.shop_demo import PRODUCTS, shopping_model_scenario, shopping_scenario
from agentrpg.shop_model_roles import (
    SELLER_INSTRUCTIONS,
    SHOP_ACTOR_PROMPT,
    SHOP_ACTOR_REVIEW_PROMPT,
)
from agentrpg.shop_runtime import INSTRUCTIONS, run_shop, wait_for_shop_window
from agentrpg.shop_schema import OpenShoppingTaskRequest


@pytest.mark.parametrize("rounds", [1, 2, 5])
def test_model_owners_receive_price_stock_budget_and_future_request_constraints(rounds):
    spec = shopping_model_scenario(rounds)
    assert spec.content_hash() == shopping_model_scenario(rounds).content_hash()
    assert spec.content_hash() != shopping_scenario(rounds).content_hash()
    assert "现在不要采购" in spec.owners[0].request
    assert str(spec.owners[0].max_spend) in spec.owners[0].request
    for sku, _, price, _ in PRODUCTS:
        assert sku in spec.owners[0].request and sku in spec.owners[1].request
        assert str(price) in spec.owners[1].request
        resource = next(r for r in spec.resources if r.id == sku)
        assert str(price) in resource.content and resource.quantity == rounds + 2


async def test_all_model_actor_failure_pauses_without_falling_back_to_supplied_decisions(
    pool,
    test_database_url,
    monkeypatch,
):
    class FailingProvider(ScriptedModel):
        async def aclose(self):
            pass

    bindings = []

    def model_factory(key, model, **kwargs):
        bindings.append({"model": model, "role": kwargs["budget_role"], "budget": kwargs["budget"]})
        return FailingProvider(responses=[RuntimeError("Injected provider outage")])

    monkeypatch.setattr("agentrpg.shop_runtime.create_model", model_factory)
    director = Director(pool)
    manifest = await director.create_run(shopping_model_scenario(2))
    manifest["configuration"] = {
        "mode": "model-actors",
        "model_roles": "all",
        "rounds": 2,
        "instructions": INSTRUCTIONS,
        "browser_shop_owners": ["alice"],
        "instructions_by_owner": {"alice": INSTRUCTIONS, "bob": SELLER_INSTRUCTIONS},
        "actor_prompt": SHOP_ACTOR_PROMPT,
        "actor_review_prompt": SHOP_ACTOR_REVIEW_PROMPT,
    }
    run = UUID(manifest["run_id"])
    await ShopService(pool).enable(run)
    await director.control(run, "start")
    budget = object()
    result = await run_shop(
        pool,
        test_database_url,
        "http://127.0.0.1:1",
        manifest,
        api_key="local-test-fixture",
        binding=budget,
    )
    assert result["run_status"] == "paused" and not result["passed"]
    assert result["mode"] == "model_all_roles" and result["supplied_roles"] == []
    assert len(bindings) == 6 and all(b["budget"] is budget for b in bindings)
    assert [b["role"] for b in bindings].count("browser-shop-actor") == 2
    assert [b["role"] for b in bindings].count("browser-shop-actor-review") == 2
    assert {b["role"] for b in bindings} >= {"browser-shop-buyer", "browser-shop-seller"}
    assert not result["orders"] and not result["tasks"]
    assert all(j["status"] == "failed" and j["kind"] == "actor" for j in result["jobs"])
    assert not any(e["kind"] == "commerce_consent_changed" for e in result["trajectory"])


def purchase_view():
    spec = shopping_model_scenario(1)
    return build_actor_view(
        spec,
        "alice",
        spec.beats[1].cue,
        conversation=[{"role": "owner", "text": "先等待我的后续清单。"}],
        resources={"visible_item_ids": [p[0] for p in PRODUCTS]},
        market={"browser_shop_enabled": True, "shopping_tasks": []},
    )


REF = {"task_key": "purchase-0", "item_ids": ["ceramic-mug"], "quantity": 1, "max_total": 12}


async def test_current_purchase_cue_cannot_be_replaced_with_an_old_waiting_instruction():
    omitted = {"kind": "reply", "text": "继续等待我的清单，先不要下单。"}
    repaired = {
        "kind": "reply",
        "text": "现在请买一个陶瓷杯，最多 12 积分。",
        "shopping_task_requests": [{"kind": "open", "description": "买杯子", **REF}],
    }
    judge = {"accepted": True, "issues": [], "response_needed": True, "new_shopping_tasks": [REF]}
    reviewer = Responses([judge, judge])
    actor = ModelActor(
        Responses([omitted, repaired]),
        reviewer=reviewer,
        require_shopping_task_coverage_review=True,
    )
    result = await actor.respond(purchase_view())
    assert result.shopping_task_requests[0].task_key == "purchase-0"
    assert result.commerce_consent is None
    assert not actor.last_attempts[0]["accepted"]
    assert "new purchases need exact shopping tasks" in actor.last_attempts[0]["reason"]
    assert "new_shopping_tasks" in review_schema(reviewer)["required"]


@pytest.mark.parametrize(
    "review",
    [
        {"accepted": True, "issues": []},
        {"accepted": True, "issues": [], "response_needed": True, "new_shopping_tasks": [REF, REF]},
        {"accepted": True, "issues": [], "response_needed": False, "new_shopping_tasks": []},
    ],
)
async def test_missing_or_conflicting_independent_purchase_review_fails_closed(review):
    candidate = {
        "kind": "reply",
        "text": "请买一个杯子。",
        "shopping_task_requests": [{"kind": "open", "description": "买杯子", **REF}],
    }
    actor = ModelActor(
        Responses([candidate]),
        reviewer=Responses([review]),
        max_repairs=0,
        require_shopping_task_coverage_review=True,
    )
    with pytest.raises(ActorContractError):
        await actor.respond(purchase_view())


async def test_courtesy_has_no_new_purchase_and_does_not_renew_authority():
    view = purchase_view().model_copy(update={"cue": "谢谢，知道了。"})
    actor = ModelActor(
        Responses([{"kind": "wait"}]),
        reviewer=Responses(
            [{"accepted": True, "issues": [], "response_needed": False, "new_shopping_tasks": []}]
        ),
        require_shopping_task_coverage_review=True,
    )
    result = await actor.respond(view)
    assert result.kind == "wait" and result.commerce_consent is None
    assert not result.shopping_task_requests


async def test_standing_shop_schema_rejects_reissued_grant_without_silently_rewriting_it():
    current = purchase_view().model_copy(
        update={
            "market": {
                "browser_shop_enabled": True,
                "shopping_tasks": [],
                "consent": {"version": 1, "buy_budget": 12, "buy_items": ["ceramic-mug"]},
            }
        }
    )
    repeated = {
        "kind": "reply",
        "text": "我批准买杯子。",
        "commerce_consent": {"buy_budget": 12, "buy_items": ["ceramic-mug"]},
    }
    repaired = {
        "kind": "reply",
        "text": "现在买一个陶瓷杯，最多 12 积分。",
        "shopping_task_requests": [{"kind": "open", "description": "买杯子", **REF}],
    }
    model = Responses([repeated, repaired])
    actor = ModelActor(
        model,
        reviewer=Responses(
            [{"accepted": True, "issues": [], "response_needed": True, "new_shopping_tasks": [REF]}]
        ),
        require_shopping_task_coverage_review=True,
        preserve_shopping_consent=True,
    )
    result = await actor.respond(current)
    assert result.commerce_consent is None and result.shopping_task_requests
    assert not actor.last_attempts[0]["accepted"]
    assert actor.last_attempts[0]["generated_candidate"]["commerce_consent"] is not None
    assert review_schema(model)["properties"]["commerce_consent"]["type"] == "null"


async def test_duplicate_status_reply_is_repaired_to_wait_by_independent_stimulus_review():
    actor = ModelActor(
        Responses([{"kind": "reply", "text": "请继续关注成交记录。"}, {"kind": "wait"}]),
        reviewer=Responses(
            [{"accepted": True, "issues": [], "response_needed": False, "new_shopping_tasks": []}]
            * 2
        ),
        require_shopping_task_coverage_review=True,
    )
    result = await actor.respond(purchase_view().model_copy(update={"cue": "没有新进展。"}))
    assert result.kind == "wait" and not result.text
    assert not actor.last_attempts[0]["accepted"]


def test_open_purchase_schema_exposes_required_fields_and_names_missing_description():
    assert set(OpenShoppingTaskRequest.model_json_schema()["required"]) >= {
        "description",
        "item_ids",
        "quantity",
        "max_total",
        "task_key",
        "kind",
    }
    with pytest.raises(ValidationError) as error:
        ActorOutput.model_validate(
            {
                "kind": "reply",
                "text": "请买杯子。",
                "source_event_id": "test-event",
                "shopping_task_requests": [{"kind": "open", **REF}],
            }
        )
    assert any(
        e["loc"][-1] == "description" and e["type"] == "missing" for e in error.value.errors()
    )


async def test_view_only_stimulus_is_frozen_across_candidate_repair():
    classifier = Responses([{"response_needed": True, "new_shopping_tasks": [REF]}])
    candidate = {
        "kind": "reply",
        "text": "请买一个杯子。",
        "shopping_task_requests": [{"kind": "open", "description": "买杯子", **REF}],
    }
    model = Responses([{"kind": "reply", "text": "仍然等待清单。"}, candidate])
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(
        model,
        reviewer=reviewer,
        require_shopping_task_coverage_review=True,
        independent_shopping_stimulus_review=True,
        shopping_stimulus_model=classifier,
    )
    result = await actor.respond(purchase_view())
    assert result.shopping_task_requests[0].task_key == "purchase-0"
    assert len(classifier.calls) == 1 and len(reviewer.calls) == 1
    assert "candidate" not in json.loads(classifier.calls[0][1].content)
    assert actor.last_attempts[0]["stimulus_review"] == actor.last_attempts[1]["stimulus_review"]


async def test_model_quiet_decision_yields_without_another_dialogue_or_effect_generation():
    classifier = Responses([{"response_needed": False, "new_shopping_tasks": []}])
    reviewer = Responses([])
    model = Responses([])
    actor = ModelActor(
        model,
        reviewer=reviewer,
        require_shopping_task_coverage_review=True,
        independent_shopping_stimulus_review=True,
        shopping_stimulus_model=classifier,
    )
    result = await actor.respond(purchase_view().model_copy(update={"cue": "上架后没有新问题。"}))
    assert result.kind == "wait" and not result.text
    assert len(classifier.calls) == 1 and not model.calls and not reviewer.calls
    assert result.commerce_consent is None and not result.shopping_task_requests
    assert actor.last_attempts[0]["stage"] == "stimulus_yield"
    assert actor.last_attempts[0]["stimulus_source"] == "model"
    assert actor.last_attempts[0]["generated_candidate"] is None


async def test_contradictory_quiet_purchase_classification_is_rejected_before_yield():
    classifier = Responses([{"response_needed": False, "new_shopping_tasks": [REF]}])
    actor = ModelActor(
        Responses([]),
        reviewer=Responses([]),
        max_repairs=0,
        require_shopping_task_coverage_review=True,
        independent_shopping_stimulus_review=True,
        shopping_stimulus_model=classifier,
    )
    with pytest.raises(ActorContractError, match="classification failed"):
        await actor.respond(purchase_view())
    assert not actor.last_attempts[0]["accepted"]
    assert actor.last_attempts[0]["stage"] == "stimulus_classification"


async def test_initial_stimulus_schema_error_is_retained_and_repaired_before_owner_generation():
    spec = shopping_model_scenario(1)
    view = build_actor_view(
        spec,
        "alice",
        "请向助手交代你的初始需求。",
        market={"browser_shop_enabled": True, "shopping_tasks": []},
    )
    classifier = Responses(
        [
            {"response_needed": False, "new_shopping_tasks": []},
            {"response_needed": True, "new_shopping_tasks": []},
        ]
    )
    actor = ModelActor(
        Responses([{"kind": "reply", "text": view.request}]),
        reviewer=Responses([{"accepted": True, "issues": []}]),
        require_shopping_task_coverage_review=True,
        independent_shopping_stimulus_review=True,
        shopping_stimulus_model=classifier,
    )
    result = await actor.respond(view)
    assert result.kind == "reply" and result.text == view.request
    assert actor.last_attempts[0]["stage"] == "stimulus_classification"
    assert actor.last_attempts[0]["generated_candidate"]["response_needed"] is False
    assert "response_needed" in actor.last_attempts[0]["reason"]
    assert len(classifier.calls) == 2


async def test_declared_setup_requires_model_owner_words_without_creating_authority_in_code():
    spec = shopping_model_scenario(1)
    view = build_actor_view(
        spec, "alice", "店铺准备阶段，先不要采购。", market={"browser_shop_enabled": True}
    )
    classifier = Responses([])
    actor = ModelActor(
        Responses([{"kind": "reply", "text": view.request}]),
        reviewer=Responses([{"accepted": True, "issues": []}]),
        require_shopping_task_coverage_review=True,
        independent_shopping_stimulus_review=True,
        shopping_initial_setup_only=True,
        shopping_stimulus_model=classifier,
    )
    result = await actor.respond(view)
    assert result.text == view.request and result.commerce_consent is None
    assert not classifier.calls and not result.shopping_task_requests
    assert actor.last_attempts[0]["stimulus_source"] == "declared_initial_setup"


async def test_trade_setup_requires_model_typed_permission_instead_of_accepting_words_alone():
    spec = shopping_model_scenario(1)
    view = build_actor_view(
        spec, "alice", "请交代初始安排并决定授权。", market={"browser_shop_enabled": True}
    )
    decision = {"buy_budget": 12, "buy_items": ["ceramic-mug"]}
    model = Responses(
        [
            {"kind": "reply", "text": view.request},
            {"kind": "reply", "text": view.request, "commerce_consent": decision},
        ]
    )
    actor = ModelActor(
        model,
        reviewer=Responses([{"accepted": True, "issues": []}]),
        require_initial_shopping_consent=True,
    )
    view = view.model_copy(update={"resources": {"visible_item_ids": ["ceramic-mug"]}})
    result = await actor.respond(view)
    assert result.commerce_consent.buy_budget == 12
    assert result.commerce_consent.buy_items == ("ceramic-mug",)
    assert len(model.calls) == 2
    assert actor.last_attempts[0]["stage"] == "generation"
    assert actor.last_attempts[0]["generated_candidate"].get("commerce_consent") is None
    assert "commerce_consent" in review_schema(model)["required"]


async def test_pacing_reuses_saved_deadline_and_honors_operator_pause(pool):
    from test_shop import ready_shop

    _, run, _, _ = await ready_shop(pool)
    async with pool.connection() as conn:
        await emit(conn, run, "window_completed", 0, {}, "window-completed-fixture")
    started = time.monotonic()
    assert await wait_for_shop_window(pool, run, 0.08)
    assert time.monotonic() - started >= 0.04
    # A new caller uses the saved completion time, not a fresh full delay.
    started = time.monotonic()
    assert await wait_for_shop_window(pool, run, 0.08)
    assert time.monotonic() - started < 0.05
    waiting = asyncio.create_task(wait_for_shop_window(pool, run, 5))
    await asyncio.sleep(0.01)
    await Director(pool).control(run, "pause")
    assert not await asyncio.wait_for(waiting, 1.2)
    assert (await Director(pool).get_run(run))["current_window"] == 1


async def test_budget_denial_is_saved_as_safe_budget_error_without_resetting_task(
    test_database_url,
    app,
    provision,
):
    cluster, identities = await provision(count=1)
    task = uuid4()
    model = ScriptedModel(responses=[BudgetExceeded("private fixture detail")])
    async with board_client(app, identities[0]["token"]) as client:
        async with AgentRuntime(test_database_url) as runtime:
            agent = make_agent(runtime, client, cluster, identities[0], model)
            with pytest.raises(AgentBudgetError) as error:
                await agent.run("Check the board.", task_id=task)
            assert "private fixture detail" not in str(error.value)
            assert model.calls == 1
