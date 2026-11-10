"""Current needs survive old completion; supplied reviews cannot hide missing coverage."""

import json

import pytest
from pydantic import ValidationError
from test_actor import Responses

from agentrpg.actor import (
    ActorContractError,
    ActorView,
    ModelActor,
    ModelGenerationError,
    build_actor_view,
)
from agentrpg.community_experiment import DailyMeetingPlan


def current_view(target="14:00", cue_id="change-2", *, required=False):
    plan = DailyMeetingPlan(pairs=2)
    return build_actor_view(
        plan.scene(),
        "bob",
        f"Current private Director cue: I now prefer {target}. Ask my assistant to coordinate.",
        conversation=[
            {"id": "earlier-owner", "role": "owner", "text": "I agree to 16:00."},
            {"id": "earlier-result", "role": "assistant", "text": "Both agreed to 16:00."},
        ],
        observations=[
            {
                "id": cue_id,
                "kind": "private_cue",
                "text": f"I now prefer {target}.",
                "owner_response_required": required,
            }
        ],
        source_event_id="saved-clock-job",
    )


def stimulus(needed=True, cue_id="change-2", target="14:00"):
    return {
        "cues": [
            {
                "cue_id": cue_id,
                "response_needed": needed,
                "material_request": f"Ask my assistant to discuss {target}." if needed else None,
                "reason": "The target changed." if needed else "The target is already agreed.",
            }
        ]
    }


def coverage(*, covered=True, cue_id="change-2"):
    return {
        "accepted": True,
        "issues": [],
        "current_cue_coverage": [
            {
                "cue_id": cue_id,
                "covered": covered,
                "missing_details": [] if covered else ["The new 14:00 request is omitted."],
            }
        ],
    }


def owner(target="14:00"):
    return {"kind": "reply", "text": f"Please ask Alice about changing our meeting to {target}."}


def policy(generations, reviews, **kwargs):
    return ModelActor(
        Responses(generations),
        reviewer=Responses(reviews),
        prompt="Frozen generation policy.",
        review_prompt="Frozen review policy.",
        require_current_cue_review=True,
        **kwargs,
    )


@pytest.mark.parametrize("text", ["", "Thanks; our earlier 16:00 work is done."])
async def test_new_need_cannot_be_silently_accepted_before_independent_coverage(text):
    actor = policy([{"kind": "wait", "text": text}], [stimulus()], max_repairs=0)
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(current_view())
    assert len(actor.model.calls) == len(actor.reviewer.calls) == 1
    assert actor.last_attempts[-1]["stage"] == "current_cue_coverage"
    assert actor.last_attempts[-1]["accepted"] is False
    assert actor.last_attempts[-1]["generated_candidate"]["text"] == text
    classified = json.loads(actor.reviewer.calls[0][1].content)
    assert "candidate" not in classified and "rejected_candidate" not in classified
    assert classified["current_cues"][0]["id"] == "change-2"


async def test_one_repair_expresses_new_need_without_reclassifying_or_scripted_speech():
    actor = policy([{"kind": "wait"}, owner()], [stimulus(), coverage()], max_repairs=1)
    result = await actor.respond(current_view())
    assert result.text == owner()["text"]
    assert result.source_event_id == "saved-clock-job"
    assert len(actor.model.calls) == len(actor.reviewer.calls) == 2
    payload = json.loads(actor.model.calls[-1][1].content)
    assert "require owner dialogue" in payload["repair_reason"]
    assert payload["current_cue_requirements"] == stimulus()
    assert "Frozen generation policy." in actor.model.calls[0][0].content
    assert "Frozen review policy." in actor.reviewer.calls[-1][0].content
    assert actor.last_attempts[-2]["accepted"] is False
    assert actor.last_attempts[-1]["accepted"] is True


async def test_global_accepted_true_cannot_override_uncovered_current_request():
    actor = policy(
        [owner("16:00"), owner()], [stimulus(), coverage(covered=False), coverage()], max_repairs=1
    )
    assert (await actor.respond(current_view())).text == owner()["text"]
    rejected = actor.last_attempts[-2]
    assert rejected["review"]["accepted"] is True and rejected["accepted"] is False
    assert "14:00" in json.loads(actor.model.calls[-1][1].content)["repair_reason"]


@pytest.mark.parametrize("change", ["missing", "foreign", "duplicate"])
async def test_review_must_cover_each_independent_requirement_exactly_once(change):
    review = coverage()
    if change == "missing":
        review["current_cue_coverage"] = []
    elif change == "foreign":
        review["current_cue_coverage"][0]["cue_id"] = "peer-private-cue"
    else:
        review["current_cue_coverage"] *= 2
    actor = policy([owner()], [stimulus(), review], max_repairs=0)
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(current_view())
    assert actor.last_attempts[-1]["accepted"] is False


@pytest.mark.parametrize("change", ["missing", "foreign", "duplicate", "missing_request"])
async def test_classifier_cannot_omit_or_substitute_an_observed_cue(change):
    classification = stimulus()
    if change == "missing":
        classification["cues"] = []
    elif change == "foreign":
        classification["cues"][0]["cue_id"] = "peer-private-cue"
    elif change == "duplicate":
        classification["cues"] *= 2
    else:
        classification["cues"][0]["material_request"] = None
    actor = policy([], [classification], max_repairs=0)
    with pytest.raises(ActorContractError, match="classification.*repair allowance"):
        await actor.respond(current_view())
    assert not actor.model.calls


async def test_already_resolved_target_allows_real_silence_and_no_forced_new_work():
    actor = policy(
        [{"kind": "wait", "text": ""}],
        [stimulus(False, target="16:00"), {"accepted": True, "issues": []}],
    )
    assert (await actor.respond(current_view("16:00"))).kind == "wait"
    assert "current_cue_coverage" not in actor.last_attempts[-1]["review"]


async def test_no_private_cue_keeps_normal_followup_and_avoids_extra_classification():
    view = current_view().model_copy(update={"observations": ()})
    actor = policy([{"kind": "wait"}], [{"accepted": True, "issues": []}])
    assert (await actor.respond(view)).kind == "wait"
    assert len(actor.reviewer.calls) == 1
    assert json.loads(actor.reviewer.calls[0][1].content)["candidate"]["kind"] == "wait"


async def test_repeated_distinct_cues_reclassify_current_target_not_previous_completion():
    actor = policy(
        [owner(), owner("16:00")],
        [
            stimulus(),
            coverage(),
            stimulus(cue_id="change-3", target="16:00"),
            coverage(cue_id="change-3"),
        ],
    )
    first = await actor.respond(current_view())
    next_view = current_view("16:00", "change-3")
    next_view = ActorView.model_validate(
        {
            **next_view.model_dump(mode="json"),
            "conversation": [
                {"id": "request-2", "role": "owner", "text": first.text},
                {"id": "result-2", "role": "assistant", "text": "Both now agree to 14:00."},
            ],
        }
    )
    assert "16:00" in (await actor.respond(next_view)).text
    assert len(actor.model.calls) == 2 and len(actor.reviewer.calls) == 4
    assert "14:00" in json.dumps(json.loads(actor.reviewer.calls[2][1].content)["view"])


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_current_cue_option_is_explicit_boolean(value):
    with pytest.raises(ValueError, match="boolean"):
        ModelActor(Responses([]), require_current_cue_review=value)


def test_current_cue_option_requires_reviewer():
    with pytest.raises(ValueError, match="independent reviewer"):
        ModelActor(Responses([]), require_current_cue_review=True)


async def test_legacy_default_keeps_original_review_schema():
    actor = ModelActor(
        Responses([{"kind": "wait"}]), reviewer=Responses([{"accepted": True, "issues": []}])
    )
    assert (await actor.respond(current_view())).kind == "wait"
    assert len(actor.reviewer.calls) == 1
    assert "current_cue_requirements" not in json.loads(actor.reviewer.calls[0][1].content)


async def test_resolved_cue_in_same_batch_does_not_cancel_a_distinct_new_need():
    view = current_view()
    previous = view.observations[0].model_copy(
        update={"id": "old-notice", "text": "The earlier 16:00 agreement is unchanged."}
    )
    view = view.model_copy(update={"observations": (*view.observations, previous)})
    classification = stimulus()
    classification["cues"] += stimulus(False, "old-notice")["cues"]
    actor = policy([owner()], [classification, coverage()])
    assert (await actor.respond(view)).text == owner()["text"]


async def test_current_cue_classification_is_not_spending_authority():
    candidate = {
        **owner(),
        "transfer_consent": {"recipient_owner_id": "alice", "amount": 1, "memo": "Invented"},
    }
    actor = policy([candidate], [stimulus()], max_repairs=0)
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(current_view())
    assert len(actor.reviewer.calls) == 1


async def test_classifier_provider_failure_is_not_turned_into_a_silent_owner():
    actor = policy([], [RuntimeError("supplied provider failure")])
    with pytest.raises(ModelGenerationError):
        await actor.respond(current_view())
    assert not actor.model.calls


async def test_classifier_cannot_veto_director_declared_communication():
    actor = policy([], [stimulus(False)], max_repairs=0)
    with pytest.raises(ActorContractError, match="classification.*repair allowance"):
        await actor.respond(current_view(required=True))
    assert not actor.model.calls
    assert "Director declares" in actor.last_attempts[-1]["reason"]


async def test_declared_communication_repairs_false_classification_before_generation():
    actor = policy([owner()], [stimulus(False), stimulus(), coverage()], max_repairs=1)
    assert (await actor.respond(current_view(required=True))).text == owner()["text"]
    assert actor.last_attempts[0]["accepted"] is False
    assert actor.last_attempts[1]["accepted"] is True
    assert len(actor.model.calls) == 1 and len(actor.reviewer.calls) == 3


async def test_declared_communication_is_enforced_without_model_cue_review():
    actor = ModelActor(Responses([{"kind": "wait"}]), max_repairs=0)
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(current_view(required=True))


@pytest.mark.parametrize("value", [1, "true", None])
def test_declared_response_contract_rejects_non_boolean_payloads(value):
    with pytest.raises(ValidationError):
        current_view(required=value)


async def test_clock_retains_rejected_cue_and_does_not_advance_or_regenerate(
    pool, app, test_database_url
):
    from uuid import uuid4

    from test_clock_runtime import runtime_case

    from agentrpg.director import DirectorError, row, rows
    from agentrpg.simulation_clock import Schedule, Sleep

    actor = policy([{"kind": "wait"}], [stimulus()], max_repairs=0)
    stack, conductor, run, clock, *_ = await runtime_case(
        pool, app, test_database_url, actors_override={"alice": actor}
    )
    async with stack:
        service = conductor.director
        # The common conductor already parks both Agent lanes at startup.
        await service.control(run, uuid4(), Sleep(process_id="actor-bob", until_seconds=100))
        await service.control(
            run,
            uuid4(),
            Schedule(
                event_key="change-2",
                at_seconds=0,
                kind="private_cue",
                recipients=("actor-alice",),
                payload={"text": "I now prefer 14:00. Ask my assistant."},
            ),
        )
        with pytest.raises(ActorContractError):
            await conductor.advance(run)
        async with pool.connection() as conn:
            jobs = await rows(conn, "SELECT * FROM director_clock_jobs WHERE run_id=%s", (run,))
            saved = await row(
                conn, "SELECT * FROM clock_runtime_actor_outputs WHERE job_id=%s", (jobs[0]["id"],)
            )
            messages = await rows(conn, "SELECT * FROM owner_messages WHERE run_id=%s", (run,))
        assert jobs[0]["status"] == "uncertain" and jobs[0]["output"] is None
        assert saved["output"]["failure_kind"] == "ActorContractError"
        assert saved["diagnostics"][-1]["stage"] == "current_cue_coverage"
        assert not messages
        assert (await service.clock.snapshot(clock))["now_seconds"] == 0
        with pytest.raises(DirectorError, match="uncertain"):
            await conductor.advance(run)
        assert len(actor.model.calls) == len(actor.reviewer.calls) == 1


def test_cue_gate_is_frozen_in_clock_bindings():
    from agentrpg.clock_runtime import ClockRuntime

    actor = policy([], [])
    conductor = ClockRuntime(None, actors={"bob": actor}, agents={})
    before = conductor.bindings()
    actor.require_current_cue_review = False
    assert conductor.bindings() != before
    assert "require_current_cue_review" not in conductor.bindings()["actors"]["bob"]
