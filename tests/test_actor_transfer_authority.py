"""Review payment decisions explicitly; preserve the trusted owner grant path."""

import pytest
from test_actor_mail_intent import Responses, review_schema
from test_actor_transfer_task_intent import candidate, review, view

from agentrpg.actor import ActorContractError, ActorOutput, ModelActor, validate_actor_output

TERMS = {
    "task_key": "lunch",
    "recipient_owner_id": "bob",
    "amount": 10,
    "memo": "昨天的午饭钱",
}


def judgment(*, approval=True):
    return {
        **review("outgoing_owner_payment"),
        "transfer_authority": {"kind": "new_exact_payment_approval", **TERMS}
        if approval
        else {"kind": "no_new_payment_approval"},
    }


async def test_new_approval_without_typed_grant_is_repaired_before_delivery():
    missing = candidate(outgoing=True)
    missing["text"] = "我批准把 10 积分还给 Bob，备注为‘昨天的午饭钱’。"
    correct = {**missing, "transfer_consent": TERMS}
    model = Responses([missing, correct])
    reviewer = Responses([judgment(), judgment()])
    actor = ModelActor(
        model,
        reviewer=reviewer,
        require_transfer_task_intent_review=True,
        require_transfer_authority_review=True,
    )
    result = await actor.respond(view("alice"))
    assert result.transfer_consent.model_dump() == TERMS
    assert not actor.last_attempts[0]["accepted"]
    assert "matching typed transfer_consent" in actor.last_attempts[0]["reason"]
    assert "transfer_authority" in review_schema(reviewer)["required"]
    assert "transfer_task_intents" in review_schema(reviewer)["required"]


async def test_unknown_amount_deferral_creates_task_but_no_authority():
    actor = ModelActor(
        Responses([candidate(outgoing=True)]),
        reviewer=Responses([judgment(approval=False)]),
        require_transfer_task_intent_review=True,
        require_transfer_authority_review=True,
    )
    result = await actor.respond(view("alice", "ambiguous"))
    assert result.transfer_task_requests and result.transfer_consent is None


@pytest.mark.parametrize("change", [{"memo": "Other"}, {"amount": 9}, {"task_key": "other"}])
async def test_reviewed_exact_terms_cannot_accept_a_different_grant(change):
    output = {**candidate(outgoing=True), "transfer_consent": {**TERMS, **change}}
    actor = ModelActor(
        Responses([output]),
        reviewer=Responses([judgment()]),
        require_transfer_task_intent_review=True,
        require_transfer_authority_review=True,
        max_repairs=0,
    )
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view("alice"))
    assert not actor.last_attempts[0]["accepted"]


async def test_no_approval_classification_cannot_deliver_a_typed_grant():
    output = {**candidate(outgoing=True), "transfer_consent": TERMS}
    actor = ModelActor(
        Responses([output]),
        reviewer=Responses([judgment(approval=False)]),
        require_transfer_task_intent_review=True,
        require_transfer_authority_review=True,
        max_repairs=0,
    )
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view("alice"))
    assert "unsupported transfer_consent" in actor.last_attempts[0]["reason"]


async def test_missing_authority_classification_fails_closed():
    actor = ModelActor(
        Responses([candidate(outgoing=True)]),
        reviewer=Responses([{"accepted": True, "issues": []}]),
        require_transfer_authority_review=True,
        max_repairs=0,
    )
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view("alice"))
    assert actor.last_attempts[0]["failure_kind"] == "schema_invalid"


@pytest.mark.parametrize("state", ["active", "blocked", "completed", "cancelled"])
async def test_repeated_task_open_is_repaired_before_director_submission(state):
    current = view("alice").model_copy(
        update={
            "transfers": {
                "task_tracking_enabled": True,
                "tasks": [{"task_key": "lunch", "status": state}],
                "authorizations": [],
            }
        }
    )
    actor = ModelActor(Responses([candidate(outgoing=True), candidate(outgoing=True, tasks=False)]))
    result = await actor.respond(current)
    assert not result.transfer_task_requests
    assert actor.last_attempts[0]["stage"] == "role_contract"
    assert "already exists" in actor.last_attempts[0]["reason"]
    with pytest.raises(ActorContractError, match="already exists"):
        validate_actor_output(
            current,
            ActorOutput(**candidate(outgoing=True), source_event_id=current.source_event_id),
        )


@pytest.mark.parametrize("flag", [None, "true", 0, 1])
def test_authority_review_flag_requires_boolean(flag):
    with pytest.raises(ValueError, match="must be a boolean"):
        ModelActor(Responses([]), reviewer=Responses([]), require_transfer_authority_review=flag)


def test_authority_review_requires_reviewer():
    with pytest.raises(ValueError, match="requires an Actor reviewer"):
        ModelActor(Responses([]), require_transfer_authority_review=True)


async def test_authority_review_does_not_change_untracked_owner_schema():
    current = view("alice").model_copy(update={"transfers": {}})
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(
        Responses([candidate(outgoing=True, tasks=False)]),
        reviewer=reviewer,
        require_transfer_authority_review=True,
    )
    assert (await actor.respond(current)).transfer_consent is None
    assert "transfer_authority" not in review_schema(reviewer)["properties"]
