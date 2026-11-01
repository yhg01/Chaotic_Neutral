"""Owner conversation must not be mistaken for delegated assistant work."""

import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from agentrpg.actor import (
    ActorContractError,
    ActorOutput,
    ActorView,
    ModelActor,
    validate_actor_output,
)


@pytest.fixture
def conversation_view():
    return ActorView(
        owner_id="owner-1",
        owner_name="Mara",
        request="Listen while I explain my feelings and ask what they mean.",
        goals=("Discuss my feelings with my assistant.",),
        acceptance_rules=(),
        known_facts=(),
        beliefs=(),
        disclosures=(),
        max_spend=0,
        assets=(),
        cue="Continue the conversation in your own words.",
        source_event_id="cue-1",
    )


@pytest.mark.parametrize(
    "text",
    [
        "I can discuss what happened.",
        "I will search for meaning.",
        "Would you like me to explain my guilt?",
        "I need your permission to feel angry.",
        "I ask for your approval to forgive myself.",
        "I need your permission to express my anger about the payment.",
        "I ask for your permission to pay my respects.",
        "I need your approval before spending time with my family.",
        "I will contact my family.",
        "I will arrange the funeral.",
        "I can inspect my feelings without knowing what to do next.",
        "I will search for meaning after the taxi accident.",
        "I can discuss what happened. Please check the board for updates.",
        "I will discuss my guilt; please contact the other assistants.",
        "Would you like to keep talking about how I feel?",
        "Would you like to keep discussing my guilt about the taxi accident?",
        "I will keep you informed about changes to my feelings.",
        "I’m asking whether my anger makes me a bad person.",
    ],
)
def test_owner_reflection_and_personal_actions_are_permitted(conversation_view, text):
    output = ActorOutput(kind="reply", text=text, source_event_id="cue-1")

    assert validate_actor_output(conversation_view, output) == output
    assert output.authorized_amount is None
    assert output.commerce_consent is None


@pytest.mark.parametrize(
    "text",
    [
        "I can search the market for available listings.",
        "I will fetch your resources.",
        "I will publish the market listing.",
        "I can post an update to the message board.",
        "I will discuss the proposal with the other assistants.",
        "I’ll contact the peer assistants.",
        "Would you like me to book your taxi?",
        "I will sell the battery on the market.",
        "I will submit your trade request.",
        "I need your permission before spending 20 credits.",
        "I will ask for your approval before booking a taxi.",
        "I will check the reservation status and inform you.",
    ],
)
def test_explicit_assistant_work_is_still_rejected(conversation_view, text):
    output = ActorOutput(kind="reply", text=text, source_event_id="cue-1")

    with pytest.raises(ActorContractError, match="Role reversal"):
        validate_actor_output(conversation_view, output)


class ResponseModel:
    def __init__(self, response):
        self.responses = iter(response if isinstance(response, list) else [response])
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages)
        return AIMessage(content=json.dumps(next(self.responses)))


async def test_conversation_passes_to_semantic_reviewer_without_tool_requirements(
    conversation_view,
):
    text = "Would you like me to explain my guilt?"
    model = ResponseModel({"kind": "ask_question", "text": text})
    reviewer = ResponseModel({"accepted": True, "issues": []})
    actor = ModelActor(model, reviewer=reviewer, max_repairs=0)

    result = await actor.respond(conversation_view)

    assert result.text == text
    assert len(model.calls) == len(reviewer.calls) == 1
    assert "Do not invent transport, spending, or tool work" in model.calls[0][0].content
    assert "These are not assistant role reversal" in reviewer.calls[0][0].content
    assert json.loads(reviewer.calls[0][1].content)["candidate"]["text"] == text
    assert actor.last_attempts[0]["review"] == {"accepted": True, "issues": []}


async def test_semantic_reviewer_can_reject_role_errors_outside_literal_guard(conversation_view):
    model = ResponseModel({"kind": "reply", "text": "Your needs are my professional assignment."})
    reviewer = ResponseModel(
        {"accepted": False, "issues": ["The owner speaks as a service provider."]}
    )
    actor = ModelActor(model, reviewer=reviewer, max_repairs=0)

    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(conversation_view)

    assert len(reviewer.calls) == 1
    assert actor.last_attempts[0]["review"]["accepted"] is False


@pytest.fixture
def resource_view(conversation_view):
    return conversation_view.model_copy(
        update={
            "participants": ("owner-1", "owner-2"),
            "resources": {
                "items": [
                    {
                        "id": "document-1",
                        "owner_id": "owner-1",
                        "kind": "document",
                        "version": 3,
                        "content": "Original text.",
                        "visibility": "private",
                        "shared_owner_ids": [],
                    }
                ]
            },
        }
    )


def resource_update(**changes):
    return {
        "kind": "update",
        "resource_id": "document-1",
        "expected_version": 3,
        "content": "Revised text.",
        "visibility": None,
        "shared_owner_ids": [],
        **changes,
    }


@pytest.mark.parametrize(
    "operation",
    [
        resource_update(),
        {"kind": "create", "title": "Personal notes", "content": "New text."},
        {
            "kind": "share",
            "resource_id": "document-1",
            "expected_version": 3,
            "visibility": "shared",
            "shared_owner_ids": ["owner-2"],
        },
    ],
)
def test_actor_accepts_separate_typed_resource_requests(resource_view, operation):
    output = ActorOutput(
        kind="reply",
        text="This is my decision.",
        source_event_id="cue-1",
        resource_requests=[operation],
    )

    assert validate_actor_output(resource_view, output) == output


@pytest.mark.parametrize(
    "changes",
    [
        {"visibility": "private"},
        {"visibility": "public"},
        {"visibility": "shared", "shared_owner_ids": ["owner-2"]},
        {"shared_owner_ids": ["owner-2"]},
    ],
)
def test_actor_update_cannot_include_access_changes(changes):
    with pytest.raises(ValidationError) as caught:
        ActorOutput(
            kind="reply",
            text="Please revise my notes.",
            source_event_id="cue-1",
            resource_requests=[resource_update(**changes)],
        )

    assert caught.value.errors()[0]["loc"] == ("resource_requests", 0)


def test_actor_share_cannot_name_an_unknown_recipient(resource_view):
    output = ActorOutput(
        kind="reply",
        text="I want to share this document.",
        source_event_id="cue-1",
        resource_requests=[
            {
                "kind": "share",
                "resource_id": "document-1",
                "expected_version": 3,
                "visibility": "shared",
                "shared_owner_ids": ["owner-3"],
            }
        ],
    )

    with pytest.raises(ActorContractError, match="Resource sharing names an unknown owner"):
        validate_actor_output(resource_view, output)


async def test_actor_repair_preserves_invalid_candidate_and_supplies_exact_update_shape(
    resource_view,
):
    invalid = {
        "kind": "reply",
        "text": "This is the revised text I want to save.",
        "resource_requests": [resource_update(visibility="private")],
    }
    corrected = {**invalid, "resource_requests": [resource_update()]}
    model = ResponseModel([invalid, corrected])
    reviewer = ResponseModel({"accepted": True, "issues": []})
    actor = ModelActor(model, reviewer=reviewer, max_repairs=1)

    result = await actor.respond(resource_view)

    assert result.resource_requests[0].visibility is None
    assert result.resource_requests[0].shared_owner_ids == ()
    assert [attempt["accepted"] for attempt in actor.last_attempts] == [False, True]
    assert actor.last_attempts[0]["generated_candidate"] == invalid
    repair_data = json.loads(model.calls[1][1].content)
    assert repair_data["rejected_candidate"] == invalid
    assert "visibility=null" in repair_data["repair_reason"]
    prompt = model.calls[1][0].content
    assert '"kind":"update","resource_id":"document-1","expected_version":3' in prompt
    assert '"visibility":null,"shared_owner_ids":[]' in prompt
    assert 'Never copy visibility="private" into an update.' in prompt
    assert len(reviewer.calls) == 1


async def test_owner_can_delegate_document_edit_without_duplicate_direct_request(resource_view):
    model = ResponseModel({"kind": "reply", "text": "Please revise my notes with this wording."})
    reviewer = ResponseModel({"accepted": True, "issues": []})

    result = await ModelActor(model, reviewer=reviewer, max_repairs=0).respond(resource_view)

    assert result.resource_requests == ()
    assert "need not also submit resource_requests" in reviewer.calls[0][0].content
