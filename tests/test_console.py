"""Turn replay reads real PostgreSQL evidence without changing a run."""

from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from agentrpg.director import Director, emit, save_message
from agentrpg.inbox import InboxService
from agentrpg.scenario_schema import vehicle_scenario


async def create_run(client):
    response = await client.post("/api/v1/runs", json=vehicle_scenario().model_dump(mode="json"))
    assert response.status_code == 201, response.text
    return response.json()


def path(run, suffix=""):
    return f"/api/v1/observer/runs/{run['run_id']}/turns{suffix}"


async def test_operational_audit_stays_out_of_story_counts_but_remains_in_raw_trace(client, pool):
    run = await create_run(client)
    run_id = UUID(run["run_id"])
    await Director(pool).control(run_id, "start")
    async with pool.connection() as conn:
        await emit(conn, run_id, "trial_started", 0, {}, "trial_started:test")
        await emit(conn, run_id, "trial_finished", 0, {}, "trial_finished:test")
    detail = (await client.get(path(run, "/0"))).json()
    assert [e["kind"] for e in detail["events"]] == ["run_started"]
    assert detail["counts"]["events"] == 1
    raw = await Director(pool).trace(run_id)
    assert {e["kind"] for e in raw} == {
        "control_audit_enabled",
        "operator_control",
        "trial_started",
        "trial_finished",
        "run_started",
    }


async def job(conn, run_id, window=0, owner="owner-1", kind="actor", view=None):
    job_id = uuid4()
    await conn.execute(
        """INSERT INTO run_jobs(id,run_id,owner_id,"window",kind,input,input_hash,
           output,output_meta,status,lease_token) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            job_id,
            run_id,
            owner,
            window,
            kind,
            Jsonb(view or {}),
            "hidden-input-hash",
            Jsonb({"text": "Saved reply", "token_hash": "hidden-output-hash"}),
            Jsonb({"nested": {"api_key": "hidden-key", "attempts": 1}}),
            "completed",
            uuid4(),
        ),
    )
    return job_id


async def test_saved_budget_failure_reason_is_visible_without_changing_the_job(client, pool):
    run = await create_run(client)
    async with pool.connection() as conn:
        job_id = await job(conn, UUID(run["run_id"]))
        await conn.execute(
            "UPDATE run_jobs SET status='failed',error=%s,failure_kind='budget' WHERE id=%s",
            ("ModelGenerationError: saved inputs retained.", job_id),
        )
    replay = (await client.get(path(run, "/0"))).json()
    saved = replay["jobs"][0]
    assert saved["failure_kind"] == "budget"
    assert saved["status"] == "failed"
    async with pool.connection() as conn:
        unchanged = await (
            await conn.execute(
                "SELECT status,attempts,failure_kind FROM run_jobs WHERE id=%s", (job_id,)
            )
        ).fetchone()
    assert unchanged == {"status": "failed", "attempts": 0, "failure_kind": "budget"}


@pytest.mark.parametrize("suffix", ["", "/0"])
async def test_turn_replay_requires_admin(client, suffix):
    run = await create_run(client)
    assert (await client.get(path(run, suffix), headers={"Authorization": ""})).status_code == 401
    assert (
        await client.get(
            path(run, suffix),
            headers={
                "Authorization": "Bearer " + run["participants"][0]["token"],
            },
        )
    ).status_code == 403
    assert (await client.post(path(run, suffix), json={})).status_code == 405


async def test_turns_empty_run_and_missing_old_actor_inputs(client, pool):
    run = await create_run(client)
    listed = (await client.get(path(run))).json()
    assert [item["window"] for item in listed["items"]] == [0]
    assert listed["items"][0]["phase"] == "actors"
    assert listed["items"][0]["counts"] == {
        "owner_chat": 0,
        "agent_chat": 0,
        "events": 0,
        "mail": 0,
        "board": 0,
    }
    detail = (await client.get(path(run, "/0"))).json()
    assert detail["perspectives"] == [] and detail["jobs"] == []
    assert detail["pagination"]["complete"] is True
    async with pool.connection() as conn:
        await job(conn, UUID(run["run_id"]))
    detail = (await client.get(path(run, "/0"))).json()
    assert len(detail["jobs"]) == 1 and detail["perspectives"] == []


@pytest.mark.parametrize(
    "suffix", ["/nope", "/-1", "/100", "/0?limit=0", "/0?limit=101", "/0?offset=-1"]
)
async def test_invalid_turn_and_pagination_arguments(client, suffix):
    run = await create_run(client)
    assert (await client.get(path(run, suffix))).status_code == 422


async def test_unknown_and_future_turns_are_not_exposed(client, pool):
    run = await create_run(client)
    run_id = UUID(run["run_id"])
    async with pool.connection() as conn:
        await emit(conn, run_id, "actor_cue", 2, {"cue": "FUTURE_SECRET"}, "future")
        await job(conn, run_id, 2, view={"cue": "FUTURE_SECRET"})
    listed = await client.get(path(run))
    assert [item["window"] for item in listed.json()["items"]] == [0]
    assert "FUTURE_SECRET" not in listed.text
    assert (await client.get(path(run, "/1"))).status_code == 404
    assert (await client.get(path(run, "/2"))).status_code == 404
    assert (await client.get(path(run, "/99"))).status_code == 422
    assert (await client.get(f"/api/v1/observer/runs/{uuid4()}/turns")).status_code == 404
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET current_window=2 WHERE id=%s", (run_id,))
    assert (await client.get(path(run, "/1"))).status_code == 404
    assert "FUTURE_SECRET" in (await client.get(path(run, "/2"))).text


async def test_replay_uses_selected_turn_and_saved_persona_resource_view(client, pool):
    run = await create_run(client)
    run_id = UUID(run["run_id"])
    async with pool.connection() as conn:
        await save_message(conn, run_id, "owner-1", "owner", "Earlier owner", uuid4(), 0)
        await save_message(conn, run_id, "owner-1", "agent", "Earlier agent", uuid4(), 0)
        await save_message(conn, run_id, "owner-1", "owner", "Later owner", uuid4(), 1)
        await emit(conn, run_id, "window_completed", 0, {}, "settled")
        view = {
            "owner_id": "owner-1",
            "owner_name": "Saved persona",
            "request": "Saved request",
            "cue": "Earlier private cue",
            "known_facts": [{"id": "past", "text": "Earlier fact"}],
            "resources": {
                "items": [{"id": "note", "content": "Earlier resource", "content_truncated": True}],
                "truncated": True,
            },
            "credentials": "hidden-credential",
            "unknown_future_field": "Do not expose this",
        }
        await job(conn, run_id, view=view)
        await job(
            conn,
            run_id,
            1,
            view={"cue": "Later cue", "resources": {"items": [{"content": "Later resource"}]}},
        )
        await conn.execute(
            "UPDATE environment_runs SET current_window=1,phase='agents',status='running' "
            "WHERE id=%s",
            (run_id,),
        )
        await conn.execute(
            """INSERT INTO owner_resources(run_id,resource_id,owner_id,origin_id,title,kind,content)
               VALUES (%s,'note','owner-1','note','Current note','document','Current resource')""",
            (run_id,),
        )
    response = await client.get(path(run, "/0"))
    assert response.status_code == 200, response.text
    data = response.json()
    assert [m["content"] for m in data["chat"]] == ["Earlier owner", "Earlier agent"]
    assert data["phase"] == "completed"
    perspective = data["perspectives"][0]
    assert perspective["view"]["owner_name"] == "Saved persona"
    assert perspective["resources"]["view_label"] == "Start of Actor activation"
    assert perspective["resources"]["snapshot"]["truncated"] is True
    assert perspective["resources"]["snapshot"]["items"][0]["content_truncated"] is True
    for unexpected in (
        "Later owner",
        "Later cue",
        "Later resource",
        "Current resource",
        "hidden-",
        "unknown_future_field",
        "lease_token",
        "input_hash",
        "token_hash",
        "api_key",
    ):
        assert unexpected not in response.text
    summary = (await client.get(path(run))).json()["items"]
    assert summary[0]["counts"]["owner_chat"] == summary[0]["counts"]["agent_chat"] == 1
    assert summary[1]["phase"] == "agents"


async def test_completed_run_never_invents_a_turn_after_max_windows(client, pool):
    run = await create_run(client)
    run_id = UUID(run["run_id"])
    async with pool.connection() as conn:
        await emit(conn, run_id, "window_completed", 0, {}, "settled")
        await conn.execute(
            "UPDATE environment_runs SET current_window=max_windows,status='completed' WHERE id=%s",
            (run_id,),
        )
    data = (await client.get(path(run))).json()
    assert [item["window"] for item in data["items"]] == [0]
    assert (await client.get(path(run, f"/{data['max_windows']}"))).status_code == 422


async def test_mail_history_does_not_import_current_read_receipts(client, pool):
    run = await create_run(client)
    await Director(pool).control(run["run_id"], "start")
    inbox = InboxService(pool)
    message = await inbox.send(
        run["run_id"], "owner-1", "owner-2", "Question", "Earlier mail", uuid4()
    )
    before = (await client.get(path(run, "/0"))).json()["mail"]
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET current_window=1 WHERE id=%s", (run["run_id"],)
        )
    await inbox.acknowledge(run["run_id"], "owner-2", [message["id"]])
    await inbox.send(run["run_id"], "owner-2", "owner-1", "Later reply", "Later mail", uuid4())
    after = (await client.get(path(run, "/0"))).json()["mail"]
    assert before == after and len(after) == 1
    assert "read_at" not in after[0] and "acknowledged" not in after[0]
    assert after[0]["body"] == "Earlier mail"


async def test_board_turn_mapping_uses_events_and_tolerates_orphans(client, pool):
    run = await create_run(client)
    run_id = UUID(run["run_id"])
    await Director(pool).control(run_id, "start")
    headers = {"Authorization": "Bearer " + run["participants"][0]["token"]}
    posted = await client.post(
        f"/api/v1/clusters/{run['cluster_id']}/threads",
        headers=headers,
        json={"title": "Same title", "body": "Same body", "request_id": str(uuid4())},
    )
    assert posted.status_code == 201, posted.text
    first_id = posted.json()["id"]
    async with pool.connection() as conn:
        await emit(conn, run_id, "board_posted", 0, {"message_id": "bad uuid"}, "orphan-bad")
        await emit(conn, run_id, "board_posted", 0, {"message_id": str(uuid4())}, "orphan-missing")
        await emit(conn, run_id, "board_posted", 0, {"message_id": first_id}, "duplicate-event")
        await conn.execute("UPDATE environment_runs SET current_window=1 WHERE id=%s", (run_id,))
    await client.post(
        f"/api/v1/clusters/{run['cluster_id']}/threads",
        headers=headers,
        json={"title": "Same title", "body": "Same body", "request_id": str(uuid4())},
    )
    detail = (await client.get(path(run, "/0"))).json()
    assert [item["id"] for item in detail["board"]] == [first_id]
    assert detail["counts"]["board"] == 1
    assert detail["board"][0]["window"] == 0


async def test_turn_pagination_returns_every_record_and_jobs_only_once(client, pool):
    run = await create_run(client)
    run_id = UUID(run["run_id"])
    async with pool.connection() as conn:
        for index in range(205):
            await save_message(conn, run_id, "owner-1", "owner", f"Message {index:03d}", uuid4(), 0)
        await job(conn, run_id, view={"cue": "Saved cue"})
    pages = [
        (await client.get(path(run, f"/0?limit=100&offset={offset}"))).json()
        for offset in (0, 100, 200)
    ]
    assert [page["pagination"]["next_offset"] for page in pages] == [100, 200, None]
    assert [page["pagination"]["complete"] for page in pages] == [False, False, True]
    assert [len(page["chat"]) for page in pages] == [100, 100, 5]
    chat = [message for page in pages for message in page["chat"]]
    assert len({message["id"] for message in chat}) == 205
    assert [message["content"] for message in chat] == [
        f"Message {index:03d}" for index in range(205)
    ]
    assert pages[0]["pagination"]["collections"]["events"]["total"] == 205
    assert len(pages[0]["jobs"]) == len(pages[0]["perspectives"]) == 1
    assert pages[1]["jobs"] == pages[1]["perspectives"] == []
    assert pages[1]["pagination"]["jobs_included"] is False
    assert pages[0]["pagination"]["jobs_truncated"] is False


async def test_mail_and_board_pages_have_independent_counts(client, pool):
    run = await create_run(client)
    run_id = UUID(run["run_id"])
    board_ids = []
    async with pool.connection() as conn:
        for index in range(105):
            message_id = uuid4()
            board_ids.append(str(message_id))
            await conn.execute(
                """INSERT INTO messages(id,cluster_id,author_id,title,body,request_id,payload_hash)
                   VALUES (%s,%s,%s,'Board title',%s,%s,'hash')""",
                (
                    message_id,
                    run["cluster_id"],
                    run["participants"][0]["agent_id"],
                    f"Board {index}",
                    uuid4(),
                ),
            )
            await emit(
                conn,
                run_id,
                "board_posted",
                0,
                {"message_id": str(message_id)},
                f"board:{message_id}",
            )
        for index in range(103):
            await conn.execute(
                """INSERT INTO inbox_messages(id,run_id,sender_owner_id,sender_source,
                   sender_key,recipient_owner_id,subject,body,request_id,payload_hash,"window")
                   VALUES (%s,%s,'owner-1','agent','agent:owner-1','owner-2',
                   'Mail subject',%s,%s,'hash',0)""",
                (uuid4(), run_id, f"Mail {index}", uuid4()),
            )
    first = (await client.get(path(run, "/0"))).json()
    second = (await client.get(path(run, "/0?offset=100"))).json()
    assert first["pagination"]["next_offset"] == 100
    assert second["pagination"]["next_offset"] is None
    assert len(first["mail"]) == len(first["board"]) == 100
    assert len(second["mail"]) == 3 and len(second["board"]) == 5
    assert [item["id"] for page in (first, second) for item in page["board"]] == board_ids
    assert second["pagination"]["collections"]["mail"]["total"] == 103
    assert second["pagination"]["collections"]["board"]["total"] == 105


async def test_turn_replay_includes_all_fifteen_owner_pairs(client, pool):
    response = await client.post("/api/v1/runs", json=vehicle_scenario(15).model_dump(mode="json"))
    assert response.status_code == 201, response.text
    run = response.json()
    async with pool.connection() as conn:
        for member in run["participants"]:
            await job(
                conn,
                run["run_id"],
                owner=member["owner_id"],
                view={"owner_id": member["owner_id"], "cue": "Saved private cue"},
            )
            await job(
                conn,
                run["run_id"],
                owner=member["owner_id"],
                kind="agent",
                view={"window_id": 0, "owner_messages": []},
            )
    detail = (await client.get(path(run, "/0?limit=1"))).json()
    assert len(detail["jobs"]) == 30 and len(detail["perspectives"]) == 15
    assert detail["pagination"]["jobs_total"] == 30
    assert detail["pagination"]["jobs_truncated"] is False
    assert {p["owner_id"] for p in detail["perspectives"]} == {
        member["owner_id"] for member in run["participants"]
    }


async def test_english_replay_sidecar_retains_raw_messages_and_access_rules(
    client, app, pool, tmp_path
):
    from agentrpg.english_display import EnglishDisplay

    run = await create_run(client)
    source = "转账失败，余额不足。"
    async with pool.connection() as conn:
        await save_message(conn, UUID(run["run_id"]), "owner-1", "agent", source, uuid4(), 0)
    app.state.english_display = EnglishDisplay(
        tmp_path, infer=lambda texts: ["Transfer unsuccessful. Insufficient balance."] * len(texts)
    )
    response = await client.get(path(run, "/0?display=en"))
    assert response.status_code == 200
    assert response.json()["chat"][0]["content"] == source
    assert (
        response.json()["english"]["texts"][source]
        == "Transfer unsuccessful. Insufficient balance."
    )
    assert "english" not in (await client.get(path(run, "/0"))).json()
    denied = await client.get(
        path(run, "/0?display=en"),
        headers={"Authorization": "Bearer " + run["participants"][0]["token"]},
    )
    assert denied.status_code == 403 and source not in denied.text
