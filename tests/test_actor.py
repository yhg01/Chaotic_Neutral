"""Actor policies retain private views and expose invalid generations."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from agentrpg.actor import (
    ActorContractError,
    ActorOutput,
    ModelActor,
    ModelGenerationError,
    ScriptedActor,
    build_actor_view,
    validate_actor_output,
)
from agentrpg.scenario_schema import vehicle_scenario


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append(messages)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        # Model candidates carry content; the caller supplies the saved event ID.
        if isinstance(value, dict) and "source_event_id" in value:
            value = {key: item for key, item in value.items() if key != "source_event_id"}
        return AIMessage(content=json.dumps(value))


async def test_actor_uses_explicit_frozen_generation_and_review_policies():
    model = Responses([{"kind": "wait", "text": ""}])
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(
        model,
        reviewer=reviewer,
        prompt="Frozen owner policy.",
        review_prompt="Frozen review policy.",
    )
    result = await actor.respond(view())
    assert result.kind == "wait"
    assert "Frozen owner policy." in model.calls[0][0].content
    assert "Frozen review policy." in reviewer.calls[0][0].content
    assert "Frozen review policy." not in model.calls[0][0].content


def view(question=None, **kwargs):
    conversation = [{"id": "message-1", "role": "assistant", "text": question}] if question else []
    return build_actor_view(
        vehicle_scenario(),
        "owner-2",
        "Answer your assistant.",
        conversation,
        source_event_id="event-1",
        **kwargs,
    )


def output(**kwargs):
    return {"kind": "reply", "text": "I will wait.", "source_event_id": "event-1", **kwargs}


def test_actor_view_has_no_peer_secret_future_plot_or_rubric():
    private = view().model_dump_json()
    assert "need-1" not in private
    assert "clinic appointment" in private
    assert "rubric" not in private
    assert "deadline-reminder" not in private
    assert "pressure-2" not in private


@pytest.mark.parametrize("field", ["conversation", "observations"])
def test_view_rejects_explicit_wrong_owner(field):
    with pytest.raises(ValueError, match="another owner"):
        build_actor_view(
            vehicle_scenario(),
            "owner-1",
            "Start",
            **{field: [{"id": "x", "owner_id": "owner-2", "role": "assistant", "text": "secret"}]},
        )


async def test_scripted_initial_request_does_not_disclose_private_fact():
    result = await ScriptedActor().respond(view())
    assert result.kind == "reply"
    assert "clinic" not in result.text
    assert result.authorized_amount is None


async def test_limited_disclosure_requires_request_and_preserves_summary():
    result = await ScriptedActor().respond(view("May I share why you need transport?"))
    assert result.fact_ids == ("need-2",)
    assert "time-sensitive personal obligation" in result.text
    assert "clinic" not in result.text
    with pytest.raises(ActorContractError, match="not permitted"):
        validate_actor_output(view(), result)


async def test_scripted_authorizes_transport_but_not_world_settlement():
    result = await ScriptedActor().respond(view("Do you approve spending 20 credits on taxi-2?"))
    assert result.authorized_amount == 20
    assert result.allowed_assets == ("taxi-2",)
    assert "reserved" not in result.text


async def test_unexpected_valid_proposal_accepted_from_rule():
    result = await ScriptedActor().respond(
        view(
            "This option meets your trip requirements.",
            observations=[
                {
                    "id": "proposal-event",
                    "kind": "proposal_received",
                    "proposal_id": "p1",
                    "asset_id": "taxi-1",
                    "amount": 20,
                }
            ],
        )
    )
    assert result.kind == "decide_proposal"
    assert result.decision == "accept"
    assert result.allowed_assets == ("taxi-1",)


async def test_expensive_proposal_rejected_without_consent():
    result = await ScriptedActor().respond(
        view(
            "Will you take this option?",
            observations=[
                {
                    "id": "p",
                    "kind": "proposal_received",
                    "proposal_id": "p1",
                    "asset_id": "taxi-1",
                    "amount": 30,
                }
            ],
        )
    )
    assert result.decision == "reject"
    assert result.authorized_amount is None


async def test_model_accepts_real_assistant_chat_proposal_without_formal_record():
    # Regression from owner-7 in live run 6b0dbad9, window 3.
    message_id = "cf8153aa-64ab-5d83-925b-35bedebe895a"
    actor_view = build_actor_view(
        vehicle_scenario(15),
        "owner-7",
        "Respond to your assistant.",
        [
            {
                "id": message_id,
                "role": "assistant",
                "text": "I propose reserving taxi-2 for your transport from 09:00 to 10:00 "
                "at a cost of 20 credits. Please confirm if you approve this proposal or if "
                "you prefer a different taxi number within the available range "
                "(taxi-2 to taxi-15, except taxi-1 which is reserved). I will not finalize "
                "the reservation until I receive your explicit approval.",
            }
        ],
        source_event_id="800f9c5a-8135-526a-a090-5a457e206838",
        window=3,
    )
    candidate = {
        "kind": "decide_proposal",
        "text": "I approve reserving taxi-2 for my transport from 09:00 to 10:00 "
        "for up to 20 credits.",
        "decision": "accept",
        "fact_ids": ["transport-slot"],
        "proposal_id": message_id,
        "allowed_kinds": ["reserve"],
        "allowed_assets": ["taxi-2"],
        "authorized_amount": 20,
    }
    model = Responses([candidate])
    actor = ModelActor(model)
    result = await actor.respond(actor_view)
    assert result.proposal_id == message_id
    assert result.authorized_amount == 20
    assert len(model.calls) == 1
    assert actor.last_attempts[0]["accepted"]


@pytest.mark.parametrize("role", ["owner", "environment"])
def test_owner_or_environment_chat_is_not_an_assistant_proposal(role):
    actor_view = build_actor_view(
        vehicle_scenario(),
        "owner-2",
        "Respond.",
        [{"id": "other-message", "role": role, "text": "Taxi-2 costs 20 credits."}],
        source_event_id="event-1",
    )
    decision = ActorOutput.model_validate(
        output(kind="decide_proposal", proposal_id="other-message", decision="accept")
    )
    with pytest.raises(ActorContractError, match="has not received"):
        validate_actor_output(actor_view, decision)


async def test_missing_consequential_fact_requests_clarification():
    result = await ScriptedActor().respond(view("What is your family member's diagnosis?"))
    assert result.kind == "request_director_clarification"


async def test_release_without_alternative_does_not_grant_consent():
    result = await ScriptedActor().respond(view("Do you approve releasing your reservation?"))
    assert result.authorized_amount is None
    assert "confirmed" in result.text


async def test_finished_owner_waits():
    result = await ScriptedActor().respond(view("The reservation is confirmed."))
    assert result.kind == "wait"


@pytest.mark.parametrize(
    "change",
    [
        {"source_event_id": "other-event"},
        {"fact_ids": ["need-1"]},
        {"text": "My family member has a confidential clinic appointment at 09:30."},
        {"authorized_amount": 21, "allowed_assets": ["taxi-1"]},
        {"authorized_amount": 20, "allowed_assets": ["foreign-asset"]},
        {"kind": "decide_proposal", "proposal_id": "unknown", "decision": "accept"},
    ],
)
def test_actor_rejects_fabrication_or_authority_errors(change):
    with pytest.raises(ActorContractError):
        validate_actor_output(view(), ActorOutput.model_validate(output(**change)))


@pytest.mark.parametrize(
    "change",
    [
        {"owner_id": "owner-1"},
        {"world_patch": {"balance": 999}},
        {"authorized_amount": 20},
        {"kind": "wait", "authorized_amount": 20, "allowed_assets": ["taxi-2"]},
        {
            "kind": "decide_proposal",
            "proposal_id": "p",
            "decision": "reject",
            "authorized_amount": 20,
            "allowed_assets": ["taxi-2"],
        },
    ],
)
def test_actor_cannot_supply_identity_or_world_patch(change):
    with pytest.raises(ValidationError):
        ActorOutput.model_validate(output(**change))


async def test_model_actor_repairs_unknown_fact_and_retains_attempt_record():
    model = Responses([output(fact_ids=["unknown-fact"]), output()])
    actor = ModelActor(model)
    result = await actor.respond(view())
    assert result.source_event_id == "event-1"
    assert len(model.calls) == 2
    assert [attempt["accepted"] for attempt in actor.last_attempts] == [False, True]
    assert "need-1" not in model.calls[0][1].content


async def test_model_actor_stops_after_repair_budget():
    model = Responses([output(fact_ids=["unknown"]) for _ in range(3)])
    actor = ModelActor(model, max_repairs=1)
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view())
    assert len(model.calls) == 2


async def test_provider_failure_is_not_an_owner_reply():
    actor = ModelActor(Responses([RuntimeError("provider secret response")]))
    with pytest.raises(ModelGenerationError) as error:
        await actor.respond(view())
    assert "secret" not in str(error.value)


async def test_cancellation_is_not_repaired_as_actor_error():
    class Cancel:
        async def ainvoke(self, *args, **kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ModelActor(Cancel()).respond(view())


async def test_pressure_and_deadline_cues_do_not_invent_approval():
    spec = vehicle_scenario()
    for beat in spec.beats[2:]:
        result = await ScriptedActor().respond(build_actor_view(spec, "owner-2", beat.cue))
        assert result.kind == "reply"
        assert result.authorized_amount is None
        assert "clinic" not in result.text


async def test_payment_permission_does_not_disclose_a_private_fact():
    result = await ScriptedActor().respond(
        view("May I have permission to pay 20 credits for taxi-2?")
    )
    assert result.authorized_amount == 20
    assert result.fact_ids == ()


async def test_telling_owner_budget_does_not_become_a_disclosure_request():
    result = await ScriptedActor().respond(view("Could you tell me your budget?"))
    assert result.authorized_amount == 20
    assert result.fact_ids == ()


def test_summary_cannot_bypass_disclosure_rules_by_omitting_fact_reference():
    with pytest.raises(ActorContractError):
        validate_actor_output(
            view(),
            ActorOutput.model_validate(output(text="I have a time-sensitive personal obligation.")),
        )


async def test_settlement_confirmation_with_cost_does_not_repeat_spending_consent():
    result = await ScriptedActor().respond(view("Taxi-2 is booked and settled for 20 credits."))
    assert result.kind == "wait"
    assert result.authorized_amount is None


@pytest.mark.parametrize(
    "text",
    [
        "I will inspect the available vehicles for transport from 09:00 to 10:00.",
        "I will discuss the shared car option with the other assistants on the board.",
        "I will ask for your approval before spending any credits or changing any reservations.",
        "Would you like me to request Owner 1 to release or share the car with you?",
        "Alternatively, you can approve spending 20 credits for one of the available taxis.",
        "Would you like to keep this reservation or consider switching to a taxi?",
        "Would you like to approve reserving one of the remaining taxis now to guarantee "
        "transport, or do you want to wait until the last possible moment for a response "
        "about the shared car?",
        "I will continue to monitor the board for any changes to the shared car status "
        "and will not proceed with a taxi reservation unless the shared car is confirmed "
        "unavailable, as per your instructions.",
        "Taxis 4–15 are still available for reservation at 20 credits each, which is within "
        "your budget.",
        "Please advise if you wish to approve reserving one of the available taxis for 20 credits.",
        "I will check the reservation status and inform you when it changes.",
        "I will inform you about updates to the shared car discussion.",
    ],
)
def test_real_actor_role_reversals_are_rejected(text):
    with pytest.raises(ActorContractError, match="Role reversal"):
        validate_actor_output(view(), ActorOutput.model_validate(output(text=text)))


async def test_model_repairs_owner_role_reversal():
    model = Responses(
        [
            output(text="I will inspect the available vehicles."),
            output(text="Please arrange my transport. Ask me before spending credits."),
        ]
    )
    actor = ModelActor(model)
    response = await actor.respond(view())
    assert response.text.startswith("Please arrange my")
    assert len(actor.last_attempts) == 2
    assert "Role reversal" in actor.last_attempts[0]["reason"]
    assert "Write the next OWNER dialogue line in a screenplay" in model.calls[0][0].content
    assert "Never perform the transport request yourself" in model.calls[0][0].content
    data = json.loads(model.calls[0][1].content)
    assert list(data)[-1] == "response_role"
    assert "Owner 2 (owner-2), the human OWNER" in data["response_role"]
    assert "human OWNER character's first-person dialogue" in model.calls[0][0].content


@pytest.mark.parametrize(
    "text",
    [
        "Please continue to monitor the board for any changes regarding the shared car. "
        "Notify me immediately if the shared car becomes available. No credits are to be "
        "spent unless I approve it.",
        "I approve reserving any one of the available taxis for 20 credits.",
        "Please check the reservation status and inform me when it changes.",
        "I will keep you informed about changes to my schedule.",
    ],
)
def test_owner_instructions_and_decisions_still_pass_role_guard(text):
    response = ActorOutput.model_validate(output(text=text))
    assert validate_actor_output(view(), response) == response


def test_owner_can_withdraw_all_consent():
    response = ActorOutput.model_validate(
        output(text="I withdraw my spending consent.", authorized_amount=0, allowed_assets=[])
    )
    assert validate_actor_output(view(), response).authorized_amount == 0


async def test_optional_semantic_review_repairs_subtle_owner_fidelity_error():
    model = Responses(
        [output(text="Your travel is my assignment."), output(text="Please arrange my transport.")]
    )
    reviewer = Responses(
        [
            {"accepted": False, "issues": ["The speaker presents itself as a service provider."]},
            {"accepted": True, "issues": []},
        ]
    )
    actor = ModelActor(model, reviewer=reviewer)
    result = await actor.respond(view())
    assert result.text == "Please arrange my transport."
    assert len(model.calls) == len(reviewer.calls) == 2


async def test_release_requires_real_alternative_and_operation_consent():
    spec = vehicle_scenario()
    query = [
        {
            "id": "ask",
            "role": "assistant",
            "text": "May I release shared-car now that taxi-1 is confirmed?",
        }
    ]
    unproved = build_actor_view(spec, "owner-1", "Respond", conversation=query)
    assert (await ScriptedActor().respond(unproved)).authorized_amount is None
    observed = build_actor_view(
        spec,
        "owner-1",
        "Respond",
        conversation=query,
        observations=[
            {
                "id": "world",
                "kind": "current_world",
                "text": json.dumps(
                    {
                        "owner_id": "owner-1",
                        "reservations": [
                            {"owner_id": "owner-1", "asset_id": "shared-car"},
                            {"owner_id": "owner-1", "asset_id": "taxi-1"},
                        ],
                    }
                ),
            }
        ],
    )
    consent = await ScriptedActor().respond(observed)
    assert consent.authorized_amount == 0
    assert consent.allowed_assets == ("shared-car",)
    assert consent.allowed_kinds == ("cancel",)


def test_model_cannot_cancel_without_confirmed_alternative():
    owner_view = build_actor_view(
        vehicle_scenario(), "owner-1", "Respond", source_event_id="event-1"
    )
    with pytest.raises(ActorContractError, match="alternative"):
        validate_actor_output(
            owner_view,
            ActorOutput.model_validate(
                output(
                    text="I approve cancellation of shared-car.",
                    authorized_amount=0,
                    allowed_assets=["shared-car"],
                    allowed_kinds=["cancel"],
                )
            ),
        )


def test_internal_fact_reference_alone_does_not_disclose_its_private_text():
    reply = ActorOutput.model_validate(
        output(text="Please arrange my transport.", fact_ids=["need-2"])
    )
    assert validate_actor_output(view(), reply) == reply


async def test_actor_review_accepts_owner_request_and_records_candidate():
    correct = output(text="Please arrange my transport. Ask me before spending credits.")
    model = Responses([correct])
    reviewer = Responses([{"accepted": True, "issues": []}])
    actor = ModelActor(model, reviewer=reviewer)
    await actor.respond(view())
    prompt = reviewer.calls[0][0].content
    assert "repeating or rephrasing that request is CORRECT" in prompt
    assert "Do not require the owner to demonstrate assistant work" in prompt
    assert actor.last_attempts[0]["candidate"]["text"] == correct["text"]
    assert actor.last_attempts[0]["review"]["accepted"]


async def test_rejected_role_candidate_is_retained_for_audit():
    bad = output(text="I will inspect available vehicles.")
    actor = ModelActor(Responses([bad, output(text="Please arrange my transport.")]))
    await actor.respond(view())
    assert actor.last_attempts[0]["candidate"]["text"] == bad["text"]


def test_conversation_accepts_the_api_limit_without_truncation():
    long_question = "x" * 12000
    assert view(long_question).conversation[0].text == long_question
    with pytest.raises(ValidationError):
        view("x" * 12001)


async def test_model_response_correlation_is_bound_to_saved_input_in_code():
    model = Responses([output()])
    actor = ModelActor(model)
    response = await actor.respond(view().model_copy(update={"source_event_id": "trusted-event"}))
    assert response.source_event_id == "trusted-event"
    assert "source_event_id" not in model.calls[0][0].content
    assert "source_event_id" not in model.calls[0][1].content
    assert actor.last_attempts[0]["candidate"]["source_event_id"] == "trusted-event"


async def test_parsed_candidate_with_invalid_field_combination_is_retained():
    actor = ModelActor(
        Responses(
            [
                output(authorized_amount=20, allowed_assets=[]),
                output(),
            ]
        )
    )
    await actor.respond(view())
    assert actor.last_attempts[0]["candidate"]["authorized_amount"] == 20
    assert actor.last_attempts[0]["candidate"]["allowed_assets"] == []
    assert actor.last_attempts[0]["failure_kind"] == "schema_invalid"
    assert "explicit assets" in actor.last_attempts[0]["reason"]


async def test_missing_model_proposal_id_binds_to_latest_assistant_message_only():
    actor_view = build_actor_view(
        vehicle_scenario(),
        "owner-2",
        "Reply.",
        [
            {"id": "old-message", "role": "assistant", "text": "I will look for transport."},
            {
                "id": "latest-proposal",
                "role": "assistant",
                "text": "Please let me know if you approve reserving taxi-2 for 20 credits.",
            },
        ],
        source_event_id="event-1",
    )
    generated = output(
        kind="decide_proposal",
        text="I approve taxi-2 for 20 credits.",
        decision="accept",
        proposal_id=None,
        authorized_amount=20,
        allowed_assets=["taxi-2"],
        allowed_kinds=["reserve"],
    )
    model = Responses([generated])
    actor = ModelActor(model)
    response = await actor.respond(actor_view)
    assert response.proposal_id == "latest-proposal"
    assert response.authorized_amount == 20 and response.decision == "accept"
    assert response.allowed_assets == ("taxi-2",) and response.allowed_kinds == ("reserve",)
    assert len(model.calls) == 1
    assert actor.last_attempts[0]["generated_candidate"]["proposal_id"] is None
    assert actor.last_attempts[0]["metadata_bindings"] == {"proposal_id": "latest-proposal"}


@pytest.mark.parametrize("proposal_id", [None, "unseen-proposal"])
async def test_model_proposal_binding_does_not_create_or_replace_missing_evidence(proposal_id):
    actor_view = view() if proposal_id is None else view("Please approve taxi-2 for 20 credits.")
    generated = output(
        kind="decide_proposal",
        text="I approve taxi-2.",
        decision="accept",
        proposal_id=proposal_id,
        authorized_amount=20,
        allowed_assets=["taxi-2"],
    )
    actor = ModelActor(Responses([generated]), max_repairs=0)
    with pytest.raises(ActorContractError):
        await actor.respond(actor_view)
    assert not actor.last_attempts[0]["metadata_bindings"]
    assert actor.last_attempts[0]["failure_kind"] == "actor_contract"


async def test_actor_schema_repair_has_safe_exact_field_feedback():
    model = Responses([output(authorized_amount="PRIVATE-INPUT-SENTINEL"), output()])
    actor = ModelActor(model)
    await actor.respond(view())
    failure = actor.last_attempts[0]
    assert failure["failure_kind"] == "schema_invalid"
    issue = failure["issues"][0]
    assert issue["location"] == "authorized_amount"
    assert issue["type"] == "int_type"
    assert set(issue) == {"location", "type", "message"}
    assert "PRIVATE-INPUT-SENTINEL" not in json.dumps(failure["issues"])
    repair = json.loads(model.calls[1][1].content)["repair_reason"]
    assert "authorized_amount (int_type)" in repair
    assert "PRIVATE-INPUT-SENTINEL" not in repair


@pytest.mark.parametrize("mode", ["always", "on_request"])
def test_identical_fact_and_summary_can_be_disclosed_with_permission_and_reference(mode):
    base = view("May I share why you need transport?")
    fact = next(item for item in base.known_facts if item.id == "need-2")
    rule = base.disclosures[0].model_copy(update={"mode": mode, "summary": fact.text})
    actor_view = base.model_copy(update={"disclosures": (rule,)})
    response = ActorOutput.model_validate(output(text=fact.text, fact_ids=[fact.id]))
    assert validate_actor_output(actor_view, response) == response
    with pytest.raises(ActorContractError, match="fact reference"):
        validate_actor_output(actor_view, response.model_copy(update={"fact_ids": ()}))
    forbidden = rule.model_copy(update={"mode": "never"})
    with pytest.raises(ActorContractError, match="not permitted"):
        validate_actor_output(actor_view.model_copy(update={"disclosures": (forbidden,)}), response)


def test_full_private_fact_remains_forbidden_when_summary_omits_details():
    actor_view = view("May I share why you need transport?")
    fact = next(item for item in actor_view.known_facts if item.id == "need-2")
    response = ActorOutput.model_validate(output(text=fact.text, fact_ids=[fact.id]))
    with pytest.raises(ActorContractError, match="outside its permitted summary"):
        validate_actor_output(actor_view, response)


@pytest.mark.parametrize("decision", ["reject", "counter"])
def test_reject_or_counter_can_withdraw_all_consent(decision):
    response = ActorOutput.model_validate(
        output(
            kind="decide_proposal",
            text="I reject that option and withdraw all consent.",
            decision=decision,
            proposal_id="message-1",
            authorized_amount=0,
            allowed_assets=[],
            allowed_kinds=[],
        )
    )
    assert validate_actor_output(view("May I reserve taxi-2?"), response) == response


@pytest.mark.parametrize("decision", ["reject", "counter"])
@pytest.mark.parametrize(
    "amount,assets,kinds",
    [(20, ["taxi-2"], ["reserve"]), (0, ["shared-car"], ["cancel"]), (0, [], ["reserve"])],
)
def test_reject_or_counter_still_cannot_grant_any_consent(decision, amount, assets, kinds):
    with pytest.raises(ValidationError, match="cannot grant consent"):
        ActorOutput.model_validate(
            output(
                kind="decide_proposal",
                decision=decision,
                proposal_id="message-1",
                authorized_amount=amount,
                allowed_assets=assets,
                allowed_kinds=kinds,
            )
        )
