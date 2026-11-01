"""Branch lineage and user JSON survive complete simulation state copies."""

import json
from contextlib import AsyncExitStack
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from psycopg.types.json import Jsonb
from test_console_execution import create_run
from test_deep_agent import call
from test_run_branches import make_agents

from agentrpg.actor import ScriptedActor
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, fingerprint, row, rows
from agentrpg.resources import ResourceService
from agentrpg.run_branches import capture_boundary, remap
from agentrpg.run_engine import RunEngine


@pytest.fixture
def branch_root(app, tmp_path):
    app.state.console_root = tmp_path
    return tmp_path


def branch_path(run_id):
    return f"/api/v1/console/runs/{run_id}/branches"


async def test_branch_of_branch_records_immediate_parent_in_trace(client, pool, branch_root):
    parent = await create_run(pool, branch_root)
    body = {"request_id": str(uuid4()), "window": 0}
    first = await client.post(branch_path(parent["run_id"]), json=body)
    assert first.status_code == 200, first.text
    first_id = UUID(first.json()["run_id"])
    await capture_boundary(pool, first_id)
    second = await client.post(branch_path(first_id), json=body)
    assert second.status_code == 200, second.text
    second_id = UUID(second.json()["run_id"])
    retry = await client.post(branch_path(first_id), json=body)
    assert retry.status_code == 200 and retry.json()["run_id"] == str(second_id)
    assert retry.json()["replayed"]
    async with pool.connection() as conn:
        events = await rows(
            conn,
            "SELECT operation_key,payload FROM run_events WHERE run_id=%s AND kind='run_branched'",
            (second_id,),
        )
        lineage = await rows(
            conn, "SELECT parent_run_id FROM run_branches WHERE child_run_id=%s", (second_id,)
        )
    assert len(events) == 2
    assert len({event["operation_key"] for event in events}) == 2
    assert {event["payload"]["parent_run_id"] for event in events} == {
        parent["run_id"],
        str(first_id),
    }
    assert lineage == [{"parent_run_id": first_id}]


async def test_json_document_and_scratch_file_are_preserved_on_branch(
    client, app, pool, branch_root, test_database_url
):
    manifest = await create_run(pool, branch_root, count=1)
    run_id = UUID(manifest["run_id"])
    document = '{"next_cursor":"business value","nested":{"next_cursor":"keep"},"items":[1,2]}'
    async with AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url))
        agents, _ = await make_agents(
            stack,
            app,
            runtime,
            manifest,
            [
                call("write_file", {"file_path": "/record.json", "content": document}),
                AIMessage(content="The record is saved."),
            ],
        )
        engine = RunEngine(pool, actors={"owner-1": ScriptedActor()}, agents=agents)
        await Director(pool).control(run_id, "start")
        advanced = await engine.advance(run_id)
        assert advanced["current_window"] == 1
        await ResourceService(pool).create_document(
            run_id, "owner-1", uuid4(), "JSON business record", document
        )
        await capture_boundary(pool, run_id, agents)
        response = await client.post(
            branch_path(run_id),
            json={
                "request_id": str(uuid4()),
                "window": 1,
            },
        )
        assert response.status_code == 200, response.text
        child_id = UUID(response.json()["run_id"])
        child = json.loads(
            (branch_root / ".local/scenario-runs" / str(child_id) / "credentials.json").read_text()
        )
        async with pool.connection() as conn:
            resources = await rows(
                conn,
                "SELECT content FROM owner_resources WHERE run_id=%s "
                "AND title='JSON business record'",
                (child_id,),
            )
        assert resources == [{"content": document}]
        child_agents, _ = await make_agents(stack, app, runtime, child, [])
        graph = child_agents["owner-1"]
        saved = await graph.graph.aget_state(graph.config)
        content = saved.values["files"]["/record.json"]["content"]
        text = "\n".join(content) if isinstance(content, list) else content
        assert text == document


def test_only_known_tool_page_cursor_is_reset():
    document = {"next_cursor": "business value", "nested": {"next_cursor": "keep"}}
    assert remap(document, {}) == document
    assert remap(json.dumps(document), {}) == json.dumps(document)
    page = ToolMessage(
        name="list_threads",
        tool_call_id="page-call",
        content=json.dumps(
            {
                "ok": True,
                "data": {"next_cursor": "old-scoped-page", "items": [document]},
            }
        ),
    )
    mapped = json.loads(remap(page, {}).content)
    assert mapped["data"]["next_cursor"] is None
    assert mapped["data"]["branch_pagination_reset_required"]
    assert mapped["data"]["items"] == [document]
    resource = ToolMessage(
        name="fetch_resource",
        tool_call_id="read-call",
        content=json.dumps(
            {
                "ok": True,
                "data": {"content": document},
            }
        ),
    )
    assert remap(resource, {}).content == resource.content


@pytest.mark.parametrize("legacy", [False, True])
async def test_branch_restores_pending_prompt_without_later_prompt(
    client, pool, branch_root, legacy
):
    from agentrpg.activation import ActivationResult
    from agentrpg.console_execution import apply_interventions

    parent = await create_run(pool, branch_root, count=1)
    parent_id = UUID(parent["run_id"])
    accepted = await client.post(
        f"/api/v1/console/runs/{parent_id}/interventions",
        json={
            "request_id": str(uuid4()),
            "owner_id": "owner-1",
            "target": "agent",
            "prompt": "Retain the private request accepted before this boundary.",
            "expected_window": 0,
        },
    )
    assert accepted.status_code == 201
    original = accepted.json()
    await capture_boundary(pool, parent_id)
    if legacy:
        # Version 1 boundaries written before this extension omitted this table.
        async with pool.connection() as conn:
            boundary = await row(
                conn,
                "SELECT snapshot FROM run_boundaries WHERE run_id=%s AND window_index=0",
                (parent_id,),
            )
            snapshot = boundary["snapshot"]
            snapshot["tables"].pop("console_interventions", None)
            await conn.execute(
                "UPDATE run_boundaries SET snapshot=%s,snapshot_hash=%s "
                "WHERE run_id=%s AND window_index=0",
                (Jsonb(snapshot), fingerprint(snapshot), parent_id),
            )
    # The original now applies the prompt and accepts a second, later prompt.
    await Director(pool).control(parent_id, "start")
    async with pool.connection() as conn, conn.transaction():
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (parent_id,))
        await apply_interventions(conn, run, 0)
    later = await client.post(
        f"/api/v1/console/runs/{parent_id}/interventions",
        json={
            "request_id": str(uuid4()),
            "owner_id": "owner-1",
            "target": "agent",
            "prompt": "Later future input must not enter the earlier branch.",
            "expected_window": 0,
        },
    )
    assert later.status_code == 201 and later.json()["effective_window"] == 1
    branched = await client.post(
        branch_path(parent_id),
        json={
            "request_id": str(uuid4()),
            "window": 0,
        },
    )
    assert branched.status_code == 200, branched.text
    child_id = UUID(branched.json()["run_id"])
    async with pool.connection() as conn:
        child_prompts = await rows(
            conn, "SELECT * FROM console_interventions WHERE run_id=%s", (child_id,)
        )
    assert len(child_prompts) == 1
    prompt = child_prompts[0]
    assert prompt["status"] == "pending" and prompt["applied_at"] is None
    assert str(prompt["id"]) != original["id"]
    assert str(prompt["request_id"]) == original["request_id"]
    assert prompt["effective_window"] == 0 and prompt["prompt"] == original["prompt"]
    received = []

    class Agent:
        async def run_activation(self, envelope):
            received.extend(envelope.owner_messages)
            return ActivationResult(status="finished")

    await Director(pool).control(child_id, "resume")
    await RunEngine(pool, actors={"owner-1": ScriptedActor()}, agents={"owner-1": Agent()}).advance(
        child_id
    )
    assert sum(item.content == original["prompt"] for item in received) == 1
    assert all("Later future" not in item.content for item in received)
    async with pool.connection() as conn, conn.transaction():
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (child_id,))
        await apply_interventions(conn, run, 0)
        messages = await rows(
            conn,
            "SELECT content FROM owner_messages WHERE run_id=%s AND request_id=%s",
            (child_id, prompt["id"]),
        )
        source_prompts = await rows(
            conn,
            "SELECT status FROM console_interventions WHERE run_id=%s ORDER BY created_at",
            (parent_id,),
        )
    assert messages == [{"content": original["prompt"]}]
    assert source_prompts == [{"status": "applied"}, {"status": "pending"}]
