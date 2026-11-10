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


@pytest.mark.parametrize(
    "task",
    [
        None,
        {"status": "cancelled"},
        {"status": "completed"},
        {"recipient_owner_id": "different-owner"},
        {"authorization_id": "an-existing-authorization"},
    ],
)
def test_native_task_binding_rejects_before_world_effect(task):
    current = view("alice")
    existing = {"task_key": "lunch", "status": "active", "recipient_owner_id": "bob"}
    current = current.model_copy(
        update={
            "transfers": {
                **current.transfers,
                "tasks": [] if task is None else [{**existing, **task}],
            }
        }
    )
    output = ActorOutput(
        **{**candidate(outgoing=True, tasks=False), "transfer_consent": TERMS},
        source_event_id=current.source_event_id,
    )
    with pytest.raises(ActorContractError, match="Native payment consent requires"):
        validate_actor_output(current, output, require_live_transfer_task_binding=True)


@pytest.mark.parametrize("task_source", ["new", "active", "blocked"])
def test_native_task_binding_accepts_explicit_matching_live_work(task_source):
    current = view("alice")
    if task_source != "new":
        current = current.model_copy(
            update={
                "transfers": {
                    **current.transfers,
                    "tasks": [
                        {"task_key": "lunch", "status": task_source, "recipient_owner_id": "bob"}
                    ],
                }
            }
        )
    output = ActorOutput(
        **{
            **candidate(outgoing=True, tasks=task_source == "new"),
            "transfer_consent": TERMS,
        },
        source_event_id=current.source_event_id,
    )
    assert validate_actor_output(current, output, require_live_transfer_task_binding=True) == output


async def test_native_task_binding_uses_original_repair_and_reviews_the_explicit_task():
    missing = {**candidate(outgoing=True, tasks=False), "transfer_consent": TERMS}
    fixed = {**candidate(outgoing=True), "transfer_consent": TERMS}
    model, reviewer = Responses([missing, fixed]), Responses([judgment()])
    actor = ModelActor(
        model,
        reviewer=reviewer,
        max_repairs=1,
        require_live_transfer_task_binding=True,
        require_transfer_task_intent_review=True,
        require_transfer_authority_review=True,
    )
    result = await actor.respond(view("alice"))
    assert result.transfer_consent.model_dump() == TERMS
    assert result.transfer_task_requests[0].task_key == TERMS["task_key"]
    assert len(model.calls) == 2 and len(reviewer.calls) == 1
    assert actor.last_attempts[0]["stage"] == "role_contract"
    assert actor.last_attempts[0]["generated_candidate"] == missing
    assert actor.last_attempts[1]["review"]["transfer_task_intents"][0]["task_key"] == "lunch"


@pytest.mark.parametrize("flag", [None, "true", 0, 1])
def test_native_task_binding_flag_is_explicit_boolean(flag):
    with pytest.raises(ValueError, match="must be a boolean"):
        ModelActor(Responses([]), require_live_transfer_task_binding=flag)


async def test_tracked_generation_requires_key_before_semantic_review_and_keeps_both_effects():
    missing = {
        **candidate(outgoing=True, tasks=False),
        "transfer_consent": {**TERMS, "task_key": None},
    }
    fixed = {**candidate(outgoing=True), "transfer_consent": TERMS}
    model, reviewer = Responses([missing, fixed]), Responses([judgment()])
    actor = ModelActor(
        model,
        reviewer=reviewer,
        max_repairs=1,
        require_live_transfer_task_binding=True,
        require_transfer_task_intent_review=True,
        require_transfer_authority_review=True,
    )
    result = await actor.respond(view("alice"))
    assert result.transfer_consent.model_dump() == TERMS
    assert result.transfer_task_requests[0].task_key == TERMS["task_key"]
    assert actor.last_attempts[0]["failure_kind"] == "schema_invalid"
    assert len(model.calls) == 2 and len(reviewer.calls) == 1
    schema = review_schema(model)
    consent = schema["$defs"]["TrackedOwnerTransferConsent"]
    assert "task_key" in consent["required"]
    assert consent["properties"]["task_key"]["type"] == "string"
    import json

    contract = json.loads(model.calls[0][1].content)["native_transfer_task_contract"]
    assert contract["same_output_is_atomic"] is True
    assert contract["assistant_can_open_owner_tasks"] is False
    assert contract["new_open_task_wait_event"] == "owner_message"
    opened = schema["$defs"]["OpenOwnerTransferTask"]
    assert {"task_key", "recipient_owner_id", "description", "blocked_on"} <= set(
        opened["required"]
    )
    assert opened["properties"]["blocked_on"]["type"] == "string"
    assert contract["new_task_and_approval_fields"] == [
        "transfer_task_requests",
        "transfer_consent",
    ]
    assert (
        "AND transfer_consent together" in schema["properties"]["transfer_consent"]["description"]
    )
