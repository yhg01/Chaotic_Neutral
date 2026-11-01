"""Resource request guidance preserves separate content and access decisions."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentrpg.actor import ActorOutput
from agentrpg.director import Director
from agentrpg.resource_schema import ResourceRequest
from agentrpg.resources import ResourceService
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario
from agentrpg.world import WorldError


def test_actor_json_schema_exposes_conditional_resource_shapes_and_valid_examples():
    schema = ActorOutput.model_json_schema()["$defs"]["ResourceRequest"]
    conditions = {
        rule["if"]["properties"]["kind"]["const"]: rule["then"]
        for rule in schema["allOf"]
        if "kind" in rule["if"]["properties"]
    }
    for kind in ("create", "update"):
        assert conditions[kind]["properties"]["visibility"] == {"type": "null"}
        assert conditions[kind]["properties"]["shared_owner_ids"] == {"maxItems": 0}
    assert set(conditions["update"]["required"]) == {"resource_id", "expected_version", "content"}
    assert conditions["share"]["properties"]["content"] == {"type": "null"}
    assert conditions["share"]["properties"]["title"] == {"type": "null"}
    assert conditions["create"]["properties"]["resource_id"] == {"type": "null"}
    assert (
        "even if the resource is currently private"
        in (schema["properties"]["visibility"]["description"])
    )
    assert schema["additionalProperties"] is False
    assert ResourceRequest.model_config["frozen"]
    examples = [ResourceRequest.model_validate(example) for example in schema["examples"]]
    assert [example.kind for example in examples] == ["create", "update", "share"]


@pytest.mark.parametrize("kind", ["create", "update"])
@pytest.mark.parametrize(
    "access", [{"visibility": "private"}, {"visibility": "public"}, {"shared_owner_ids": ["peer"]}]
)
def test_content_requests_reject_access_fields_with_exact_repair(kind, access):
    operation = {"kind": kind, "title": "Note", "content": "Revised text.", **access}
    if kind == "update":
        operation.update(resource_id="note", expected_version=0)
    with pytest.raises(ValidationError) as caught:
        ResourceRequest.model_validate(operation)
    error = str(caught.value)
    assert f"{kind.capitalize()} requires visibility=null and shared_owner_ids=[]." in error
    assert "Do not copy the resource's current visibility" in error
    assert "separate kind='share' request" in error


@pytest.mark.parametrize("field", ["title", "content"])
def test_share_rejects_text_changes_with_exact_repair(field):
    with pytest.raises(ValidationError, match="content=null, title=null"):
        ResourceRequest.model_validate(
            {
                "kind": "share",
                "resource_id": "note",
                "expected_version": 0,
                "visibility": "private",
                field: "Text edit",
            }
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"resource_id": "existing"},
        {"expected_version": 0},
        {"quantity": 1},
        {"kind": "item"},
    ],
)
def test_create_remains_a_private_new_document_without_inventory(extra):
    with pytest.raises(ValidationError):
        ResourceRequest.model_validate(
            {
                "kind": "create",
                "title": "Note",
                "content": "New text.",
                **extra,
            }
        )


@pytest.fixture
async def resource_run(pool):
    data = vehicle_scenario().model_dump(mode="json")
    data["resources"] = [
        {
            "id": "own-note",
            "owner_id": "owner-1",
            "title": "Private note",
            "content": "Original text.",
            "agent_writable": False,
        }
    ]
    director = Director(pool)
    created = await director.create_run(ScenarioSpec.model_validate(data))
    run_id = UUID(created["run_id"])
    await director.control(run_id, "start")
    return run_id, director, ResourceService(pool)


async def test_invalid_update_cannot_change_access_content_version_or_audit(resource_run):
    run_id, director, resources = resource_run
    before = await resources.snapshot(run_id)
    journal = await resources.journal(run_id)
    events = await director.trace(run_id)
    for access in (
        {"visibility": "private"},
        {"visibility": "public"},
        {"visibility": "shared", "shared_owner_ids": ["owner-2"]},
        {"shared_owner_ids": ["owner-2"]},
    ):
        with pytest.raises(WorldError, match="Invalid resource request"):
            await resources.apply_request(
                run_id,
                "owner-1",
                uuid4(),
                {
                    "kind": "update",
                    "resource_id": "own-note",
                    "expected_version": 0,
                    "content": "Must not be stored.",
                    **access,
                },
            )
        assert await resources.snapshot(run_id) == before
        assert await resources.journal(run_id) == journal
        assert await director.trace(run_id) == events
        with pytest.raises(WorldError, match="not found"):
            await resources.fetch(run_id, "owner-2", "own-note")


async def test_owner_update_preserves_access_until_separate_authorized_share(resource_run):
    run_id, _, resources = resource_run
    update = {
        "kind": "update",
        "resource_id": "own-note",
        "expected_version": 0,
        "content": "Owner revision.",
        "visibility": None,
        "shared_owner_ids": [],
    }
    with pytest.raises(WorldError, match="read-only"):
        await resources.apply_request(run_id, "owner-1", uuid4(), update, trusted=False)
    changed = await resources.apply_request(run_id, "owner-1", uuid4(), update)
    assert changed["content"] == "Owner revision."
    assert changed["version"] == 1
    assert changed["visibility"] == "private" and changed["shared_owner_ids"] == []
    with pytest.raises(WorldError, match="not found"):
        await resources.fetch(run_id, "owner-2", "own-note")
    share = {
        "kind": "share",
        "resource_id": "own-note",
        "expected_version": 1,
        "title": None,
        "content": None,
        "visibility": "shared",
        "shared_owner_ids": ["owner-2"],
    }
    with pytest.raises(WorldError, match="Only the owner"):
        await resources.apply_request(run_id, "owner-1", uuid4(), share, trusted=False)
    with pytest.raises(WorldError, match="not found"):
        await resources.apply_request(run_id, "owner-2", uuid4(), share)
    changed = await resources.apply_request(run_id, "owner-1", uuid4(), share)
    assert changed["version"] == 2
    assert (await resources.fetch(run_id, "owner-2", "own-note"))["content"] == "Owner revision."
    changed = await resources.apply_request(
        run_id,
        "owner-1",
        uuid4(),
        {
            **update,
            "expected_version": 2,
            "content": "Another owner revision.",
        },
    )
    assert changed["visibility"] == "shared" and changed["shared_owner_ids"] == ["owner-2"]
    assert changed["version"] == 3
    assert await resources.snapshot(run_id) == resources.replay(await resources.journal(run_id))
