"""Turn branches preserve past world and graph state without future-state leakage."""

import asyncio
import json
from contextlib import AsyncExitStack
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from psycopg.types.json import Jsonb
from test_console_execution import create_run
from test_deep_agent import ScriptedModel, call

from agentrpg.actor import ScriptedActor
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, row, save_message
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxService
from agentrpg.mail_schema import InitialMailSpec
from agentrpg.resources import ResourceService
from agentrpg.run_branches import capture_boundary, remap
from agentrpg.run_engine import RunEngine


@pytest.fixture
def root(app, tmp_path):
    app.state.console_root = tmp_path
    return tmp_path


def path(manifest, suffix="branches"):
    return f"/api/v1/console/runs/{manifest['run_id']}/{suffix}"


async def test_initial_restart_mail_cues_budget_and_idempotency(client, pool, root):
    manifest = await create_run(pool, root)
    run_id = UUID(manifest["run_id"])
    # Add initial mail to the frozen fixture before execution.
    spec = (await Director(pool).get_run(run_id))["scenario"]
    spec["initial_mail"] = [
        InitialMailSpec(
            id="first", recipient_owner_id="owner-1", subject="Notice", body="Private initial mail"
        ).model_dump(mode="json")
    ]
    from agentrpg.scenario_schema import ScenarioSpec

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET scenario=%s,scenario_hash=%s WHERE id=%s",
            (Jsonb(spec), ScenarioSpec.model_validate(spec).content_hash(), run_id),
        )
    body = {"request_id": str(uuid4()), "window": 0}
    first, second = await asyncio.gather(
        *(client.post(path(manifest), json=body) for _ in range(2))
    )
    assert first.status_code == second.status_code == 200, first.text
    assert first.json()["run_id"] == second.json()["run_id"]
    child_id = UUID(first.json()["run_id"])
    child = await Director(pool).get_run(child_id)
    assert child["status"] == "paused" and child["current_window"] == 0
    async with pool.connection() as conn:
        assert (
            await row(conn, "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s", (child_id,))
        )["n"] == 1
        assert (await row(conn, "SELECT count(*) AS n FROM model_campaigns"))["n"] == 1
    saved = json.loads(
        (root / ".local/scenario-runs" / str(child_id) / "credentials.json").read_text()
    )
    assert saved["configuration"]["budget"] == manifest["configuration"]["budget"]
    assert all(p["token"] not in first.text for p in saved["participants"])
    assert (
        root / ".local/scenario-runs" / str(child_id) / "credentials.json"
    ).stat().st_mode & 0o777 == 0o600
    await Director(pool).control(child_id, "resume")
    await Director(pool).fire_beats(child_id)
    assert len([e for e in await Director(pool).trace(child_id) if e["kind"] == "actor_cue"]) == 2
    changed = await client.post(
        path(manifest), json={**body, "prompt": "Change", "owner_id": "owner-1"}
    )
    assert changed.status_code == 409
    assert (await Director(pool).get_run(run_id))["status"] == "pending"


async def test_legacy_turn_auth_validation_and_unknown_configuration(client, pool, root):
    manifest = await create_run(pool, root)
    body = {"request_id": str(uuid4()), "window": 1}
    assert (await client.post(path(manifest), json=body)).status_code == 409
    assert (await client.post(path(manifest), json={**body, "window": 99})).status_code == 422
    assert (
        await client.post(
            path(manifest), json={**body, "window": 0, "prompt": "Change", "owner_id": "unknown"}
        )
    ).status_code == 422
    headers = {"Authorization": "Bearer " + manifest["participants"][0]["token"]}
    assert (await client.get(path(manifest, "boundaries"), headers=headers)).status_code == 403
    assert (await client.post(path(manifest), json=body, headers=headers)).status_code == 403
    listing = (await client.get(path(manifest, "boundaries"))).json()
    assert listing["items"][0]["source"] == "initial"
    credentials = root / ".local/scenario-runs" / manifest["run_id"] / "credentials.json"
    credentials.unlink()
    assert (await client.post(path(manifest), json={**body, "window": 0})).status_code == 409


async def make_agents(stack, app, runtime, manifest, responses):
    agents, models = {}, {}
    for participant in manifest["participants"]:
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test/api/v1/",
                headers={"Authorization": "Bearer " + participant["token"]},
            )
        )
        model = models[participant["owner_id"]] = ScriptedModel(responses=list(responses))
        agents[participant["owner_id"]] = runtime.agent(
            model=model,
            board=BoardTools(client, manifest["cluster_id"]),
            environment=EnvironmentTools(client, manifest["run_id"]),
            agent_id=participant["agent_id"],
            session_id=participant["session_id"],
            instructions=manifest["configuration"]["instructions"],
        )
    return agents, models


async def test_branch_restores_graph_files_board_mail_and_private_state(
    client, app, pool, root, test_database_url
):
    manifest = await create_run(pool, root)
    parent = UUID(manifest["run_id"])
    async with AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url))
        agents, models = await make_agents(
            stack,
            app,
            runtime,
            manifest,
            [
                call(
                    "write_file",
                    {"file_path": "/memory.txt", "content": "Remember the cedar tree."},
                ),
                call("post_thread", {"title": "Cedar", "body": "Past board note."}, "board-1"),
                AIMessage(content="Past update completed."),
                AIMessage(content="Future private marker must not leak."),
            ],
        )
        engine = RunEngine(
            pool,
            actors={p["owner_id"]: ScriptedActor() for p in manifest["participants"]},
            agents=agents,
        )
        await Director(pool).control(parent, "start")
        result = await engine.advance(parent)
        assert result["current_window"] == 1, result
        await InboxService(pool).send(
            parent, "owner-1", "owner-2", "Past mail", "Before the branch.", uuid4(), source="agent"
        )
        await ResourceService(pool).create_document(
            parent, "owner-1", uuid4(), "Past private document", "Before the branch."
        )
        await capture_boundary(pool, parent, agents)
        boundary = (await client.get(path(manifest, "boundaries"))).json()["items"][1]
        assert boundary["available"], boundary
        # Source progresses. Branching must still use the earlier immutable boundary.
        continued = await engine.advance(parent)
        assert continued["current_window"] == 2, continued["result"]
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE owner_resources SET content='Future resource marker' WHERE run_id=%s",
                (parent,),
            )
        body = {
            "request_id": str(uuid4()),
            "window": 1,
            "owner_id": "owner-1",
            "target": "agent",
            "prompt": "Keep the cedar memory. Tell me one calm next step.",
        }
        response = await client.post(path(manifest), json=body)
        assert response.status_code == 200, response.text
        child_id = UUID(response.json()["run_id"])
        child = json.loads(
            (root / ".local/scenario-runs" / str(child_id) / "credentials.json").read_text()
        )
        async with pool.connection() as conn:
            resources = await (
                await conn.execute(
                    "SELECT content FROM owner_resources WHERE run_id=%s", (child_id,)
                )
            ).fetchall()
            assert all("Future" not in r["content"] for r in resources)
            assert (
                await row(
                    conn,
                    "SELECT count(*) AS n FROM messages WHERE cluster_id=%s",
                    (child["cluster_id"],),
                )
            )["n"] == 2
            assert (
                await row(
                    conn, "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s", (child_id,)
                )
            )["n"] == 1
            owner = await row(
                conn,
                "SELECT * FROM owner_messages WHERE run_id=%s AND source='owner' "
                "AND request_id<>%s LIMIT 1",
                (child_id, body["request_id"]),
            )
            # Actor job IDs are also owner-message request IDs. Their formula stays valid.
            repeated = await save_message(
                conn,
                child_id,
                owner["owner_id"],
                "owner",
                owner["content"],
                owner["request_id"],
                owner["window"],
            )
            assert repeated["id"] == owner["id"]
        child_agents, child_models = await make_agents(
            stack, app, runtime, child, [AIMessage(content="A calm child reply.")]
        )
        for agent in child_agents.values():
            state = await agent.graph.aget_state(agent.config)
            assert not state.next
            assert "cedar" in str(state.values["files"])
            assert "Past update completed" in str(state.values["messages"])
            assert "Future private marker" not in str(state.values)
        await Director(pool).control(child_id, "resume")
        child_engine = RunEngine(
            pool,
            actors={p["owner_id"]: ScriptedActor() for p in child["participants"]},
            agents=child_agents,
        )
        result = await child_engine.advance(child_id)
        async with pool.connection() as conn:
            failures = await (
                await conn.execute(
                    "SELECT owner_id,kind,status,error FROM run_jobs WHERE run_id=%s", (child_id,)
                )
            ).fetchall()
        assert result["current_window"] == 2, (result["status"], result["result"], failures)
        assert all(model.calls == 1 for model in child_models.values())
        assert "calm next step" in str(child_models["owner-1"].seen)
        assert "Future private marker" not in str(child_models["owner-1"].seen)
        assert (await Director(pool).get_run(parent))["current_window"] == 2


def test_identity_mapping_resets_scoped_cursors_and_preserves_literal_text():
    original, child = str(uuid4()), str(uuid4())
    value = {"id": original, "next_cursor": "opaque-parent-cursor", "body": "x"}
    message = ToolMessage(
        name="list_threads",
        tool_call_id="read-page",
        content=json.dumps({"ok": True, "data": value}),
    )
    result = remap(message, {original: child})
    parsed = json.loads(result.content)["data"]
    assert parsed["id"] == child
    assert parsed["next_cursor"] is None and parsed["branch_pagination_reset_required"]
    assert remap("Keep words exactly.", {}) == "Keep words exactly."


async def test_branch_rejects_snapshot_with_changed_integrity_hash(client, pool, root):
    manifest = await create_run(pool, root)
    run_id = UUID(manifest["run_id"])
    await capture_boundary(pool, run_id)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE run_boundaries SET snapshot_hash='changed' WHERE run_id=%s", (run_id,)
        )
    response = await client.post(path(manifest), json={"request_id": str(uuid4()), "window": 0})
    assert response.status_code == 409


async def test_boundary_rejects_pending_agent_task_without_changing_source(pool, root):
    from agentrpg.director import DirectorError
    from agentrpg.run_branches import session_key

    manifest = await create_run(pool, root)
    run_id = UUID(manifest["run_id"])
    await Director(pool).control(run_id, "start")
    participant = manifest["participants"][-1]
    key = session_key(manifest["cluster_id"], participant)
    task_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_sessions(session_key,cluster_id,agent_id,session_id,"
            "instructions_hash) VALUES(%s,%s,%s,%s,'saved-instructions')",
            (key, manifest["cluster_id"], participant["agent_id"], participant["session_id"]),
        )
        await conn.execute(
            "INSERT INTO agent_tasks(session_key,task_id,prompt_hash,model_calls) "
            "VALUES(%s,%s,'saved-prompt',3)",
            (key, task_id),
        )
        before_task = await row(
            conn, "SELECT * FROM agent_tasks WHERE session_key=%s AND task_id=%s", (key, task_id)
        )
        assert before_task["status"] == "pending"
    before_run = await Director(pool).get_run(run_id)
    before_trace = await Director(pool).trace(run_id)
    with pytest.raises(DirectorError, match="assistant task is unfinished"):
        await capture_boundary(pool, run_id)
    async with pool.connection() as conn:
        assert (
            await row(conn, "SELECT run_id FROM run_boundaries WHERE run_id=%s", (run_id,)) is None
        )
        assert (
            await row(
                conn,
                "SELECT * FROM agent_tasks WHERE session_key=%s AND task_id=%s",
                (key, task_id),
            )
            == before_task
        )
    assert await Director(pool).get_run(run_id) == before_run
    assert await Director(pool).trace(run_id) == before_trace


@pytest.mark.parametrize("count", [2, 15])
async def test_all_participants_clone_as_separate_identities(client, pool, root, count):
    manifest = await create_run(pool, root, count)
    response = await client.post(
        path(manifest),
        json={
            "request_id": str(uuid4()),
            "window": 0,
            "target": "actor",
            "owner_id": f"owner-{count}",
            "prompt": "Ask for a slower pace.",
        },
    )
    assert response.status_code == 200, response.text
    child_id = UUID(response.json()["run_id"])
    members = await Director(pool).participants(child_id)
    assert len(members) == count
    assert {str(p["agent_id"]) for p in members}.isdisjoint(
        p["agent_id"] for p in manifest["participants"]
    )
    events = await Director(pool).trace(child_id)
    cues = [e for e in events if e["kind"] == "actor_cue"]
    assert len(cues) == 1 and cues[0]["owner_id"] == f"owner-{count}"
    assert all(p["token"] not in response.text for p in manifest["participants"])
