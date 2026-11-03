"""Do not drop an unresolved owner repayment task when its amount is unknown."""

from uuid import UUID

import pytest
from test_actor_mail_intent import Responses, review_schema
from test_actor_transfer_authority import TERMS, judgment
from test_actor_transfer_task_intent import candidate, view

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorContractError, ModelActor, ScriptedActor
from agentrpg.director import Director
from agentrpg.run_engine import RunEngine
from agentrpg.transfer_tasks import TransferTaskService
from agentrpg.transfer_validation import validation_scenario

REF = {"task_key": "lunch", "recipient_owner_id": "bob"}


def coverage(refs=None):
    return {
        "accepted": True,
        "issues": [],
        "outgoing_task_coverage": [REF] if refs is None else refs,
    }


async def test_omitted_unknown_amount_task_is_repaired_without_a_payment_grant():
    model = Responses([candidate(outgoing=True, tasks=False), candidate(outgoing=True)])
    reviewer = Responses([coverage(), coverage()])
    actor = ModelActor(model, reviewer=reviewer, require_transfer_task_coverage_review=True)
    result = await actor.respond(view("alice", "ambiguous"))
    assert result.transfer_task_requests[0].task_key == "lunch"
    assert result.transfer_consent is None
    assert not actor.last_attempts[0]["accepted"]
    assert "no matching persistent task" in actor.last_attempts[0]["reason"]
    assert "outgoing_task_coverage" in review_schema(reviewer)["required"]


@pytest.mark.parametrize("state", ["active", "blocked"])
async def test_existing_live_task_covers_followup_without_reopening(state):
    current = view("alice", "ambiguous").model_copy(
        update={
            "transfers": {
                "task_tracking_enabled": True,
                "tasks": [{**REF, "status": state}],
                "authorizations": [],
            }
        }
    )
    actor = ModelActor(
        Responses([candidate(outgoing=True, tasks=False)]),
        reviewer=Responses([coverage()]),
        require_transfer_task_coverage_review=True,
    )
    assert not (await actor.respond(current)).transfer_task_requests


@pytest.mark.parametrize(
    "review_value",
    [
        {"accepted": True, "issues": []},
        coverage([REF, REF]),
        coverage([{**REF, "recipient_owner_id": "unknown"}]),
    ],
)
async def test_missing_duplicate_or_uncovered_reference_fails_closed(review_value):
    actor = ModelActor(
        Responses([candidate(outgoing=True)]),
        reviewer=Responses([review_value]),
        require_transfer_task_coverage_review=True,
        max_repairs=0,
    )
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view("alice", "ambiguous"))
    assert not actor.last_attempts[0]["accepted"]


async def test_incoming_monitoring_keeps_dialogue_and_does_not_need_outgoing_task():
    actor = ModelActor(
        Responses([candidate(tasks=False)]),
        reviewer=Responses([coverage([])]),
        require_transfer_task_coverage_review=True,
    )
    result = await actor.respond(view("bob", "ambiguous"))
    assert result.text and not result.transfer_task_requests and result.transfer_consent is None


async def test_coverage_composes_with_direction_and_exact_authority_review():
    actor = ModelActor(
        Responses([{**candidate(outgoing=True), "transfer_consent": TERMS}]),
        reviewer=Responses([{**judgment(), "outgoing_task_coverage": [REF]}]),
        require_transfer_task_intent_review=True,
        require_transfer_authority_review=True,
        require_transfer_task_coverage_review=True,
    )
    assert (await actor.respond(view("alice"))).transfer_consent.model_dump() == TERMS


@pytest.mark.parametrize("flag", [None, "true", 0, 1])
def test_coverage_flag_requires_boolean(flag):
    with pytest.raises(ValueError, match="must be a boolean"):
        ModelActor(
            Responses([]), reviewer=Responses([]), require_transfer_task_coverage_review=flag
        )


def test_coverage_requires_reviewer():
    with pytest.raises(ValueError, match="requires an Actor reviewer"):
        ModelActor(Responses([]), require_transfer_task_coverage_review=True)


async def test_repaired_missing_task_commits_through_real_director_without_authority(pool):
    director = Director(pool)
    manifest = await director.create_run(validation_scenario("ambiguous"))
    run = UUID(manifest["run_id"])
    await TransferTaskService(pool).enable(run)
    alice = ModelActor(
        Responses([candidate(outgoing=True, tasks=False), candidate(outgoing=True)]),
        reviewer=Responses([coverage(), coverage()]),
        require_transfer_task_coverage_review=True,
    )

    class Finished:
        async def run_activation(self, envelope):
            return ActivationResult(status="finished")

    engine = RunEngine(
        pool,
        actors={"alice": alice, "bob": ScriptedActor()},
        agents={"alice": Finished(), "bob": Finished()},
    )
    await director.control(run, "start")
    await engine.advance(run)
    tasks = await TransferTaskService(pool).list(run, "alice")
    assert len(tasks) == 1 and tasks[0]["status"] == "blocked"
    assert tasks[0]["authorization_id"] is None and tasks[0]["payment_request_id"] is None
    assert len(await director.messages(run, "alice")) == 1
