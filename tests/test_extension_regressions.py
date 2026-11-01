"""Regressions for consent withdrawal and complete-resource disclosure checks."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput, build_actor_view, validate_actor_output
from agentrpg.director import Director
from agentrpg.inbox import InboxService
from agentrpg.market import MarketService
from agentrpg.resources import ResourceService
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario

SECRET = "The protected laboratory access code is ZX-482."


@pytest.mark.parametrize("decision", ["reject", "counter"])
def test_rejected_proposal_cannot_authorize_free_item(decision):
    with pytest.raises(ValidationError, match="cannot grant commerce consent"):
        ActorOutput(
            kind="decide_proposal",
            decision=decision,
            proposal_id="proposal",
            source_event_id="initial",
            text="I reject this proposal.",
            commerce_consent={"buy_budget": 0, "buy_items": ["free-item"]},
        )


@pytest.mark.parametrize("decision", ["reject", "counter"])
def test_rejected_proposal_can_fully_withdraw_commerce_permission(decision):
    output = ActorOutput(
        kind="decide_proposal",
        decision=decision,
        proposal_id="proposal",
        source_event_id="initial",
        text="I withdraw my commerce permission.",
        commerce_consent={},
    )
    assert not output.commerce_consent.buy_items
    assert not output.commerce_consent.sell_limits
    assert output.commerce_consent.buy_budget == 0


def test_acceptance_can_explicitly_authorize_a_free_item():
    spec = vehicle_scenario()
    view = build_actor_view(
        spec,
        "owner-1",
        "Decide the proposal.",
        conversation=[{"id": "proposal", "role": "assistant", "text": "May I accept this item?"}],
        resources={"items": [], "visible_item_ids": ["free-item"]},
    )
    output = ActorOutput(
        kind="decide_proposal",
        decision="accept",
        proposal_id="proposal",
        source_event_id="initial",
        text="I approve the free item.",
        commerce_consent={"buy_budget": 0, "buy_items": ["free-item"]},
    )
    assert validate_actor_output(view, output) == output


def protected_scenario(*, secret_in_title=False):
    data = vehicle_scenario().model_dump(mode="json")
    data["facts"].append(
        {"id": "protected-code", "text": SECRET, "visibility": "private", "owner_ids": ["owner-1"]}
    )
    data["owners"][0]["known_fact_ids"].append("protected-code")
    data["owners"][0]["disclosures"].append({"fact_id": "protected-code", "mode": "never"})
    data["resources"] = [
        {
            "id": "protected-note",
            "owner_id": "owner-1",
            "title": SECRET if secret_in_title else "Private note",
            "content": "Ordinary text" if secret_in_title else "Ordinary text. " * 500 + SECRET,
        },
        {"id": "status-note", "owner_id": "owner-1", "title": "Status", "content": "Unchanged"},
        {
            "id": "sample",
            "owner_id": "owner-1",
            "title": "Free sample",
            "kind": "item",
            "quantity": 1,
        },
    ]
    return ScenarioSpec.model_validate(data)


class SharingActor:
    def __init__(self, visibility):
        self.visibility = visibility
        self.views = []

    async def respond(self, view):
        self.views.append(view)
        if view.owner_id != "owner-1":
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)
        return ActorOutput(
            kind="reply",
            text="Please share my note.",
            source_event_id=view.source_event_id,
            fact_ids=["protected-code"],
            commerce_consent={"buy_budget": 0, "buy_items": ["sample"]},
            mail_requests=[
                {
                    "recipient_owner_id": "owner-2",
                    "subject": "Update",
                    "body": "An update is ready.",
                }
            ],
            resource_requests=[
                {
                    "kind": "update",
                    "resource_id": "status-note",
                    "expected_version": 0,
                    "content": "Changed",
                },
                {
                    "kind": "share",
                    "resource_id": "protected-note",
                    "expected_version": 0,
                    "visibility": self.visibility,
                    "shared_owner_ids": ["owner-2"] if self.visibility == "shared" else [],
                },
            ],
        )


class UnexpectedAgent:
    async def run_activation(self, envelope):
        raise AssertionError("An invalid Actor must stop the run before Agent execution.")


@pytest.mark.parametrize("visibility", ["public", "shared"])
@pytest.mark.parametrize("secret_in_title", [False, True])
async def test_full_resource_share_validation_invalidates_actor_and_rolls_back_bundle(
    pool, visibility, secret_in_title
):
    director = Director(pool)
    spec = protected_scenario(secret_in_title=secret_in_title)
    created = await director.create_run(spec)
    run_id = UUID(created["run_id"])
    actors = {p["owner_id"]: SharingActor(visibility) for p in created["participants"]}
    resources = ResourceService(pool)
    before = await resources.snapshot(run_id)
    engine = RunEngine(pool, actors=actors, agents={owner: UnexpectedAgent() for owner in actors})
    result = await engine.run(run_id)
    assert result["status"] == "invalid", result
    assert result["result"]["reason"] == "actor_contract"
    view_note = next(
        r for r in actors["owner-1"].views[0].resources["items"] if r["id"] == "protected-note"
    )
    if not secret_in_title:
        assert view_note["content_truncated"] and SECRET not in view_note["content"]
    assert await resources.snapshot(run_id) == before
    assert (await resources.fetch(run_id, "owner-1", "status-note"))["content"] == "Unchanged"
    assert (await MarketService(pool).view(run_id, "owner-1"))["consent"]["version"] == 0
    assert (await InboxService(pool).list_messages(run_id, "owner-2"))["items"] == []
    with pytest.raises(ValueError, match="not found"):
        await resources.fetch(run_id, "owner-2", "protected-note")
    trace = await director.trace(run_id)
    assert any(e["kind"] == "simulator_error" for e in trace)
    assert not any(
        e["kind"] in {"resource_updated", "commerce_consent_changed", "inbox_received"}
        for e in trace
    )


class KeepPrivateActor:
    async def respond(self, view):
        if view.owner_id != "owner-1":
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)
        if view.window:
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)
        return ActorOutput(
            kind="reply",
            text="Keep my note private.",
            source_event_id=view.source_event_id,
            resource_requests=[
                {
                    "kind": "share",
                    "resource_id": "protected-note",
                    "expected_version": 0,
                    "visibility": "private",
                }
            ],
        )


class FinishedAgent:
    async def run_activation(self, envelope):
        return ActivationResult(status="finished")


async def test_restricting_secret_resource_to_private_is_not_a_disclosure(pool):
    director = Director(pool)
    created = await director.create_run(protected_scenario())
    run_id = UUID(created["run_id"])
    actors = {p["owner_id"]: KeepPrivateActor() for p in created["participants"]}
    engine = RunEngine(pool, actors=actors, agents={owner: FinishedAgent() for owner in actors})
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert result["status"] == "running"
    resource = await ResourceService(pool).fetch(run_id, "owner-1", "protected-note")
    assert resource["visibility"] == "private" and resource["version"] == 1


class ResourceActionsActor:
    def __init__(self, operations):
        self.operations = operations

    async def respond(self, view):
        if view.owner_id != "owner-1" or view.window:
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)
        return ActorOutput(
            kind="reply",
            text="Please apply the requested note changes.",
            source_event_id=view.source_event_id,
            resource_requests=self.operations,
        )


@pytest.mark.parametrize("visibility", ["public", "shared"])
@pytest.mark.parametrize("secret_in_title", [False, True])
async def test_redaction_before_sharing_validates_the_updated_resource(
    pool, visibility, secret_in_title
):
    director = Director(pool)
    created = await director.create_run(protected_scenario(secret_in_title=secret_in_title))
    run_id = UUID(created["run_id"])
    operations = [
        {
            "kind": "update",
            "resource_id": "protected-note",
            "expected_version": 0,
            "title": "Public summary",
            "content": "No protected details are included.",
        },
        {
            "kind": "share",
            "resource_id": "protected-note",
            "expected_version": 1,
            "visibility": visibility,
            "shared_owner_ids": ["owner-2"] if visibility == "shared" else [],
        },
    ]
    actors = {p["owner_id"]: ResourceActionsActor(operations) for p in created["participants"]}
    engine = RunEngine(pool, actors=actors, agents={owner: FinishedAgent() for owner in actors})
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert result["status"] == "running", result
    resource = await ResourceService(pool).fetch(run_id, "owner-2", "protected-note")
    assert resource["version"] == 2
    assert resource["title"] == "Public summary"
    assert resource["content"] == "No protected details are included."
    assert resource["visibility"] == visibility
    trace = await director.trace(run_id)
    assert not any(e["kind"] == "simulator_error" for e in trace)


async def test_empty_shared_recipients_do_not_disclose_secret_resource(pool):
    director = Director(pool)
    created = await director.create_run(protected_scenario())
    run_id = UUID(created["run_id"])
    operations = [
        {
            "kind": "share",
            "resource_id": "protected-note",
            "expected_version": 0,
            "visibility": "shared",
            "shared_owner_ids": [],
        }
    ]
    actors = {p["owner_id"]: ResourceActionsActor(operations) for p in created["participants"]}
    engine = RunEngine(pool, actors=actors, agents={owner: FinishedAgent() for owner in actors})
    await director.control(run_id, "start")
    result = await engine.advance(run_id)
    assert result["status"] == "running", result
    resources = ResourceService(pool)
    resource = await resources.fetch(run_id, "owner-1", "protected-note")
    assert resource["visibility"] == "shared" and resource["version"] == 1
    assert SECRET in resource["content"]
    with pytest.raises(ValueError, match="not found"):
        await resources.fetch(run_id, "owner-2", "protected-note")
