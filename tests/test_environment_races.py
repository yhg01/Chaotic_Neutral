"""PostgreSQL race and recovery checks across the environment boundaries."""

import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agentrpg.actor import ActorContractError, ActorOutput, ScriptedActor, build_actor_view
from agentrpg.director import Director, DirectorError, fingerprint, row
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.world import WorldService


def headers(participant):
    return {"Authorization": f"Bearer {participant['token']}"}


def post_body(title="Transport"):
    return {"request_id": str(uuid4()), "title": title, "body": "Please discuss transport."}


@pytest.fixture
async def environment(pool):
    director = Director(pool)
    spec = vehicle_scenario()
    created = await director.create_run(spec)
    await director.control(UUID(created["run_id"]), "start")
    return director, spec, created


async def test_action_http_retry_keeps_original_window_and_checks_changed_payload(
    client, pool, environment
):
    _, spec, created = environment
    run_id = UUID(created["run_id"])
    participant = created["participants"][0]
    owner = participant["owner_id"]
    world = WorldService(pool)
    await world.set_consent(run_id, owner, 20, ["taxi-1"], uuid4())
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET phase='agents' WHERE id=%s", (run_id,))
    path = f"/api/v1/runs/{run_id}/actions"
    payload = {
        "request_id": str(uuid4()),
        "kind": "reserve",
        "asset_id": "taxi-1",
        "expected_version": 0,
    }
    pending = await client.post(path, json=payload, headers=headers(participant))
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"
    completed = (await world.settle(run_id, 0, spec.seed))[0]
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=1 WHERE id=%s", (run_id,))
    retry = await client.post(path, json=payload, headers=headers(participant))
    assert retry.status_code == 200, retry.text
    assert retry.json() == completed
    for changed in ({"kind": "cancel"}, {"asset_id": "taxi-2"}, {"expected_version": 1}):
        response = await client.post(
            path, json={**payload, **changed}, headers=headers(participant)
        )
        assert response.status_code == 409, response.text
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET status='completed' WHERE id=%s", (run_id,))
    assert (await client.post(path, json=payload, headers=headers(participant))).json() == completed
    assert (
        await client.post(
            path, json={**payload, "request_id": str(uuid4())}, headers=headers(participant)
        )
    ).status_code == 409


@pytest.mark.parametrize("status", ["paused", "completed", "incomplete", "invalid"])
async def test_stopped_run_rejects_new_board_writes_and_keeps_identical_retries(
    client, pool, environment, status
):
    _, _, created = environment
    one, two = created["participants"]
    run_id = UUID(created["run_id"])
    path = f"/api/v1/clusters/{created['cluster_id']}/threads"
    original_payload = post_body()
    original = await client.post(path, json=original_payload, headers=headers(one))
    assert original.status_code == 201, original.text
    reply_path = path + f"/{original.json()['id']}/replies"
    reply_payload = {"request_id": str(uuid4()), "body": "I received the proposal."}
    reply = await client.post(reply_path, json=reply_payload, headers=headers(two))
    assert reply.status_code == 201, reply.text
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET status=%s WHERE id=%s", (status, run_id))
    repeated = await client.post(path, json=original_payload, headers=headers(one))
    assert repeated.status_code == 200 and repeated.json() == original.json()
    repeated_reply = await client.post(reply_path, json=reply_payload, headers=headers(two))
    assert repeated_reply.status_code == 200 and repeated_reply.json() == reply.json()
    assert (await client.post(path, json=post_body(), headers=headers(one))).status_code == 409
    assert (
        await client.post(
            reply_path, json={**reply_payload, "request_id": str(uuid4())}, headers=headers(two)
        )
    ).status_code == 409
    changed = await client.post(
        path, json={**original_payload, "body": "Changed request"}, headers=headers(one)
    )
    assert changed.status_code == 409
    assert (await client.get(path, headers=headers(one))).status_code == 200
    async with pool.connection() as conn:
        count = await row(
            conn,
            "SELECT count(*) AS n FROM run_events WHERE run_id=%s AND kind='board_posted'",
            (run_id,),
        )
    assert count["n"] == 2


async def test_run_board_posts_remain_concurrent_and_pause_waits_for_late_commit(
    client, pool, environment
):
    _, _, created = environment
    one, two = created["participants"]
    run_id = UUID(created["run_id"])
    path = f"/api/v1/clusters/{created['cluster_id']}/threads"
    delayed_payload = post_body("Delayed post")
    lock_key = uuid4().int % (2**31 - 1)
    # Generated UUID and integer literals are safe; the function is confined
    # to the disposable test schema and does not add application test hooks.
    async with pool.connection() as conn:
        await conn.execute(f"""
            CREATE FUNCTION delay_environment_post() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.request_id = '{delayed_payload["request_id"]}'::uuid THEN
                    PERFORM pg_advisory_xact_lock({lock_key});
                END IF;
                RETURN NEW;
            END $$
        """)
        await conn.execute(
            "CREATE TRIGGER delay_environment_post BEFORE INSERT ON messages "
            "FOR EACH ROW EXECUTE FUNCTION delay_environment_post()"
        )
    pause = None
    async with pool.connection() as gate:
        await gate.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        delayed = asyncio.create_task(client.post(path, json=delayed_payload, headers=headers(one)))
        try:

            async def wait_for_insert():
                while True:
                    blocked = await row(
                        gate,
                        "SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                        "AND objid=%s AND NOT granted",
                        (lock_key,),
                    )
                    if blocked:
                        return
                    assert not delayed.done(), "The delayed post never reached its insert."
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_insert(), 5)
            independent = await asyncio.wait_for(
                client.post(path, json=post_body("Independent post"), headers=headers(two)), 5
            )
            assert independent.status_code == 201, independent.text
            visible = await asyncio.wait_for(client.get(path, headers=headers(two)), 5)
            assert [item["id"] for item in visible.json()["items"]] == [independent.json()["id"]]
            assert not delayed.done()
            pause = asyncio.create_task(client.post(f"/api/v1/runs/{run_id}/control/pause"))
            await asyncio.sleep(0.05)
            assert not pause.done(), "Pause crossed a board write that had not committed."
        finally:
            await gate.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            completed = await asyncio.wait_for(delayed, 5)
            if pause is not None:
                paused = await asyncio.wait_for(pause, 5)
                assert paused.status_code == 200, paused.text
        assert completed.status_code == 201, completed.text
    visible = await client.get(path, headers=headers(one))
    assert len(visible.json()["items"]) == 2
    assert (await client.post(path, json=post_body(), headers=headers(one))).status_code == 409


async def saved_actor_job(pool, environment):
    _, spec, created = environment
    run_id = UUID(created["run_id"])
    owner = created["participants"][0]["owner_id"]
    view = build_actor_view(
        spec,
        owner,
        "Respond to the assistant's request.",
        source_event_id="approval-request",
        conversation=[{"role": "assistant", "text": "May I reserve taxi-1 for 20 credits?"}],
    )
    output = ActorOutput(
        kind="reply",
        text="I authorize 20 credits for taxi-1.",
        authorized_amount=20,
        allowed_assets=("taxi-1",),
        source_event_id=view.source_event_id,
    ).model_dump(mode="json")
    job_id, token = uuid4(), uuid4()
    async with pool.connection() as conn:
        job = await row(
            conn,
            """INSERT INTO run_jobs(id,run_id,owner_id,"window",kind,input,input_hash,output,
                       status,lease_token,lease_until)
               VALUES (%s,%s,%s,0,'actor',%s,%s,%s,'saved',%s,
                       clock_timestamp()+interval '10 minutes') RETURNING *""",
            (
                job_id,
                run_id,
                owner,
                Jsonb(view.model_dump(mode="json")),
                fingerprint(view.model_dump(mode="json")),
                Jsonb(output),
                token,
            ),
        )
    engine = RunEngine(pool, actors={owner: ScriptedActor()}, agents={owner: object()})
    return engine, job, output, token


@pytest.mark.parametrize("fence", ["paused", "completed", "stale_lease", "advanced", "phase"])
async def test_actor_output_cannot_grant_consent_past_its_fence(pool, environment, fence):
    engine, job, output, token = await saved_actor_job(pool, environment)
    _, spec, _ = environment
    async with pool.connection() as conn:
        if fence in {"paused", "completed"}:
            await conn.execute(
                "UPDATE environment_runs SET status=%s WHERE id=%s", (fence, job["run_id"])
            )
        elif fence == "stale_lease":
            await conn.execute(
                "UPDATE run_jobs SET lease_token=%s WHERE id=%s", (uuid4(), job["id"])
            )
        elif fence == "advanced":
            await conn.execute(
                "UPDATE environment_runs SET current_window=1 WHERE id=%s", (job["run_id"],)
            )
        else:
            await conn.execute(
                "UPDATE environment_runs SET phase='agents' WHERE id=%s", (job["run_id"],)
            )
    with pytest.raises(DirectorError, match="lease changed|paused or advanced"):
        await engine._apply(job, output, spec, token)
    account = (await engine.world.view(job["run_id"], job["owner_id"]))["account"]
    assert account["consent_version"] == 0 and account["consent_assets"] == []
    assert await engine.director.messages(job["run_id"], job["owner_id"]) == []
    trace = await engine.director.trace(job["run_id"])
    assert not any(event["kind"] in {"consent_changed", "actor_turn"} for event in trace)


async def test_actor_consent_and_message_commit_together_or_roll_back(pool, environment):
    engine, job, output, token = await saved_actor_job(pool, environment)
    _, spec, _ = environment
    async with pool.connection() as conn:
        await conn.execute("""
            CREATE FUNCTION fail_owner_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'simulated owner delivery failure';
            END $$
        """)
        await conn.execute(
            "CREATE TRIGGER fail_owner_delivery BEFORE INSERT ON owner_messages "
            "FOR EACH ROW EXECUTE FUNCTION fail_owner_delivery()"
        )
    with pytest.raises(psycopg.Error):
        await engine._apply(job, output, spec, token)
    assert (await engine.world.view(job["run_id"], job["owner_id"]))["account"][
        "consent_version"
    ] == 0
    async with pool.connection() as conn:
        await conn.execute("DROP TRIGGER fail_owner_delivery ON owner_messages")
    await engine._apply(job, output, spec, token)
    await engine._apply(job, output, spec, token)
    assert (await engine.world.view(job["run_id"], job["owner_id"]))["account"][
        "consent_version"
    ] == 1
    assert len(await engine.director.messages(job["run_id"], job["owner_id"])) == 1


async def test_actor_can_withdraw_all_previously_granted_authority(pool, environment):
    engine, job, _, token = await saved_actor_job(pool, environment)
    _, spec, _ = environment
    await engine.world.set_consent(
        job["run_id"],
        job["owner_id"],
        20,
        ["taxi-1"],
        uuid4(),
        allowed_kinds=["reserve", "cancel"],
    )
    output = ActorOutput(
        kind="reply",
        text="I withdraw my previous transport permission.",
        source_event_id=job["input"]["source_event_id"],
        authorized_amount=0,
        allowed_assets=(),
        allowed_kinds=(),
    ).model_dump(mode="json")
    async with pool.connection() as conn:
        await conn.execute("UPDATE run_jobs SET output=%s WHERE id=%s", (Jsonb(output), job["id"]))
    await engine._apply(job, output, spec, token)
    account = (await engine.world.view(job["run_id"], job["owner_id"]))["account"]
    assert account["consent_budget"] == 0
    assert account["consent_assets"] == account["consent_kinds"] == []
    assert account["consent_version"] == 2


@pytest.mark.parametrize("fence", ["stale_lease", "completed", "paused"])
async def test_late_actor_contract_error_cannot_invalidate_fenced_run(pool, environment, fence):
    engine, job, _, _ = await saved_actor_job(pool, environment)
    _, spec, _ = environment
    entered, release = asyncio.Event(), asyncio.Event()

    class InvalidActor:
        async def respond(self, view):
            entered.set()
            await release.wait()
            raise ActorContractError("The response violated the role contract.")

    engine.actors[job["owner_id"]] = InvalidActor()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE run_jobs SET output=NULL,status='pending',lease_token=NULL,lease_until=NULL "
            "WHERE id=%s",
            (job["id"],),
        )
    worker = asyncio.create_task(engine._execute(job, spec))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with pool.connection() as conn:
            if fence == "stale_lease":
                await conn.execute(
                    "UPDATE run_jobs SET lease_token=%s WHERE id=%s", (uuid4(), job["id"])
                )
            else:
                await conn.execute(
                    "UPDATE environment_runs SET status=%s WHERE id=%s", (fence, job["run_id"])
                )
    finally:
        release.set()
        await asyncio.wait_for(worker, 5)
    run = await engine.director.get_run(job["run_id"])
    assert run["status"] == ("running" if fence == "stale_lease" else fence)
    trace = await engine.director.trace(job["run_id"])
    assert not any(event["kind"] == "simulator_error" for event in trace)
