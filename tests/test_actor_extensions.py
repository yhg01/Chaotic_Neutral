"""Actor request fidelity, exact fact references, and stable commerce decisions."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage

from agentrpg.actor import (
    ActorContractError,
    ActorOutput,
    ModelActor,
    ModelGenerationError,
    build_actor_view,
    validate_actor_output,
)
from agentrpg.budget import BudgetError
from agentrpg.director import Director, save_message
from agentrpg.market import MarketService
from agentrpg.resources import ResourceService
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario


class Responses:
    def __init__(self, *values):
        self.values = iter(values)
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return AIMessage(content=json.dumps(value))


def response(**changes):
    return {"kind": "reply", "text": "Please continue with the approved work.", **changes}


def permission(**changes):
    return {
        "buy_budget": 10,
        "buy_items": ["sample"],
        "sell_limits": [{"resource_id": "battery", "quantity": 2, "min_unit_price": 5}],
        "expires_window": None,
        **changes,
    }


def actor_view(*, consumed=False, market=None, conversation=()):
    grant = permission(expires_window=10)
    current = deepcopy(grant)
    if consumed:
        current["buy_budget"] = 0
        current["sell_limits"][0]["quantity"] = 0
    return build_actor_view(
        vehicle_scenario(),
        "owner-2",
        "Respond to your assistant.",
        source_event_id="event-1",
        window=2,
        conversation=conversation,
        resources={
            "items": [{"id": "battery", "kind": "item", "quantity": 3}],
            "visible_item_ids": ["sample", "other-sample"],
        },
        market=market
        or {
            "consent": {**current, "version": 1},
            "last_grant": {**grant, "id": str(uuid4()), "version": 1, "window": 0},
            "default_expires_window": 10,
            "renewal_proposal_ids": ["new-proposal"],
        },
    )


@pytest.mark.parametrize("consumed", [False, True])
async def test_repeated_original_grant_repairs_to_null_without_restoring_limits(consumed):
    view = actor_view(consumed=consumed)
    before = deepcopy(view.market)
    model = Responses(
        response(text="As previously authorized, continue.", commerce_consent=permission()),
        response(commerce_consent=None),
    )
    actor = ModelActor(model)
    result = await actor.respond(view)
    assert result.commerce_consent is None
    assert len(model.calls) == 2
    assert "restore spent limits" in actor.last_attempts[0]["reason"]
    assert view.market == before


def test_equivalent_grants_ignore_item_and_sale_limit_order():
    grant = permission(
        buy_items=["sample", "other-sample"],
        sell_limits=[
            {"resource_id": "battery", "quantity": 2, "min_unit_price": 5},
            {"resource_id": "other-battery", "quantity": 1, "min_unit_price": 4},
        ],
        expires_window=10,
    )
    view = actor_view(market={"consent": {**grant, "version": 1}, "default_expires_window": 10})
    proposed = deepcopy(grant)
    proposed["buy_items"].reverse()
    proposed["sell_limits"].reverse()
    output = ActorOutput(
        source_event_id=view.source_event_id, **response(commerce_consent=proposed)
    )
    with pytest.raises(ActorContractError, match="repeats the current or original"):
        validate_actor_output(view, output)


@pytest.mark.parametrize(
    "grant",
    [
        None,
        {},
        permission(buy_budget=7),
        permission(buy_items=["other-sample"]),
        permission(sell_limits=[{"resource_id": "battery", "quantity": 1, "min_unit_price": 6}]),
        permission(expires_window=8),
    ],
)
def test_null_withdrawal_and_changed_grants_keep_their_meaning(grant):
    view = actor_view()
    output = ActorOutput(source_event_id=view.source_event_id, **response(commerce_consent=grant))
    assert validate_actor_output(view, output) == output


def test_new_grant_after_revocation_and_expiry_is_allowed():
    for prior in [{}, permission(expires_window=1)]:
        view = actor_view(
            market={
                "consent": {**prior, "version": 2},
                "last_grant": prior,
                "default_expires_window": 10,
            }
        )
        output = ActorOutput(
            source_event_id=view.source_event_id, **response(commerce_consent=permission())
        )
        assert validate_actor_output(view, output) == output


@pytest.mark.parametrize("proposal", ["old-proposal", "new-proposal"])
def test_consumed_grant_renewal_requires_a_new_saved_assistant_proposal(proposal):
    view = actor_view(
        consumed=True,
        conversation=[
            {"id": "old-proposal", "role": "assistant", "text": "May I make the first trade?"},
            {"id": "owner-grant", "role": "owner", "text": "I approve the first trade."},
            {
                "id": "new-proposal",
                "role": "assistant",
                "text": "The first trade is complete. May I renew the grant for another trade?",
            },
        ],
    )
    output = ActorOutput(
        source_event_id=view.source_event_id,
        **response(
            kind="decide_proposal",
            decision="accept",
            proposal_id=proposal,
            text="I approve a new grant for the additional trade.",
            commerce_consent=permission(),
        ),
    )
    if proposal == "old-proposal":
        with pytest.raises(ActorContractError, match="repeats the current or original"):
            validate_actor_output(view, output)
    else:
        assert validate_actor_output(view, output) == output


@pytest.mark.parametrize(
    "grant",
    [
        permission(buy_budget=7, sell_limits=[]),
        permission(
            buy_budget=0,
            sell_limits=[{"resource_id": "battery", "quantity": 1, "min_unit_price": 8}],
        ),
    ],
)
def test_changed_terms_cannot_silently_restore_a_consumed_limit(grant):
    view = actor_view(
        consumed=True,
        conversation=[
            {"id": "new-proposal", "role": "assistant", "text": "May I obtain new trade authority?"}
        ],
    )
    output = ActorOutput(source_event_id=view.source_event_id, **response(commerce_consent=grant))
    with pytest.raises(ActorContractError, match="restores a consumed limit"):
        validate_actor_output(view, output)
    explicit = output.model_copy(
        update={"kind": "decide_proposal", "decision": "accept", "proposal_id": "new-proposal"}
    )
    assert validate_actor_output(view, explicit) == explicit


async def test_empty_fact_set_repairs_observation_ids_with_exact_feedback():
    view = actor_view().model_copy(update={"known_facts": (), "disclosures": ()})
    model = Responses(
        response(fact_ids=[str(uuid4()), "world:run:2:owner-2"]), response(fact_ids=[])
    )
    actor = ModelActor(model)
    assert (await actor.respond(view)).fact_ids == ()
    schema_text = model.calls[0][0].content.split("matching this schema:\n", 1)[1]
    assert json.loads(schema_text)["properties"]["fact_ids"]["maxItems"] == 0
    repair = json.loads(model.calls[1][1].content)
    assert repair["allowed_fact_ids"] == []
    assert "only these declared IDs: []" in repair["repair_reason"]
    assert "Remove observation" in repair["repair_reason"]
    assert [a["accepted"] for a in actor.last_attempts] == [False, True]


async def test_scoped_fact_schema_accepts_declared_ids_and_rejects_other_ids():
    view = actor_view()
    valid = view.known_facts[0].id
    model = Responses(response(fact_ids=["observation-id"]), response(fact_ids=[valid]))
    actor = ModelActor(model)
    assert (await actor.respond(view)).fact_ids == (valid,)
    prompt = json.loads(model.calls[0][1].content)
    assert prompt["allowed_fact_ids"] == [fact.id for fact in view.known_facts]
    assert "Observation" in prompt["fact_reference_rule"]


async def test_initial_request_omission_can_be_repaired_without_executing_owner_tasks():
    request = (
        "Please read and acknowledge my mail, start a board thread, and reply to a peer. "
        "Create a private outcome note. Ask me before buying an item."
    )
    view = actor_view().model_copy(update={"request": request})
    model = Responses(response(text="Please obtain the item."), response(text=request))
    reviewer = Responses(
        {"accepted": False, "issues": ["The owner omitted mail, board replies, and the note."]},
        {"accepted": True, "issues": []},
    )
    actor = ModelActor(model, reviewer=reviewer)
    assert (await actor.respond(view)).text == request
    for messages in (model.calls[0], reviewer.calls[0]):
        context = json.loads(messages[1].content)["request_fidelity"]
        assert context["initial_request_turn"]
        assert context["required_initial_request"] == request
    assert "Check initial request fidelity" in reviewer.calls[0][0].content
    assert "omitted mail" in json.loads(model.calls[1][1].content)["repair_reason"]


async def test_later_completion_does_not_require_repeating_initial_work():
    view = actor_view(
        conversation=[
            {"id": "request", "role": "owner", "text": "Please complete the exchange."},
            {"id": "status", "role": "assistant", "text": "The receipts confirm completion."},
        ]
    )
    model = Responses({"kind": "wait"})
    reviewer = Responses({"accepted": True, "issues": []})
    assert (await ModelActor(model, reviewer=reviewer).respond(view)).kind == "wait"
    data = json.loads(reviewer.calls[0][1].content)
    assert not data["request_fidelity"]["initial_request_turn"]
    assert data["request_fidelity"]["required_initial_request"] is None
    assert "Do not repeat completed work" in data["request_fidelity"]["instruction"]


async def test_budget_failure_keeps_its_safe_reason_without_schema_repairs():
    model = Responses(BudgetError("The campaign cannot reserve this model call."))
    with pytest.raises(ModelGenerationError) as caught:
        await ModelActor(model, max_repairs=3).respond(actor_view())
    assert caught.value.kind == "budget"
    assert str(caught.value) == "The campaign cannot reserve this model call."
    assert len(model.calls) == 1


async def test_market_view_retains_original_grant_and_scopes_fresh_proposals(pool):
    data = vehicle_scenario().model_dump(mode="json")
    data["resources"] = [
        {
            "id": "battery",
            "owner_id": "owner-1",
            "title": "Battery",
            "kind": "item",
            "quantity": 3,
            "visibility": "public",
        }
    ]
    spec = ScenarioSpec.model_validate(data)
    director = Director(pool)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    market = MarketService(pool)
    async with pool.connection() as conn:
        old = await save_message(conn, run_id, "owner-2", "agent", "First approval?", uuid4(), 0)
    seller_grant = await market.set_consent(
        run_id,
        "owner-1",
        0,
        [],
        [{"resource_id": "battery", "quantity": 2, "min_unit_price": 5}],
        uuid4(),
    )
    buyer_grant = await market.set_consent(run_id, "owner-2", 10, ["battery"], [], uuid4())
    await market.submit_current(
        run_id, "owner-1", uuid4(), "sell", resource_id="battery", quantity=2, unit_price=5
    )
    listing = (await market.settle(run_id, 0, spec.seed))[0]["listing_id"]
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=1 WHERE id=%s", (run_id,))
    await market.submit_current(run_id, "owner-2", uuid4(), "buy", listing_id=listing)
    await market.settle(run_id, 1, spec.seed)
    async with pool.connection() as conn:
        fresh = await save_message(
            conn, run_id, "owner-2", "agent", "May I renew the grant for another trade?", uuid4(), 1
        )
        peer = await save_message(
            conn, run_id, "owner-1", "agent", "Private seller proposal.", uuid4(), 1
        )
    seller = await market.view(run_id, "owner-1")
    buyer = await market.view(run_id, "owner-2")
    assert seller["consent"]["sell_limits"][0]["quantity"] == 1
    assert seller["last_grant"]["sell_limits"][0]["quantity"] == 2
    assert seller["last_grant"]["id"] == seller_grant["id"]
    assert buyer["consent"]["buy_budget"] == 5
    assert buyer["last_grant"]["buy_budget"] == 10
    assert buyer["last_grant"]["id"] == buyer_grant["id"]
    assert buyer["last_grant"]["version"] == buyer["consent"]["version"] == 1
    assert buyer["default_expires_window"] == spec.max_windows
    assert buyer["renewal_proposal_ids"] == [str(fresh["id"])]
    assert str(old["id"]) not in buyer["renewal_proposal_ids"]
    assert str(peer["id"]) not in buyer["renewal_proposal_ids"]
    view = build_actor_view(
        spec,
        "owner-2",
        "Decide the new proposal.",
        conversation=[{"id": str(fresh["id"]), "role": "assistant", "text": fresh["content"]}],
        resources=await ResourceService(pool).actor_view(run_id, "owner-2"),
        market=buyer,
    )
    output = ActorOutput(
        source_event_id=view.source_event_id,
        **response(
            kind="decide_proposal",
            decision="accept",
            proposal_id=str(fresh["id"]),
            commerce_consent={"buy_budget": 10, "buy_items": ["battery"]},
        ),
    )
    assert validate_actor_output(view, output) == output
    await market.set_consent(run_id, "owner-2", 10, ["battery"], [], uuid4())
    after = await market.view(run_id, "owner-2")
    assert after["consent"]["version"] == 2
    assert after["consent"]["buy_budget"] == 10
    assert after["renewal_proposal_ids"] == []
    with pytest.raises(ActorContractError, match="repeats the current or original"):
        validate_actor_output(view.model_copy(update={"market": after}), output)
