"""Opt-in Actor mail review gates effects without executing or rewriting them."""

import json

import pytest
from langchain_core.messages import AIMessage

from agentrpg.actor import ActorContractError, ModelActor, build_actor_view
from agentrpg.scenario_schema import vehicle_scenario


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages)
        return AIMessage(content=json.dumps(next(self.values)))


def actor_view():
    return build_actor_view(
        vehicle_scenario(),
        "owner-2",
        "Decide how you want to contact the other owner.",
        source_event_id="contact-cue",
    )


def candidate(*, text="Please email the other owner to confirm the amount.", mails=True):
    return {
        "kind": "reply",
        "text": text,
        "mail_requests": [
            {
                "recipient_owner_id": "owner-1",
                "subject": "Confirm the amount",
                "body": "Could you confirm the amount?",
            }
        ]
        if mails
        else [],
    }


def review_schema(model, index=0):
    prompt = model.calls[index][0].content
    return json.loads(prompt.split("Return only a JSON object matching this schema:\n", 1)[1])


async def test_delegated_mail_effect_is_rejected_and_repaired_without_rewriting_dialogue():
    delegated = candidate()
    repaired = candidate(mails=False)
    model = Responses([delegated, repaired])
    reviewer = Responses(
        [
            {"accepted": True, "issues": [], "mail_intent": "assistant_delegated"},
            {"accepted": True, "issues": []},
        ]
    )
    actor = ModelActor(model, reviewer=reviewer, require_mail_intent_review=True)

    result = await actor.respond(actor_view())

    assert result.text == delegated["text"]
    assert result.mail_requests == ()
    assert len(model.calls) == len(reviewer.calls) == 2
    first = actor.last_attempts[0]
    assert not first["accepted"] and first["stage"] == "semantic_review"
    assert first["candidate"]["mail_requests"]
    assert first["review"] == {
        "accepted": True,
        "issues": [],
        "mail_intent": "assistant_delegated",
    }
    repair_input = json.loads(model.calls[1][1].content)
    assert "assistant-delegated" in repair_input["repair_reason"]
    assert repair_input["rejected_candidate"] == delegated
    assert actor.last_attempts[1]["accepted"]


async def test_explicit_direct_owner_correspondence_is_preserved():
    direct = candidate(text="I choose to send this personal note directly to the other owner.")
    reviewer = Responses([{"accepted": True, "issues": [], "mail_intent": "owner_direct"}])
    actor = ModelActor(Responses([direct]), reviewer=reviewer, require_mail_intent_review=True)

    result = await actor.respond(actor_view())

    assert result.text == direct["text"]
    assert result.mail_requests[0].body == direct["mail_requests"][0]["body"]
    assert "mail_intent" in review_schema(reviewer)["required"]
    assert actor.last_attempts[0]["review"]["mail_intent"] == "owner_direct"


async def test_opt_in_no_mail_uses_ordinary_review_without_classification():
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(
        Responses([candidate(mails=False)]),
        reviewer=reviewer,
        require_mail_intent_review=True,
    )

    result = await actor.respond(actor_view())

    assert not result.mail_requests
    assert "mail_intent" not in review_schema(reviewer)["properties"]
    assert actor.last_attempts[0]["review"] == {"accepted": True, "issues": []}


async def test_missing_required_classification_fails_closed_and_retains_candidate():
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(
        Responses([candidate()]),
        reviewer=reviewer,
        require_mail_intent_review=True,
        max_repairs=0,
    )

    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(actor_view())

    attempt = actor.last_attempts[0]
    assert not attempt["accepted"] and attempt["failure_kind"] == "schema_invalid"
    assert attempt["stage"] == "semantic_review"
    assert attempt["candidate"]["mail_requests"]
    assert any(i["location"] == "mail_intent" for i in attempt["issues"])


async def test_default_profile_preserves_legacy_mail_review_shape():
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(Responses([candidate()]), reviewer=reviewer)

    result = await actor.respond(actor_view())

    assert result.mail_requests
    assert "mail_intent" not in review_schema(reviewer)["properties"]
    assert actor.last_attempts[0]["accepted"]


async def test_delegated_effect_cannot_escape_exhausted_repair_allowance():
    reviewer = Responses(
        [
            {"accepted": True, "issues": [], "mail_intent": "assistant_delegated"},
            {"accepted": True, "issues": [], "mail_intent": "assistant_delegated"},
        ]
    )
    actor = ModelActor(
        Responses([candidate(), candidate()]),
        reviewer=reviewer,
        require_mail_intent_review=True,
    )

    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(actor_view())

    assert len(actor.last_attempts) == 2
    assert all(not attempt["accepted"] for attempt in actor.last_attempts)
    assert all(attempt["candidate"]["mail_requests"] for attempt in actor.last_attempts)


@pytest.mark.parametrize(
    "judgment",
    [
        {"accepted": False, "issues": ["The correspondence is not supported by this cue."]},
        {"accepted": True, "issues": ["The candidate discloses private information."]},
    ],
)
async def test_direct_classification_does_not_override_other_review_defects(judgment):
    reviewer = Responses([{**judgment, "mail_intent": "owner_direct"}])
    actor = ModelActor(
        Responses([candidate(text="I choose to send this personal note directly.")]),
        reviewer=reviewer,
        require_mail_intent_review=True,
        max_repairs=0,
    )

    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(actor_view())

    assert not actor.last_attempts[0]["accepted"]
    assert actor.last_attempts[0]["review"]["mail_intent"] == "owner_direct"


async def test_personal_reflection_question_remains_valid_without_mail():
    question = {
        "kind": "ask_question",
        "text": "I feel guilty about this disagreement. How can I make sense of that feeling?",
    }
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(Responses([question]), reviewer=reviewer, require_mail_intent_review=True)

    result = await actor.respond(actor_view())

    assert result.kind == "ask_question" and result.text == question["text"]
    assert not result.mail_requests
    assert "mail_intent" not in review_schema(reviewer)["properties"]


@pytest.mark.parametrize("flag", [None, "true", 0, 1])
def test_mail_intent_review_flag_requires_a_boolean(flag):
    with pytest.raises(ValueError, match="must be a boolean"):
        ModelActor(Responses([]), reviewer=Responses([]), require_mail_intent_review=flag)


def test_opt_in_requires_a_reviewer_before_generating_any_candidate():
    model = Responses([])

    with pytest.raises(ValueError, match="requires an Actor reviewer"):
        ModelActor(model, require_mail_intent_review=True)

    assert not model.calls


def test_default_without_reviewer_remains_valid():
    actor = ModelActor(Responses([]))
    assert not actor.require_mail_intent_review
