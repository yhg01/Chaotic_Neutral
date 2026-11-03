"""Owner payment tasks must not be inferred from incoming-payment monitoring."""

from uuid import UUID

import pytest
from test_actor_mail_intent import Responses, review_schema

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorContractError, ModelActor, ScriptedActor, build_actor_view
from agentrpg.director import Director, rows
from agentrpg.run_engine import RunEngine
from agentrpg.transfer_tasks import TransferTaskService
from agentrpg.transfer_validation import validation_scenario


def view(owner="bob", case="baseline"):
    return build_actor_view(
        validation_scenario(case),
        owner,
        "State your current need.",
        transfers={"task_tracking_enabled": True, "tasks": [], "authorizations": []},
    )


def candidate(*, outgoing=False, tasks=True):
    return {
        "kind": "reply",
        "text": "请先核实午饭金额，再帮我归还 Bob。"
        if outgoing
        else "请帮我留意 Alice 的还款消息和账户。",
        "transfer_task_requests": [
            {
                "kind": "open",
                "task_key": "lunch",
                "recipient_owner_id": "bob" if outgoing else "alice",
                "description": "Repay lunch" if outgoing else "Monitor receipt",
                "blocked_on": "inbox_received",
            }
        ]
        if tasks
        else [],
    }


def review(intent, key="lunch"):
    return {
        "accepted": True,
        "issues": [],
        "transfer_task_intents": [{"task_key": key, "intent": intent}],
    }


async def test_incoming_monitor_is_rejected_even_when_review_says_accepted():
    original = candidate()
    model = Responses([original, candidate(tasks=False)])
    reviewer = Responses([review("incoming_payment_monitor"), {"accepted": True, "issues": []}])
    actor = ModelActor(model, reviewer=reviewer, require_transfer_task_intent_review=True)

    result = await actor.respond(view())

    assert result.text == original["text"] and not result.transfer_task_requests
    assert not actor.last_attempts[0]["accepted"]
    assert actor.last_attempts[0]["review"]["transfer_task_intents"][0]["intent"] == (
        "incoming_payment_monitor"
    )
    assert "Incoming-payment monitoring" in actor.last_attempts[0]["reason"]
    assert "transfer_task_intents" in review_schema(reviewer)["required"]
    assert "transfer_task_intents" not in review_schema(reviewer, 1)["properties"]


async def test_unknown_amount_is_a_valid_outgoing_task_without_payment_consent():
    actor = ModelActor(
        Responses([candidate(outgoing=True)]),
        reviewer=Responses([review("outgoing_owner_payment")]),
        require_transfer_task_intent_review=True,
    )
    result = await actor.respond(view("alice", "ambiguous"))
    assert result.transfer_task_requests[0].recipient_owner_id == "bob"
    assert result.transfer_consent is None


@pytest.mark.parametrize(
    "judgment",
    [
        {"accepted": True, "issues": []},
        review("unsupported"),
        review("outgoing_owner_payment", "invented-key"),
        {**review("outgoing_owner_payment"), "transfer_task_intents": []},
        {
            **review("outgoing_owner_payment"),
            "transfer_task_intents": [
                {"task_key": "lunch", "intent": "outgoing_owner_payment"},
                {"task_key": "lunch", "intent": "outgoing_owner_payment"},
            ],
        },
        {**review("outgoing_owner_payment"), "issues": ["Unsupported dialogue."]},
    ],
)
async def test_missing_wrong_duplicate_or_unsupported_review_cannot_commit(judgment):
    actor = ModelActor(
        Responses([candidate(outgoing=True)]),
        reviewer=Responses([judgment]),
        require_transfer_task_intent_review=True,
        max_repairs=0,
    )
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view("alice"))
    assert not actor.last_attempts[0]["accepted"]
    assert actor.last_attempts[0]["candidate"]["transfer_task_requests"]


async def test_task_and_mail_intent_are_both_required_for_combined_effects():
    value = candidate(outgoing=True)
    value["mail_requests"] = [
        {"recipient_owner_id": "bob", "subject": "Lunch", "body": "I will check the amount."}
    ]
    judgment = {**review("outgoing_owner_payment"), "mail_intent": "assistant_delegated"}
    reviewer = Responses([judgment])
    actor = ModelActor(
        Responses([value]),
        reviewer=reviewer,
        require_transfer_task_intent_review=True,
        require_mail_intent_review=True,
        max_repairs=0,
    )
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view("alice"))
    assert {"mail_intent", "transfer_task_intents"} <= set(review_schema(reviewer)["required"])
    assert not actor.last_attempts[0]["accepted"]


async def test_legacy_task_profile_keeps_original_review_contract():
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(Responses([candidate()]), reviewer=reviewer)
    assert (await actor.respond(view())).transfer_task_requests
    assert "transfer_task_intents" not in review_schema(reviewer)["properties"]


@pytest.mark.parametrize("flag", [None, "true", 0, 1])
def test_task_review_flag_requires_boolean(flag):
    with pytest.raises(ValueError, match="must be a boolean"):
        ModelActor(Responses([]), reviewer=Responses([]), require_transfer_task_intent_review=flag)


def test_task_review_requires_reviewer():
    with pytest.raises(ValueError, match="requires an Actor reviewer"):
        ModelActor(Responses([]), require_transfer_task_intent_review=True)


async def test_rejected_monitor_never_becomes_director_business_task(pool):
    director = Director(pool)
    manifest = await director.create_run(validation_scenario("baseline"))
    run = UUID(manifest["run_id"])
    await TransferTaskService(pool).enable(run)
    bob = ModelActor(
        Responses([candidate(), candidate(tasks=False)]),
        reviewer=Responses([review("incoming_payment_monitor"), {"accepted": True, "issues": []}]),
        require_transfer_task_intent_review=True,
    )

    class Finished:
        async def run_activation(self, envelope):
            return ActivationResult(status="finished")

    engine = RunEngine(
        pool,
        actors={"alice": ScriptedActor(), "bob": bob},
        agents={"alice": Finished(), "bob": Finished()},
    )
    await director.control(run, "start")
    await engine.advance(run)

    assert not await TransferTaskService(pool).list(run, "bob")
    messages = await director.messages(run, "bob")
    assert messages[0]["content"] == candidate()["text"]
    async with pool.connection() as conn:
        jobs = await rows(
            conn,
            "SELECT output_meta FROM run_jobs WHERE run_id=%s AND owner_id='bob' AND kind='actor'",
            (run,),
        )
    attempts = jobs[0]["output_meta"]["attempts"]
    assert len(attempts) == 2 and not attempts[0]["accepted"] and attempts[1]["accepted"]
