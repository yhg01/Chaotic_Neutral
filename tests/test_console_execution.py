"""Real PostgreSQL controls with deterministic workers and no provider calls."""

import asyncio
import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from agentrpg import console_execution as console
from agentrpg.director import Director, fingerprint, row, rows
from agentrpg.scenario_schema import vehicle_scenario


class RecordingSupervisor:
    def __init__(self):
        self.launches = []

    async def launch(self, operation_id, run_id):
        self.launches.append((operation_id, run_id))

    async def close(self):
        pass


@pytest.fixture
def controls(app, tmp_path):
    if "/api/v1/console/runs/{run_id}/execution" not in app.openapi()["paths"]:
        app.include_router(console.router)
    app.state.console_root = tmp_path
    app.state.console_supervisor = RecordingSupervisor()
    return app.state.console_supervisor


async def create_run(pool, tmp_path, count=2, spec=None):
    manifest = await Director(pool).create_run(spec or vehicle_scenario(count))
    campaign_id = uuid4()
    manifest["configuration"] = {
        "actors": "scripted",
        "model": "openai/gpt-4.1",
        "actor_model": "openai/gpt-4.1",
        "max_model_calls": 18,
        "timeout_seconds": 240,
        "instructions": "Help the assigned owner.",
        "base_url": "http://127.0.0.1:8000",
        "budget": {"campaign_id": str(campaign_id), "limit_usd": "30"},
    }
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO model_campaigns(id,name,limit_usd) VALUES(%s,'Test',30)", (campaign_id,)
        )
    console.save_private(
        tmp_path / ".local/scenario-runs" / str(manifest["run_id"]) / "credentials.json", manifest
    )
    return json.loads(json.dumps(manifest, default=str))


def endpoint(manifest, action="execution"):
    return f"/api/v1/console/runs/{manifest['run_id']}/{action}"


async def test_admin_only_and_no_credential_leak(client, pool, tmp_path, controls):
    manifest = await create_run(pool, tmp_path)
    response = await client.get(endpoint(manifest))
    assert response.status_code == 200
    data = response.json()
    assert data["capabilities"]["can_execute"]
    assert data["next_intervention_window"] == 0
    assert Decimal(data["budget"]["available_usd"]) == 30
    assert "token" not in response.text and "instructions" not in response.text
    for method, action, body in (
        ("GET", "execution", None),
        ("POST", "execution", {"request_id": str(uuid4()), "mode": "step"}),
        ("POST", "execution/pause", {"request_id": str(uuid4())}),
        (
            "POST",
            "interventions",
            {
                "request_id": str(uuid4()),
                "owner_id": "owner-1",
                "target": "actor",
                "prompt": "Wait.",
                "expected_window": 0,
            },
        ),
    ):
        result = await client.request(
            method,
            endpoint(manifest, action),
            json=body,
            headers={"Authorization": f"Bearer {manifest['participants'][0]['token']}"},
        )
        assert result.status_code == 403
    assert not controls.launches


async def test_start_is_idempotent_and_concurrent_start_is_unique(client, pool, tmp_path, controls):
    manifest = await create_run(pool, tmp_path)
    body = {"request_id": str(uuid4()), "mode": "step"}
    first, repeated = await asyncio.gather(
        *(client.post(endpoint(manifest), json=body) for _ in range(2))
    )
    assert first.status_code == repeated.status_code == 202
    assert first.json()["execution"]["id"] == repeated.json()["execution"]["id"]
    assert len(controls.launches) == 1
    changed = await client.post(endpoint(manifest), json={**body, "mode": "continue"})
    assert changed.status_code == 409
    duplicate = await client.post(endpoint(manifest), json={**body, "request_id": str(uuid4())})
    assert duplicate.status_code == 409


@pytest.mark.parametrize(
    "change", ["missing", "campaign", "identity", "origin", "model", "symlink"]
)
async def test_invalid_local_manifest_blocks_launch(client, pool, tmp_path, controls, change):
    manifest = await create_run(pool, tmp_path)
    path = tmp_path / ".local/scenario-runs" / manifest["run_id"] / "credentials.json"
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        alternate = tmp_path / "outside.json"
        path.rename(alternate)
        path.symlink_to(alternate)
    else:
        if change == "campaign":
            manifest["configuration"]["budget"]["campaign_id"] = str(uuid4())
        elif change == "identity":
            manifest["participants"][0]["token"] = "invalid-secret"
        elif change == "origin":
            manifest["configuration"]["base_url"] = "http://secret@external.example"
        else:
            manifest["configuration"]["model"] = "unbounded/model"
        console.save_private(path, manifest)
    state = await client.get(endpoint(manifest))
    assert not state.json()["capabilities"]["can_execute"]
    result = await client.post(
        endpoint(manifest), json={"request_id": str(uuid4()), "mode": "continue"}
    )
    assert result.status_code == 409
    assert "invalid-secret" not in result.text and "secret@" not in result.text
    assert not controls.launches


async def test_pause_cancels_queued_start_and_repeat_does_not_pause_later_work(
    client, pool, tmp_path, controls
):
    manifest = await create_run(pool, tmp_path)
    await client.post(endpoint(manifest), json={"request_id": str(uuid4()), "mode": "step"})
    body = {"request_id": str(uuid4())}
    paused = await client.post(endpoint(manifest, "execution/pause"), json=body)
    assert paused.json()["status"] == "paused"
    assert paused.json()["execution"]["status"] == "paused"
    assert paused.json()["execution"]["pause_requested"]
    await Director(pool).control(UUID(manifest["run_id"]), "resume")
    repeated = await client.post(endpoint(manifest, "execution/pause"), json=body)
    assert repeated.status_code == 200 and repeated.json()["status"] == "running"


async def test_interventions_private_durable_and_no_consent_change(
    client, pool, tmp_path, controls
):
    manifest = await create_run(pool, tmp_path)
    run_id = UUID(manifest["run_id"])
    bodies = [
        {
            "request_id": str(uuid4()),
            "owner_id": "owner-1",
            "target": "actor",
            "prompt": "Express doubt about the plan.",
            "expected_window": 0,
        },
        {
            "request_id": str(uuid4()),
            "owner_id": "owner-2",
            "target": "agent",
            "prompt": "Please explain the other options.",
            "expected_window": 0,
        },
    ]
    responses = [
        await client.post(endpoint(manifest, "interventions"), json=body) for body in bodies
    ]
    assert all(response.status_code == 201 for response in responses)
    assert all(response.json()["effective_window"] == 0 for response in responses)
    async with pool.connection() as conn, conn.transaction():
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,))
        before = await rows(conn, "SELECT * FROM world_accounts WHERE run_id=%s", (run_id,))
        await console.apply_interventions(conn, run, 0)
        await console.apply_interventions(conn, run, 0)
        after = await rows(conn, "SELECT * FROM world_accounts WHERE run_id=%s", (run_id,))
        assert before == after
        cues = await rows(
            conn, "SELECT * FROM run_events WHERE run_id=%s AND kind='actor_cue'", (run_id,)
        )
        messages = await rows(conn, "SELECT * FROM owner_messages WHERE run_id=%s", (run_id,))
        assert len(cues) == len(messages) == 1
        assert cues[0]["owner_id"] == "owner-1" and cues[0]["payload"]["source"] == "console"
        assert messages[0]["owner_id"] == "owner-2" and messages[0]["source"] == "owner"
    repeated = await client.post(endpoint(manifest, "interventions"), json=bodies[0])
    assert repeated.status_code == 200 and repeated.json()["status"] == "applied"
    changed = await client.post(
        endpoint(manifest, "interventions"), json={**bodies[0], "prompt": "Different."}
    )
    assert changed.status_code == 409


async def test_prompt_does_not_change_prepared_job_and_stale_turn_rejected(
    client, pool, tmp_path, controls
):
    manifest = await create_run(pool, tmp_path)
    run_id = UUID(manifest["run_id"])
    async with pool.connection() as conn:
        await conn.execute(
            'INSERT INTO run_jobs(id,run_id,owner_id,"window",kind,input,input_hash) '
            "VALUES(%s,%s,%s,0,%s,%s,%s)",
            (uuid4(), run_id, "owner-1", "actor", Jsonb({"saved": "original"}), "hash"),
        )
    body = {
        "request_id": str(uuid4()),
        "owner_id": "owner-1",
        "target": "actor",
        "prompt": "New direction.",
        "expected_window": 0,
    }
    response = await client.post(endpoint(manifest, "interventions"), json=body)
    assert response.json()["effective_window"] == 1
    async with pool.connection() as conn:
        job = await row(conn, "SELECT input FROM run_jobs WHERE run_id=%s", (run_id,))
        assert job["input"] == {"saved": "original"}
        await conn.execute("UPDATE environment_runs SET current_window=1 WHERE id=%s", (run_id,))
    stale = await client.post(
        endpoint(manifest, "interventions"), json={**body, "request_id": str(uuid4())}
    )
    assert stale.status_code == 409


async def test_saved_boundary_closes_current_turn_for_new_prompts(client, pool, tmp_path, controls):
    manifest = await create_run(pool, tmp_path)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_boundaries(run_id,window_index,snapshot,snapshot_hash) "
            "VALUES(%s,0,'{}','test')",
            (UUID(manifest["run_id"]),),
        )
    result = await client.post(
        endpoint(manifest, "interventions"),
        json={
            "request_id": str(uuid4()),
            "owner_id": "owner-1",
            "target": "actor",
            "prompt": "Add this at the next unopened turn.",
            "expected_window": 0,
        },
    )
    assert result.status_code == 201 and result.json()["effective_window"] == 1


async def test_completed_run_requires_branch(client, pool, tmp_path, controls):
    manifest = await create_run(pool, tmp_path)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET status='completed' WHERE id=%s",
            (UUID(manifest["run_id"]),),
        )
    state = (await client.get(endpoint(manifest))).json()
    assert not state["capabilities"]["can_execute"]
    assert not state["capabilities"]["can_intervene"]
    response = await client.post(
        endpoint(manifest), json={"request_id": str(uuid4()), "mode": "step"}
    )
    assert response.status_code == 409 and not controls.launches


async def test_direct_cli_worker_cannot_be_started_twice(client, pool, tmp_path, controls):
    import hashlib

    manifest = await create_run(pool, tmp_path)
    key = int.from_bytes(
        hashlib.sha256(f"scenario-runner:{manifest['run_id']}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    async with pool.connection() as lease:
        await lease.execute("SELECT pg_advisory_lock(%s)", (key,))
        await lease.commit()
        state = (await client.get(endpoint(manifest))).json()
        assert not state["capabilities"]["can_execute"]
        response = await client.post(
            endpoint(manifest), json={"request_id": str(uuid4()), "mode": "step"}
        )
        assert response.status_code == 409 and not controls.launches
        await lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
        await lease.commit()


async def test_budget_exhaustion_counts_uncertain_reservations(client, pool, tmp_path, controls):
    manifest = await create_run(pool, tmp_path)
    campaign_id = UUID(manifest["configuration"]["budget"]["campaign_id"])
    async with pool.connection() as conn:
        for status, reserved, reported in (
            ("reported", 15, 10),
            ("uncertain", 12, None),
            ("reserved", 8, None),
        ):
            await conn.execute(
                """INSERT INTO model_spend_attempts
                (id,campaign_id,run_id,role,model,reserved_usd,reported_usd,status,
                 prompt_token_bound,output_token_bound,price_snapshot)
                VALUES(%s,%s,%s,'assistant','openai/gpt-4.1',%s,%s,%s,1,1,'{}')""",
                (uuid4(), campaign_id, UUID(manifest["run_id"]), reserved, reported, status),
            )
    state = (await client.get(endpoint(manifest))).json()
    assert Decimal(state["budget"]["spent_usd"]) == 10
    assert Decimal(state["budget"]["reserved_usd"]) == 20
    assert Decimal(state["budget"]["available_usd"]) == 0
    response = await client.post(
        endpoint(manifest), json={"request_id": str(uuid4()), "mode": "step"}
    )
    assert response.status_code == 409 and not controls.launches


async def test_paused_queued_worker_does_not_execute(
    client, pool, tmp_path, controls, test_database_url, monkeypatch
):
    manifest = await create_run(pool, tmp_path)
    response = await client.post(
        endpoint(manifest), json={"request_id": str(uuid4()), "mode": "step"}
    )
    operation_id = UUID(response.json()["execution"]["id"])
    await client.post(endpoint(manifest, "execution/pause"), json={"request_id": str(uuid4())})
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    async def forbidden(*args, **kwargs):
        pytest.fail("A cancelled queued worker must not execute.")

    await console.worker(operation_id, root=tmp_path, execute=forbidden)
    async with pool.connection() as conn:
        operation = await row(
            conn, "SELECT status FROM console_executions WHERE id=%s", (operation_id,)
        )
    assert operation["status"] == "paused"


async def test_supervisor_fixed_command_private_log_and_crash_status(
    pool, tmp_path, settings, controls, monkeypatch
):
    import stat
    import sys

    manifest = await create_run(pool, tmp_path)
    operation_id, run_id = uuid4(), UUID(manifest["run_id"])
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash) "
            "VALUES(%s,%s,%s,'step','hash')",
            (operation_id, run_id, uuid4()),
        )
    done = asyncio.Event()

    class FakeProcess:
        pid = 12345

        async def wait(self):
            await done.wait()
            return 1

    async def spawn(*args, **kwargs):
        assert args == (
            sys.executable,
            "-m",
            "agentrpg.console_execution",
            "--worker",
            str(operation_id),
        )
        assert "shell" not in kwargs and kwargs["cwd"] == tmp_path
        return FakeProcess()

    monkeypatch.setattr(console.asyncio, "create_subprocess_exec", spawn)
    supervisor = console.ExecutionSupervisor(pool, tmp_path, settings)
    await supervisor.launch(operation_id, run_id)
    path = tmp_path / ".local/scenario-runs" / str(run_id) / f"console-{operation_id}.log"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    done.set()
    await asyncio.gather(*list(supervisor.tasks))
    async with pool.connection() as conn:
        operation = await row(
            conn, "SELECT status FROM console_executions WHERE id=%s", (operation_id,)
        )
    assert operation["status"] == "interrupted"


async def test_fifteen_owners_can_queue_independent_prompts(client, pool, tmp_path, controls):
    manifest = await create_run(pool, tmp_path, 15)
    responses = await asyncio.gather(
        *(
            client.post(
                endpoint(manifest, "interventions"),
                json={
                    "request_id": str(uuid4()),
                    "owner_id": p["owner_id"],
                    "target": "agent",
                    "prompt": f"Private request for {p['owner_id']}",
                    "expected_window": 0,
                },
            )
            for p in manifest["participants"]
        )
    )
    assert all(response.status_code == 201 for response in responses)
    async with pool.connection() as conn, conn.transaction():
        run = await row(
            conn,
            "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE",
            (UUID(manifest["run_id"]),),
        )
        await console.apply_interventions(conn, run, 0)
        messages = await rows(
            conn, "SELECT owner_id,content FROM owner_messages WHERE run_id=%s", (run["id"],)
        )
    assert len(messages) == 15
    assert all(
        message["content"] == f"Private request for {message['owner_id']}" for message in messages
    )


async def test_stale_worker_recovered_only_after_its_lock_is_free(client, pool, tmp_path, controls):
    manifest = await create_run(pool, tmp_path)
    response = await client.post(
        endpoint(manifest), json={"request_id": str(uuid4()), "mode": "step"}
    )
    operation_id = UUID(response.json()["execution"]["id"])
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE console_executions SET status='running',"
            "heartbeat_at=clock_timestamp()-interval '90 seconds' WHERE id=%s",
            (operation_id,),
        )
    async with pool.connection() as lease:
        await lease.execute("SELECT pg_advisory_lock(%s)", (console.lock_key(manifest["run_id"]),))
        await lease.commit()
        state = await client.get(endpoint(manifest))
        assert state.json()["execution"]["status"] == "running"
        await lease.execute(
            "SELECT pg_advisory_unlock(%s)", (console.lock_key(manifest["run_id"]),)
        )
        await lease.commit()
    recovered = await client.get(endpoint(manifest))
    assert recovered.json()["execution"]["status"] == "interrupted"
    assert recovered.json()["capabilities"]["can_execute"]


async def test_worker_runs_fixed_bounded_invocation_and_keeps_campaign(
    pool, tmp_path, controls, test_database_url, monkeypatch
):
    manifest = await create_run(pool, tmp_path)
    operation_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash) "
            "VALUES(%s,%s,%s,'step',%s)",
            (operation_id, UUID(manifest["run_id"]), uuid4(), fingerprint({"mode": "step"})),
        )
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    calls = []

    async def execute(args, *, root, env):
        calls.append(args)
        assert root == tmp_path and args.windows == 1 and not args.semantic_review
        assert args.console_operation == operation_id
        saved = console.load_manifest(root, manifest["run_id"])
        assert saved["configuration"]["budget"] == manifest["configuration"]["budget"]
        return {"status": "paused"}

    await console.worker(operation_id, root=tmp_path, execute=execute)
    await console.worker(operation_id, root=tmp_path, execute=execute)
    assert len(calls) == 1
    async with pool.connection() as conn:
        saved = await row(conn, "SELECT * FROM console_executions WHERE id=%s", (operation_id,))
    assert saved["status"] == "paused" and saved["finished_at"] is not None


async def test_worker_failure_is_safe_and_does_not_relaunch(
    pool, tmp_path, controls, test_database_url, monkeypatch
):
    manifest = await create_run(pool, tmp_path)
    operation_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash) "
            "VALUES(%s,%s,%s,'continue','hash')",
            (operation_id, UUID(manifest["run_id"]), uuid4()),
        )
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    async def fail(*args, **kwargs):
        raise RuntimeError("private-provider-key")

    await console.worker(operation_id, root=tmp_path, execute=fail)
    async with pool.connection() as conn:
        operation = await row(conn, "SELECT * FROM console_executions WHERE id=%s", (operation_id,))
    assert operation["status"] == "failed"
    assert "private-provider-key" not in operation["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"target": "director"},
        {"prompt": "   "},
        {"expected_window": True},
        {"expected_window": -1},
        {"prompt": "x" * 12001},
        {"owner_id": "unknown"},
    ],
)
async def test_invalid_prompt_rejected(client, pool, tmp_path, controls, body):
    manifest = await create_run(pool, tmp_path)
    result = await client.post(
        endpoint(manifest, "interventions"),
        json={
            "request_id": str(uuid4()),
            "owner_id": "owner-1",
            "target": "actor",
            "prompt": "Please wait.",
            "expected_window": 0,
            **body,
        },
    )
    assert result.status_code in {404, 422}


async def test_prepare_applies_once_before_actor_and_agent_input(client, pool, tmp_path, controls):
    from agentrpg.run_engine import RunEngine

    manifest = await create_run(pool, tmp_path)
    run_id = UUID(manifest["run_id"])
    director = Director(pool)
    engine = RunEngine(pool, actors={}, agents={})
    for owner, target in (("owner-1", "actor"), ("owner-2", "agent")):
        response = await client.post(
            endpoint(manifest, "interventions"),
            json={
                "request_id": str(uuid4()),
                "owner_id": owner,
                "target": target,
                "prompt": f"Private {target} prompt.",
                "expected_window": 0,
            },
        )
        assert response.status_code == 201
    await director.control(run_id, "start")
    participants = await director.participants(run_id)
    spec = vehicle_scenario(2)
    await engine._prepare(run_id, spec, participants, 0, "actor")
    async with pool.connection() as conn:
        first = await rows(
            conn, "SELECT id,input,input_hash FROM run_jobs WHERE run_id=%s", (run_id,)
        )
    await engine._prepare(run_id, spec, participants, 0, "actor")
    async with pool.connection() as conn:
        again = await rows(
            conn, "SELECT id,input,input_hash FROM run_jobs WHERE run_id=%s", (run_id,)
        )
        assert again == first
        assert first[0]["input"]["cue"] == "Private actor prompt."
        assert first[0]["input"]["owner_id"] == "owner-1"
        await conn.execute("UPDATE environment_runs SET phase='agents' WHERE id=%s", (run_id,))
    await engine._prepare(run_id, spec, participants, 0, "agent")
    await engine._prepare(run_id, spec, participants, 0, "agent")
    async with pool.connection() as conn:
        messages = await rows(
            conn, "SELECT owner_id,content FROM owner_messages WHERE run_id=%s", (run_id,)
        )
        assert messages == [{"owner_id": "owner-2", "content": "Private agent prompt."}]
        agent = await row(
            conn,
            "SELECT input FROM run_jobs WHERE run_id=%s AND owner_id='owner-2' AND kind='agent'",
            (run_id,),
        )
        assert agent["input"]["owner_messages"][0]["content"] == "Private agent prompt."
        applied = await rows(
            conn,
            "SELECT id FROM run_events WHERE run_id=%s AND kind='console_intervention_applied'",
            (run_id,),
        )
        assert len(applied) == 2


async def test_pending_intervention_prevents_early_completion(client, pool, tmp_path, controls):
    from agentrpg.activation import ActivationResult
    from agentrpg.actor import ScriptedActor
    from agentrpg.run_engine import RunEngine
    from agentrpg.scenario_schema import ScenarioSpec

    data = vehicle_scenario(1).model_dump(mode="json")
    data["beats"] = [beat for beat in data["beats"] if beat["trigger"]["kind"] == "run_started"]
    spec = ScenarioSpec.model_validate(data)
    manifest = await create_run(pool, tmp_path, spec=spec)
    run_id = UUID(manifest["run_id"])
    director = Director(pool)
    views = []

    class Actor(ScriptedActor):
        async def respond(self, view):
            views.append(view)
            if view.window == 0:
                queued = await client.post(
                    endpoint(manifest, "interventions"),
                    json={
                        "request_id": str(uuid4()),
                        "owner_id": "owner-1",
                        "target": "actor",
                        "prompt": "Ask your assistant to consider one more option.",
                        "expected_window": 0,
                    },
                )
                assert queued.status_code == 201 and queued.json()["effective_window"] == 1
            return await super().respond(view)

    class FinishedAgent:
        async def run_activation(self, envelope):
            return ActivationResult(status="finished")

    engine = RunEngine(pool, actors={"owner-1": Actor()}, agents={"owner-1": FinishedAgent()})
    await director.control(run_id, "start")
    first = await engine.advance(run_id)
    assert first["status"] == "running" and first["current_window"] == 1
    async with pool.connection() as conn:
        pending = await row(
            conn, "SELECT status FROM console_interventions WHERE run_id=%s", (run_id,)
        )
        assert pending["status"] == "pending"
    await engine.advance(run_id)
    assert len(views) == 2
    assert "one more option" in views[1].cue
    async with pool.connection() as conn:
        saved = await row(
            conn, "SELECT status FROM console_interventions WHERE run_id=%s", (run_id,)
        )
        assert saved["status"] == "applied"
        count = await row(
            conn,
            "SELECT count(*) AS n FROM run_events WHERE run_id=%s "
            "AND kind='console_intervention_applied'",
            (run_id,),
        )
        assert count["n"] == 1


async def test_real_worker_subprocess_uses_scoped_database_and_stops_before_models(
    pool, tmp_path, settings, controls
):
    """A cancelled operation exits before manifest loading or any model code."""
    manifest = await create_run(pool, tmp_path)
    operation_id, run_id = uuid4(), UUID(manifest["run_id"])
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash,"
            "status,pause_requested) VALUES(%s,%s,%s,'step','hash','paused',true)",
            (operation_id, run_id, uuid4()),
        )
    supervisor = console.ExecutionSupervisor(pool, tmp_path, settings)
    await supervisor.launch(operation_id, run_id)
    process = supervisor.processes[operation_id]
    await asyncio.wait_for(asyncio.gather(*list(supervisor.tasks)), 15)
    assert process.returncode == 0
    async with pool.connection() as conn:
        operation = await row(
            conn, "SELECT status,pid FROM console_executions WHERE id=%s", (operation_id,)
        )
        charges = await row(
            conn, "SELECT count(*) AS n FROM model_spend_attempts WHERE run_id=%s", (run_id,)
        )
    assert operation["status"] == "paused" and operation["pid"] == process.pid
    assert charges["n"] == 0
    await supervisor.close()


async def test_shutdown_retains_pending_cost_reservation(
    pool, tmp_path, settings, controls, monkeypatch
):
    import sys

    manifest = await create_run(pool, tmp_path)
    operation_id, run_id = uuid4(), UUID(manifest["run_id"])
    campaign_id = UUID(manifest["configuration"]["budget"]["campaign_id"])
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash,status) "
            "VALUES(%s,%s,%s,'step','hash','running')",
            (operation_id, run_id, uuid4()),
        )
        await conn.execute(
            """INSERT INTO model_spend_attempts
            (id,campaign_id,run_id,role,model,reserved_usd,status,
             prompt_token_bound,output_token_bound,price_snapshot)
            VALUES(%s,%s,%s,'assistant','openai/gpt-4.1',4,'reserved',1,1,'{}')""",
            (uuid4(), campaign_id, run_id),
        )
    spawn = asyncio.create_subprocess_exec

    async def idle_process(*args, **kwargs):
        # An actual process waits without importing the worker or using models.
        return await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)

    monkeypatch.setattr(console.asyncio, "create_subprocess_exec", idle_process)
    supervisor = console.ExecutionSupervisor(pool, tmp_path, settings)
    await supervisor.launch(operation_id, run_id)
    process = supervisor.processes[operation_id]
    await supervisor.close()
    assert process.returncode is not None and not supervisor.processes
    async with pool.connection() as conn:
        operation = await row(
            conn, "SELECT status FROM console_executions WHERE id=%s", (operation_id,)
        )
        reservation = await row(
            conn, "SELECT status,reserved_usd FROM model_spend_attempts WHERE run_id=%s", (run_id,)
        )
    assert operation["status"] == "interrupted"
    assert reservation["status"] == "reserved" and reservation["reserved_usd"] == 4


async def test_supervisor_keeps_process_watch_when_pid_write_fails(
    pool, tmp_path, settings, controls, monkeypatch
):
    manifest = await create_run(pool, tmp_path)
    operation_id, run_id = uuid4(), UUID(manifest["run_id"])
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash) "
            "VALUES(%s,%s,%s,'step','hash')",
            (operation_id, run_id, uuid4()),
        )
    stopped = asyncio.Event()

    class Process:
        pid = 67890

        async def wait(self):
            await stopped.wait()

    class TransientPool:
        calls = 0

        def connection(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Temporary connection failure.")
            return pool.connection()

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(console.asyncio, "create_subprocess_exec", spawn)
    supervisor = console.ExecutionSupervisor(TransientPool(), tmp_path, settings)
    await supervisor.launch(operation_id, run_id)
    assert operation_id in supervisor.processes and len(supervisor.tasks) == 1
    stopped.set()
    await asyncio.gather(*list(supervisor.tasks))
    async with pool.connection() as conn:
        saved = await row(
            conn, "SELECT status FROM console_executions WHERE id=%s", (operation_id,)
        )
    assert saved["status"] == "interrupted"
