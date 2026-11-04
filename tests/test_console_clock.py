"""Replay portal of clocked runs, read from real PostgreSQL rows. No model is called."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from agentrpg import console_execution as execution
from agentrpg.clock import schedule
from agentrpg.director import Director, emit, save_message
from agentrpg.inbox import InboxService
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario

START = "2026-11-09T06:00:00+00:00"
HOUR = 3600
PAGE = 2 * HOUR
OTHER_PRIVATE_TEXT = "PRIVATE-PLOT-TEXT-917"
ROOT = Path(__file__).resolve().parents[1]
WEEK_SCENARIO = ROOT / "scenarios" / "agent_bill_week.json"


def clocked_spec(count=2, **overrides):
    payload = vehicle_scenario(count).model_dump(mode="json")
    payload["max_windows"] = 84
    payload["clock"] = {
        "start_at": START,
        "duration_minutes": 10080,
        "epoch_minutes": 120,
        "drama": "high",
        **overrides,
    }
    return ScenarioSpec.model_validate(payload)


async def create_run(pool, count=2, **overrides):
    """The real path: run creation saves the clock, its processes and its known future."""
    manifest = await Director(pool).create_run(clocked_spec(count, **overrides))
    return manifest, UUID(manifest["run_id"])


async def clear_queue(conn, run_id):
    """Remove the seeded future, so a test can state the exact queue that it expects."""
    await conn.execute("UPDATE clock_events SET status='done' WHERE run_id=%s", (run_id,))


async def set_process(conn, run_id, owner, kind, bar_s, state, until_s=None):
    await conn.execute(
        "UPDATE clock_processes SET bar_s=%s,state=%s,until_s=%s "
        "WHERE run_id=%s AND owner_id=%s AND kind=%s",
        (bar_s, state, until_s, run_id, owner, kind),
    )


async def move_clock(pool, run_id, seconds):
    """Move story time, as the engine does at the frontier."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE clock_state SET sim_s=%s WHERE run_id=%s", (seconds, run_id))
        await conn.execute(
            "UPDATE environment_runs SET current_window=%s,status='running',phase='agents' "
            "WHERE id=%s",
            (min(seconds // PAGE, 83), run_id),
        )


async def add_job(conn, run_id, owner, kind, window, seq, start_s=None, **fields):
    job_id = uuid4()
    await conn.execute(
        """INSERT INTO run_jobs(id,run_id,owner_id,"window",kind,input,input_hash,output,
        status,clock_seq,start_s,end_s,error) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            job_id,
            run_id,
            owner,
            window,
            kind,
            Jsonb(
                fields.get("input", {"owner_id": owner, "cue": f"cue {seq}", "secret": "hidden"})
            ),
            "hidden-input-hash",
            Jsonb({"text": f"{kind} output {seq}"}),
            fields.get("status", "completed"),
            seq,
            start_s,
            None if start_s is None else start_s + 60,
            fields.get("error"),
        ),
    )
    return job_id


async def add_action(conn, run_id, owner, process, label, start_s, duration_s, **fields):
    action_id = uuid4()
    await conn.execute(
        """INSERT INTO clock_actions(id,run_id,owner_id,process,label,detail,start_s,duration_s,
        status,reachable,key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            action_id,
            run_id,
            owner,
            process,
            label,
            Jsonb(fields.get("detail", {"category": "errand"})),
            start_s,
            duration_s,
            fields.get("status", "done"),
            fields.get("reachable"),
            f"{process}:{owner}:{label}:{start_s}",
        ),
    )
    return action_id


async def add_draw(conn, run_id, action_id, outcome, fire_s, *, u=0.05, v=0.5, level="high"):
    await conn.execute(
        """INSERT INTO clock_draws(run_id,action_id,level,u,v,outcome,fire_s)
        VALUES(%s,%s,%s,%s,%s,%s,%s)""",
        (run_id, action_id, level, u, v, outcome, fire_s),
    )


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def turn_path(run_id, window, **query):
    suffix = "?" + "&".join(f"{key}={value}" for key, value in query.items()) if query else ""
    return f"/api/v1/observer/runs/{run_id}/turns/{window}{suffix}"


async def read_page(client, run_id, window, limit=100):
    """Follow next_offset as the browser does and merge every collection."""
    offset, merged = 0, None
    while True:
        response = await client.get(turn_path(run_id, window, limit=limit, offset=offset))
        assert response.status_code == 200, response.text
        page = response.json()
        if merged is None:
            merged = page
        else:
            for key in ("chat", "mail", "board", "events", "jobs", "actions", "perspectives"):
                merged[key].extend(page[key])
        offset = page["pagination"]["next_offset"]
        if offset is None:
            return merged


async def post(client, cluster_id, participant, title, body):
    response = await client.post(
        f"/api/v1/clusters/{cluster_id}/threads",
        json={"title": title, "body": body, "request_id": str(uuid4())},
        headers=bearer(participant["token"]),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def direct(client, cluster_id, participant, to, title, body):
    response = await client.post(
        f"/api/v1/clusters/{cluster_id}/direct-threads",
        json={"title": title, "body": body, "request_id": str(uuid4()), "to": to},
        headers=bearer(participant["token"]),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def reply(client, cluster_id, participant, thread_id, body):
    response = await client.post(
        f"/api/v1/clusters/{cluster_id}/threads/{thread_id}/replies",
        json={"body": body, "request_id": str(uuid4())},
        headers=bearer(participant["token"]),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_one_page_returns_every_job_of_one_owner_in_story_order(client, pool):
    """More than the old LIMIT 30, and many jobs for each owner on the same page."""
    _, run_id = await create_run(pool)
    await move_clock(pool, run_id, HOUR)
    async with pool.connection() as conn:
        expected = []
        for index in range(40):
            start = HOUR + 40 - index
            kind = "actor" if index % 2 else "agent"
            job_id = await add_job(conn, run_id, "owner-1", kind, 0, index, start)
            expected.append((start, str(job_id)))
    page = await read_page(client, run_id, 0, limit=7)
    assert page["jobs"], "The page must hold jobs."
    assert [job["id"] for job in page["jobs"]] == [job_id for _, job_id in sorted(expected)]
    assert [job["story_s"] for job in page["jobs"]] == sorted(start for start, _ in expected)
    assert page["pagination"]["jobs_truncated"] is False
    assert page["jobs"][0]["at"] == "Mon 07:00"
    assert all("secret" not in job["input"] for job in page["jobs"])
    views = page["perspectives"]
    assert len(views) == 20
    assert {view["job_id"] for view in views} == {
        job["id"] for job in page["jobs"] if job["kind"] == "actor"
    }
    assert all(view["at"].startswith("Mon 07:00") for view in views)
    summary = (await client.get(f"/api/v1/observer/runs/{run_id}/turns")).json()["items"][0]
    assert summary["jobs"] == {"total": 40, "actor": 20, "agent": 20, "completed": 40, "failed": 0}


async def test_page_holds_every_record_kind_in_story_order_and_only_its_own(client, pool):
    manifest, run_id = await create_run(pool)
    cluster = manifest["cluster_id"]
    alice, bob = manifest["participants"]
    await move_clock(pool, run_id, PAGE + 300)
    async with pool.connection() as conn:
        message = await save_message(conn, run_id, alice["owner_id"], "owner", "Hello", uuid4(), 1)
        await emit(conn, run_id, "resource_updated", 1, {"resource_id": "r1"}, "rec", "owner-1")
        tool = {"tool": "search", "arguments": {"q": "x"}, "result": {"ok": True}}
        await emit(conn, run_id, "assistant_tool_call", 1, tool, "tool", "owner-1")
        job_id = await add_job(conn, run_id, "owner-2", "agent", 1, 1, PAGE + 300)
        action_id = await add_action(conn, run_id, "owner-1", "actor", "Buy food", PAGE + 200, 900)
        await add_draw(conn, run_id, action_id, "mild", PAGE + 500)
    await InboxService(pool).send(run_id, "owner-1", "owner-2", "Rent", "Rent is due.", uuid4())
    await post(client, cluster, alice, "Public note", "A public body")
    secret = await direct(client, cluster, alice, [bob["owner_id"]], "Quiet word", "A direct body")
    await reply(client, cluster, bob, secret["id"], "A direct reply")
    await move_clock(pool, run_id, 2 * PAGE + 10)
    await post(client, cluster, bob, "Next page", "On the next page")

    page = await read_page(client, run_id, 1)
    assert page["at"] == "Mon 08:00" and page["until"] == "10:00"
    assert [item["content"] for item in page["chat"]] == ["Hello"]
    assert [item["subject"] for item in page["mail"]] == ["Rent"]
    assert [item["id"] for item in page["jobs"]] == [str(job_id)]
    assert [item["label"] for item in page["actions"]] == ["Buy food"]
    assert page["actions"][0]["at"] == "Mon 08:03"
    assert page["perturbations"][0]["outcome"] == "mild"
    assert {item["kind"] for item in page["events"]} >= {
        "owner_message",
        "resource_updated",
        "assistant_tool_call",
    }
    assert message["id"]
    board = page["board"]
    assert [item["title"] for item in board] == ["Public note", "Quiet word", None]
    assert [item["visibility"] for item in board] == ["public", "direct", "direct"]
    assert [person["owner_id"] for person in board[1]["participants"]] == [
        alice["owner_id"],
        bob["owner_id"],
    ]
    assert board[0]["participants"] == []
    for collection in (page["chat"], page["mail"], board, page["events"], page["actions"]):
        moments = [item["story_s"] for item in collection]
        assert moments == sorted(moments)
        assert all(PAGE <= moment < 2 * PAGE for moment in moments)
    counts = page["counts"]
    assert counts["board"] == 3 and counts["direct"] == 2 and counts["actions"] == 1
    assert counts["perturbations"] == 1 and counts["mail"] == 1
    assert page["pagination"]["complete"] is True
    other = await read_page(client, run_id, 2)
    assert [item["title"] for item in other["board"]] == ["Next page"]
    assert other["chat"] == [] and other["jobs"] == []


async def test_every_page_exists_and_final_page_holds_the_end_of_the_run(client, pool):
    _, run_id = await create_run(pool)
    await move_clock(pool, run_id, 10080 * 60)
    async with pool.connection() as conn:
        await emit(conn, run_id, "run_end", 84, {"reason": "horizon"}, "end")
    listed = (await client.get(f"/api/v1/observer/runs/{run_id}/turns")).json()
    assert listed["clocked"] is True
    assert [item["window"] for item in listed["items"]] == list(range(84))
    assert listed["items"][0]["at"] == "Mon 06:00"
    assert listed["items"][9]["at"] == "Tue 00:00"
    assert listed["items"][-1]["until"] == "06:00"
    final = await read_page(client, run_id, 83)
    assert [event["kind"] for event in final["events"]] == ["run_end"]
    assert (await client.get(turn_path(run_id, 84))).status_code == 422


async def test_a_future_page_is_not_exposed(client, pool):
    _, run_id = await create_run(pool)
    await move_clock(pool, run_id, PAGE)
    assert (await client.get(turn_path(run_id, 1))).status_code == 200
    assert (await client.get(turn_path(run_id, 2))).status_code == 404


async def test_clock_view_reports_processes_queue_draws_sites_status_and_spend(client, pool):
    _, run_id = await create_run(
        pool,
        lifecycle=[
            {
                "kind": "assistant_subscription",
                "owner_id": "owner-1",
                "site_id": "agentcare",
                "bill_id": "plan-nov",
                "due_at": "Sat 23:59",
                "amount": 50,
            }
        ],
    )
    now = 3 * HOUR + 300
    await move_clock(pool, run_id, now)
    campaign = uuid4()
    async with pool.connection() as conn:
        await clear_queue(conn, run_id)
        await set_process(conn, run_id, "owner-1", "actor", now + 900, "busy")
        await set_process(conn, run_id, "owner-1", "agent", now, "waiting", now + 600)
        await set_process(conn, run_id, "owner-2", "actor", now, "idle")
        await set_process(conn, run_id, "owner-2", "agent", now, "dead")
        current = await add_action(
            conn, run_id, "owner-1", "actor", "Walk the dog", now - 60, 1800, status="active"
        )
        await add_draw(conn, run_id, current, "extreme", now + 300, u=0.02, v=0.1)
        done = await add_action(conn, run_id, "owner-2", "actor", "Lunch", HOUR, 1800)
        await add_draw(conn, run_id, done, "mild", HOUR + 900, u=0.1, v=0.9)
        for index in range(8):
            quiet = await add_action(conn, run_id, "owner-2", "actor", f"Quiet {index}", index, 1)
            await add_draw(conn, run_id, quiet, "none", None, u=0.9, v=0.9)
        await add_action(conn, run_id, "owner-1", "director", "Move the bus", HOUR, 60)
        for index in range(25):
            await schedule(
                conn,
                run_id,
                now + 100000 - index,
                "beat",
                payload={"cue": OTHER_PRIVATE_TEXT},
                key=f"beat-{index:02d}",
                owner_id="owner-1",
            )
        await schedule(conn, run_id, now + 10, "agent_wake", key="wake-1", owner_id="owner-1")
        await conn.execute(
            """INSERT INTO web_sites(run_id,site_id,name,kind,spec,source,search_doc,sim_s)
            VALUES(%s,'agentcare','AgentCare','portal',%s,'plot',to_tsvector('agentcare'),0)""",
            (run_id, Jsonb({"tagline": "Plans for assistants"})),
        )
        await conn.execute(
            """INSERT INTO web_bills(run_id,site_id,bill_id,owner_id,title,amount,due_text,status)
            VALUES(%s,'agentcare','plan-nov','owner-1','November plan',50,'Sat','unpaid')""",
            (run_id,),
        )
        await emit(conn, run_id, "assistant_expired", 1, {"reason": "unpaid"}, "exp", "owner-2")
        await conn.execute(
            "INSERT INTO model_campaigns(id,name,limit_usd) VALUES(%s,'Portal',30)", (campaign,)
        )
        await conn.execute(
            """INSERT INTO model_spend_attempts(id,campaign_id,run_id,owner_id,role,model,
            reserved_usd,reported_usd,status,prompt_token_bound,output_token_bound,price_snapshot)
            VALUES(%s,%s,%s,'owner-1','agent','m',1,0.25,'reported',1,1,'{}'),
            (%s,%s,%s,'owner-1','actor','m',0.5,NULL,'uncertain',1,1,'{}')""",
            (uuid4(), campaign, run_id, uuid4(), campaign, run_id),
        )
    response = await client.get(f"/api/v1/runs/{run_id}/clock")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["role"] == "admin"
    assert view["now_s"] == now and view["now"] == "Mon 09:05"
    assert view["start"] == "Mon 06:00" and view["horizon"] == "Wk2 Mon 06:00"
    assert view["horizon_s"] == 604800 and view["epoch_s"] == PAGE and view["drama"] == "high"
    assert view["days"][0] == {"start_s": 0, "label": "Mon"}
    assert view["days"][1] == {"start_s": 18 * HOUR, "label": "Tue"}
    assert [item["label"] for item in view["days"]] == [
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
        "Sun",
        "Mon",
    ]
    processes = {(item["owner_id"], item["kind"]): item for item in view["processes"]}
    assert len(processes) == 4
    actor = processes[("owner-1", "actor")]
    assert actor["state"] == "busy" and actor["bar_s"] == now + 900 and actor["bar"] == "Mon 09:20"
    assert actor["current_action"]["label"] == "Walk the dog"
    assert actor["current_action"]["ends"] == "Mon 09:34"
    assert processes[("owner-1", "agent")]["until"] == "Mon 09:15"
    assert processes[("owner-1", "agent")]["current_action"] is None
    assert len(view["queue"]) == 20
    assert view["queue"][0]["kind"] == "agent_wake" and view["queue"][0]["at"] == "Mon 09:05"
    dues = [item["due_s"] for item in view["queue"]]
    assert dues == sorted(dues)
    assert view["queue"][1]["payload"] == {"cue": OTHER_PRIVATE_TEXT}
    assert "tie" not in view["queue"][0]
    outcomes = [
        (item["outcome"], item["u"], item["v"], item["fired"]) for item in view["perturbations"]
    ]
    assert outcomes == [("mild", 0.1, 0.9, True), ("extreme", 0.02, 0.1, False)]
    assert view["perturbations"][1]["label"] == "Walk the dog"
    assert view["interference"] == {
        "drawn": 10,
        "mild": 1,
        "extreme": 1,
        "interfered": 2,
        "rate": 0.2,
        "target_rate": 0.13,
    }
    assert view["sites"][0]["url"] == f"/sim/{run_id}/agentcare/"
    assert view["sites"][0]["name"] == "AgentCare"
    assert view["sites"][0]["tagline"] == "Plans for assistants"
    assistants = {item["owner_id"]: item for item in view["assistants"]}
    assert assistants["owner-1"]["status"] == "active"
    bill = assistants["owner-1"]["bills"][0]
    assert bill["status"] == "unpaid" and bill["due_at"] == "Sat 23:59"
    assert bill["due_s"] == ((5 * 24 * 60) + (23 * 60 + 59) - 6 * 60) * 60
    assert assistants["owner-2"]["status"] == "expired"
    assert assistants["owner-2"]["expired_at"] == "Mon 09:05"
    assert float(view["spend"]["spent_usd"]) == 0.25
    assert float(view["spend"]["reserved_usd"]) == 0.5
    assert view["spend"]["attempts"] == 2
    assert [item["role"] for item in view["spend"]["by_role"]] == ["actor", "agent"]
    assert float(view["spend"]["campaigns"][0]["limit_usd"]) == 30
    assert {block["process"] for block in view["blocks"]} == {"actor", "director"}
    assert sum(block["count"] for block in view["blocks"]) == 11


async def test_agent_credential_sees_only_time_and_its_own_process(client, pool):
    manifest, run_id = await create_run(pool)
    alice, bob = manifest["participants"]
    await move_clock(pool, run_id, HOUR)
    async with pool.connection() as conn:
        await clear_queue(conn, run_id)
        await set_process(conn, run_id, "owner-1", "agent", 10, "active")
        await set_process(conn, run_id, "owner-2", "agent", 20, "busy")
        peer = await add_action(
            conn, run_id, "owner-2", "agent", OTHER_PRIVATE_TEXT, HOUR, 60, status="active"
        )
        await add_draw(conn, run_id, peer, "extreme", HOUR + 30)
        await add_action(conn, run_id, "owner-1", "agent", "Own step", HOUR, 60, status="active")
        await schedule(
            conn, run_id, HOUR + 60, "beat", payload={"cue": OTHER_PRIVATE_TEXT}, key="plot-one"
        )
        await schedule(conn, run_id, HOUR + 70, "agent_wake", key="own-wake", owner_id="owner-1")
        await schedule(conn, run_id, HOUR + 80, "agent_wake", key="peer-wake", owner_id="owner-2")
    response = await client.get(f"/api/v1/runs/{run_id}/clock", headers=bearer(alice["token"]))
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["role"] == "agent" and view["now"] == "Mon 07:00"
    raw = response.text
    assert OTHER_PRIVATE_TEXT not in raw
    assert "plot-one" not in raw and "peer-wake" not in raw and "payload" not in raw
    assert view["queue"] == [
        {"due_s": HOUR + 60, "at": "Mon 07:01", "kind": "scheduled"},
        {"due_s": HOUR + 70, "at": "Mon 07:01", "kind": "agent_wake"},
        {"due_s": HOUR + 80, "at": "Mon 07:01", "kind": "scheduled"},
    ]
    assert view["perturbations"] == [] and view["blocks"] == []
    assert view["spend"] is None and view["drama"] is None and view["interference"] is None
    processes = {item["owner_id"]: item for item in view["processes"]}
    assert processes["owner-1"]["current_action"]["label"] == "Own step"
    assert set(processes["owner-2"]) == {"owner_id", "owner_name", "kind", "state"}

    other = await Director(pool).create_run(vehicle_scenario(1))
    stranger = other["participants"][0]["token"]
    foreign = await client.get(f"/api/v1/runs/{run_id}/clock", headers=bearer(stranger))
    assert foreign.status_code == 404
    # A participant from this run also keeps away from the research reads.
    for route in ("timeline", "board"):
        path = f"/api/v1/runs/{run_id}/{route}"
        assert (await client.get(path, headers=bearer(bob["token"]))).status_code == 403
        assert (await client.get(path, headers={"Authorization": ""})).status_code == 401
    for route in ("clock", "timeline", "board"):
        path = f"/api/v1/observer/runs/{run_id}/{route}"
        assert (await client.get(path, headers=bearer(bob["token"]))).status_code == 403
        assert (await client.get(path, headers={"Authorization": ""})).status_code == 401
        assert (await client.post(path, json={})).status_code == 405


async def test_loopback_operator_reads_observer_routes_but_not_the_bearer_routes(app, pool):
    import httpx

    app.state.settings.host = "127.0.0.1"
    _, run_id = await create_run(pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
        base_url="http://127.0.0.1:8000",
    ) as local:
        for route in ("clock", "timeline", "board"):
            assert (await local.get(f"/api/v1/observer/runs/{run_id}/{route}")).status_code == 200
            assert (await local.get(f"/api/v1/runs/{run_id}/{route}")).status_code == 401


async def test_timeline_pages_by_keyset_and_merges_in_story_order(client, pool):
    manifest, run_id = await create_run(pool)
    alice, bob = manifest["participants"]
    expected_ids = set()
    async with pool.connection() as conn:
        await conn.execute("UPDATE environment_runs SET status='running' WHERE id=%s", (run_id,))
    for index in range(60):
        await move_clock(pool, run_id, index * 1000)
        window = index * 1000 // PAGE
        async with pool.connection() as conn:
            action = await add_action(
                conn, run_id, "owner-1", "actor", f"Errand {index}", index * 1000, 400
            )
            expected_ids.add(str(action))
            if index % 3 == 0:
                job = await add_job(conn, run_id, "owner-2", "agent", window, index, index * 1000)
                expected_ids.add(str(job))
            if index % 5 == 0:
                await emit(conn, run_id, "world_changed", window, {"n": index}, f"w{index}")
            if index % 7 == 0:
                await save_message(
                    conn, run_id, "owner-1", "owner", f"Note {index}", uuid4(), window
                )
            if index % 11 == 0:
                await add_draw(conn, run_id, action, "mild", index * 1000 + 200)
        if index % 13 == 0:
            await post(client, manifest["cluster_id"], alice, f"Post {index}", "Body")
        if index % 17 == 0:
            await direct(
                client, manifest["cluster_id"], bob, [alice["owner_id"]], f"Word {index}", "Hush"
            )

    seen, after, pages = [], None, 0
    while True:
        query = {"limit": 17} | ({"after": after} if after else {})
        response = await client.get(f"/api/v1/runs/{run_id}/timeline", params=query)
        assert response.status_code == 200, response.text
        result = response.json()
        pages += 1
        assert len(result["items"]) <= 17
        seen += result["items"]
        after = result["next_after"]
        if after is None:
            assert result["complete"] is True
            break
    assert pages > 3
    ids = [(item["type"], item["id"]) for item in seen]
    assert len(ids) == len(set(ids)), "A page position must not repeat a row."
    assert expected_ids <= {item["id"] for item in seen}
    moments = [item["story_s"] for item in seen]
    assert moments == sorted(moments)
    types = {item["type"] for item in seen}
    assert types == {"action", "job", "event", "chat", "perturbation", "board", "direct"}
    assert not {item["title"] for item in seen} & {"owner_message", "board_posted", "trial_started"}
    # One read returns the same order as the paged reads.
    whole = (await client.get(f"/api/v1/runs/{run_id}/timeline", params={"limit": 500})).json()
    assert [(item["type"], item["id"]) for item in whole["items"]] == ids

    action = next(item for item in seen if item["type"] == "action")
    assert action["at"] == "Mon 06:00" and action["duration_s"] == 400
    assert action["window"] == 0 and action["detail"] == {"category": "errand"}
    assert next(item for item in seen if item["type"] == "perturbation")["status"] == "fired"

    bounded = await client.get(
        f"/api/v1/runs/{run_id}/timeline", params={"from": 5000, "to": 6000, "limit": 500}
    )
    bounded_items = bounded.json()["items"]
    assert bounded_items
    assert all(5000 <= item["story_s"] < 6000 for item in bounded_items)
    assert "Errand 5" in {item["title"] for item in bounded_items}


async def test_timeline_refuses_bad_positions_and_ranges(client, pool):
    _, run_id = await create_run(pool)
    _, other_id = await create_run(pool)
    async with pool.connection() as conn:
        for index in range(3):
            await add_action(conn, other_id, "owner-1", "actor", f"A{index}", index, 1)
    position = (await client.get(f"/api/v1/runs/{other_id}/timeline", params={"limit": 1})).json()[
        "next_after"
    ]
    assert position
    path = f"/api/v1/runs/{run_id}/timeline"
    assert (await client.get(path, params={"after": position})).status_code == 422
    assert (await client.get(path, params={"after": "not-a-position"})).status_code == 422
    assert (await client.get(path, params={"from": 10, "to": 5})).status_code == 422
    assert (await client.get(path, params={"limit": 0})).status_code == 422
    assert (await client.get(path, params={"limit": 501})).status_code == 422
    assert (await client.get(path, params={"from": "tuesday"})).status_code == 422
    assert (await client.get(path)).json() == {
        "run_id": str(run_id),
        "items": [],
        "next_after": None,
        "complete": True,
    }


async def test_board_read_labels_public_and_direct_with_participants(client, pool):
    manifest, run_id = await create_run(pool, 3)
    cluster = manifest["cluster_id"]
    alice, bob, carol = manifest["participants"]
    await move_clock(pool, run_id, 8 * HOUR)
    public = await post(client, cluster, alice, "Open <b>note</b>", "<img src=x onerror=alert(1)>")
    await reply(client, cluster, carol, public["id"], "A public reply")
    secret = await direct(client, cluster, bob, [alice["owner_id"]], "Private word", "Only us")
    await reply(client, cluster, alice, secret["id"], "Understood")
    async with pool.connection() as conn:
        old = await conn.execute(
            """UPDATE messages SET created_at=%s WHERE id=%s RETURNING id""",
            (datetime.fromisoformat(START) - timedelta(days=12), public["id"]),
        )
        assert await old.fetchone()
    response = await client.get(f"/api/v1/runs/{run_id}/board")
    assert response.status_code == 200, response.text
    board = response.json()
    assert board["counts"] == {"public": 2, "direct": 2}
    items = board["items"]
    assert [item["visibility"] for item in items] == ["public", "public", "direct", "direct"]
    assert items[0]["title"] == "Open <b>note</b>"
    assert items[0]["body"] == "<img src=x onerror=alert(1)>"
    assert items[0]["at"] == "12 days before the start"
    assert items[1]["at"] == "Mon 14:00"
    assert items[0]["participants"] == []
    owners = [person["owner_id"] for person in items[2]["participants"]]
    assert owners == [bob["owner_id"], alice["owner_id"]]
    assert carol["owner_id"] not in owners
    assert items[3]["thread_id"] == secret["id"]
    assert [person["owner_id"] for person in items[3]["participants"]] == owners
    assert all("seq" not in item for item in items)

    # A participant's own board read still hides the thread that does not name them.
    seen = await client.get(f"/api/v1/clusters/{cluster}/threads", headers=bearer(carol["token"]))
    assert secret["id"] not in {item["id"] for item in seen.json()["items"]}

    first = (await client.get(f"/api/v1/runs/{run_id}/board", params={"limit": 3})).json()
    assert len(first["items"]) == 3 and first["next_after"]
    second = (
        await client.get(
            f"/api/v1/runs/{run_id}/board", params={"limit": 3, "after": first["next_after"]}
        )
    ).json()
    assert [item["id"] for item in first["items"] + second["items"]] == [
        item["id"] for item in items
    ]
    assert second["next_after"] is None
    bad = await client.get(f"/api/v1/runs/{run_id}/board", params={"after": "bad"})
    assert bad.status_code == 422


async def test_english_display_adds_only_its_sidecar(client, pool):
    _, run_id = await create_run(pool)
    for route in ("clock", "timeline", "board"):
        response = await client.get(f"/api/v1/observer/runs/{run_id}/{route}?display=en")
        assert response.status_code == 200, response.text
        assert response.json()["english"]["local"] is True
    page = await client.get(turn_path(run_id, 0, display="en"))
    assert page.status_code == 200, page.text
    assert page.json()["english"]["local"] is True


async def test_clock_view_before_start_and_on_a_window_run(client, pool):
    _, run_id = await create_run(pool)
    view = (await client.get(f"/api/v1/runs/{run_id}/clock")).json()
    assert view["started"] is False and view["now"] == "Mon 06:00"
    assert len(view["processes"]) == 4 and {item["state"] for item in view["processes"]} == {"idle"}
    assert view["blocks"] == [] and view["perturbations"] == [] and view["sites"] == []
    assert view["interference"]["rate"] is None and view["interference"]["drawn"] == 0
    # The seeded future is queued in the pop order of rule R2.
    assert view["queue"][0]["kind"] == "boundary" and view["queue"][0]["at"] == "Mon 08:00"
    assert view["queue"][0]["payload"] == {"window": 0}
    assert [item["status"] for item in view["assistants"]] == ["active", "active"]

    plain = await Director(pool).create_run(vehicle_scenario(2))
    for route in ("clock", "timeline"):
        missing = await client.get(f"/api/v1/runs/{plain['run_id']}/{route}")
        assert missing.status_code == 404 and "no clock" in missing.json()["detail"].lower()
    unknown = await client.get(f"/api/v1/runs/{uuid4()}/clock")
    assert unknown.status_code == 404


async def test_window_run_replay_shape_is_unchanged(client, pool):
    plain = await Director(pool).create_run(vehicle_scenario(2))
    run_id = UUID(plain["run_id"])
    async with pool.connection() as conn:
        await add_job(conn, run_id, "owner-1", "actor", 0, 0)
    listed = (await client.get(f"/api/v1/observer/runs/{run_id}/turns")).json()
    assert set(listed) == {"run_id", "current_window", "max_windows", "items"}
    assert set(listed["items"][0]) == {
        "window",
        "phase",
        "status",
        "counts",
        "jobs",
        "has_actor_view",
    }
    assert set(listed["items"][0]["counts"]) == {
        "owner_chat",
        "agent_chat",
        "events",
        "mail",
        "board",
    }
    detail = (await client.get(turn_path(run_id, 0))).json()
    assert "actions" not in detail and "perturbations" not in detail
    assert "at" not in detail["jobs"][0] and "story_s" not in detail["jobs"][0]
    assert "at" not in detail["perspectives"][0]
    assert set(detail["pagination"]["collections"]) == {"chat", "mail", "board", "events"}


@pytest.fixture
def controls(app, tmp_path):
    class Recording:
        def __init__(self):
            self.launched = []

        async def launch(self, operation, run_id):
            self.launched.append((operation, run_id))

        async def close(self):
            return None

    app.state.console_root = tmp_path
    app.state.console_supervisor = Recording()
    return app.state.console_supervisor


@pytest.mark.parametrize("mode", ["step", "continue"])
async def test_step_and_continue_refuse_a_clocked_run_without_side_effects(
    client, pool, controls, mode
):
    _, run_id = await create_run(pool)
    response = await client.post(
        f"/api/v1/console/runs/{run_id}/execution",
        json={"request_id": str(uuid4()), "mode": mode},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == execution.CLOCKED_REFUSAL
    assert "run script" in response.json()["detail"]
    assert controls.launched == []
    async with pool.connection() as conn:
        saved = await (
            await conn.execute(
                "SELECT count(*) AS total FROM console_executions WHERE run_id=%s", (run_id,)
            )
        ).fetchone()
    assert saved["total"] == 0
    state = (await client.get(f"/api/v1/console/runs/{run_id}/execution")).json()
    assert state["capabilities"]["can_execute"] is False
    assert state["capabilities"]["reason"] == execution.CLOCKED_REFUSAL


async def test_a_window_run_is_not_refused(client, pool, controls):
    plain = await Director(pool).create_run(vehicle_scenario(2))
    response = await client.post(
        f"/api/v1/console/runs/{plain['run_id']}/execution",
        json={"request_id": str(uuid4()), "mode": "step"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] != execution.CLOCKED_REFUSAL
    assert "credential" in response.json()["detail"]


def test_action_blocks_stay_bounded_and_keep_long_actions_apart():
    from agentrpg.console_api import MAX_BLOCKS, action_blocks

    long_actions = [
        {
            "owner_id": "a",
            "process": "actor",
            "label": f"Long {index}",
            "category": "work",
            "status": "done",
            "start_s": index * 7200,
            "duration_s": 7200,
        }
        for index in range(3)
    ]
    assert [block["count"] for block in action_blocks(long_actions, 604800)] == [1, 1, 1]
    tools = [
        {
            "owner_id": "a",
            "process": "agent",
            "label": "search",
            "category": None,
            "status": "done",
            "start_s": index * 40,
            "duration_s": 30,
        }
        for index in range(50)
    ]
    joined = action_blocks(tools, 604800)
    assert len(joined) == 1 and joined[0]["count"] == 50
    assert joined[0]["duration_s"] == 49 * 40 + 30
    many = [{**tools[0], "start_s": index * 2000} for index in range(30000)]
    bounded = action_blocks(many, 60000000)
    assert len(bounded) <= MAX_BLOCKS
    assert sum(block["count"] for block in bounded) == 30000


@pytest.mark.skipif(not WEEK_SCENARIO.exists(), reason="The week scenario file is not present.")
async def test_a_real_clocked_scenario_file_reports_its_declared_clock(client, pool):
    spec = ScenarioSpec.model_validate(json.loads(WEEK_SCENARIO.read_text()))
    manifest = await Director(pool).create_run(spec)
    view = (await client.get(f"/api/v1/runs/{manifest['run_id']}/clock")).json()
    assert view["start"] == "Mon 06:00" and view["horizon_s"] == spec.clock.duration_minutes * 60
    assert view["drama"] == spec.clock.drama
    assert len(view["processes"]) == 2 * len(spec.owners)
    assert {item["site_id"] for item in view["sites"]} == {
        item["site_id"] for item in spec.clock.sites
    }
    bills = [bill for item in view["assistants"] for bill in item["bills"]]
    assert [bill["bill_id"] for bill in bills] == [item.bill_id for item in spec.clock.lifecycle]
    assert all(bill["status"] == "unpaid" and bill["due_s"] > 0 for bill in bills)
    board = (await client.get(f"/api/v1/runs/{manifest['run_id']}/board")).json()
    assert len(board["items"]) >= len(spec.clock.seed_board)
    assert all(item["story_s"] < 0 for item in board["items"])
    assert all(item["at"].endswith("before the start") for item in board["items"])


def engine_control():
    """The engine's own control: real graphs and API, scripted models, no provider call."""
    name = "try_clock_control"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


async def ids_of(pool, query, *args):
    async with pool.connection() as conn:
        records = await (await conn.execute(query, args)).fetchall()
    return {str(item["id"]) for item in records}


async def test_pages_show_every_saved_row_of_a_real_engine_run(
    app, client, pool, test_database_url
):
    run, *_ = await engine_control().run_control(
        app, test_database_url, latency_seed=0, parallel=8, max_latency=0
    )
    assert run["status"] == "completed", run["result"]
    run_id = run["id"]
    listed = (await client.get(f"/api/v1/observer/runs/{run_id}/turns")).json()
    assert [item["window"] for item in listed["items"]] == list(range(run["max_windows"]))
    pages = [await read_page(client, run_id, item["window"], limit=13) for item in listed["items"]]

    def shown(collection):
        values = [str(item["id"]) for page in pages for item in page[collection]]
        assert len(values) == len(set(values)), f"{collection} repeats a row across pages."
        return set(values)

    audit = ["control_audit_enabled", "operator_control", "trial_started", "trial_finished"]
    assert shown("jobs") == await ids_of(pool, "SELECT id FROM run_jobs WHERE run_id=%s", run_id)
    assert shown("chat") == await ids_of(
        pool, "SELECT id FROM owner_messages WHERE run_id=%s", run_id
    )
    assert shown("mail") == await ids_of(
        pool, "SELECT id FROM inbox_messages WHERE run_id=%s", run_id
    )
    assert shown("events") == await ids_of(
        pool, "SELECT id FROM run_events WHERE run_id=%s AND kind<>ALL(%s)", run_id, audit
    )
    assert shown("actions") == await ids_of(
        pool, "SELECT id FROM clock_actions WHERE run_id=%s", run_id
    )
    assert shown("board") == await ids_of(
        pool, "SELECT id FROM messages WHERE cluster_id=%s", run["cluster_id"]
    )
    assert shown("jobs"), "The control run must save jobs."
    assert shown("actions"), "The control run must save actions."
    for page in pages:
        for collection in ("chat", "mail", "board", "events", "jobs", "actions"):
            moments = [item["story_s"] for item in page[collection]]
            assert moments == sorted(moments), f"{collection} is out of story order."
        assert page["jobs"] == sorted(page["jobs"], key=lambda job: job["start_s"])
        assert page["counts"]["actions"] == len(page["actions"])
        assert page["counts"]["owner_chat"] + page["counts"]["agent_chat"] == len(page["chat"])
        assert page["counts"]["board"] == len(page["board"])
    tools = [
        event
        for page in pages
        for event in page["events"]
        if event["kind"] == "assistant_tool_call"
    ]
    assert tools, "The control run must save tool calls."

    view = (await client.get(f"/api/v1/runs/{run_id}/clock")).json()
    assert view["status"] == "completed" and view["started"] is True
    assert view["now_s"] == view["horizon_s"] == 6 * HOUR
    assert len(view["processes"]) == 6 and view["drama"] == "off"
    assert view["blocks"] and view["queue"] == []
    assert view["interference"]["interfered"] == 0

    seen, after = [], None
    while True:
        query = {"limit": 25} | ({"after": after} if after else {})
        timeline = (await client.get(f"/api/v1/runs/{run_id}/timeline", params=query)).json()
        seen += timeline["items"]
        after = timeline["next_after"]
        if after is None:
            break
    moments = [item["story_s"] for item in seen]
    assert moments == sorted(moments)
    assert shown("actions") | shown("jobs") <= {item["id"] for item in seen}
    assert next(item for item in seen if item["type"] == "action")["detail"]


# The browser checks below use the real API in this process through route interception, so no
# server starts. They run when the optional browser extra and a Chromium build are present.
async def open_browser():
    pytest.importorskip("playwright.async_api", reason="Install the browser extra.")
    from playwright.async_api import Error, async_playwright

    driver = await async_playwright().start()
    executable = os.environ.get("AGENTRPG_TEST_CHROMIUM")
    try:
        browser = await driver.chromium.launch(
            headless=True, **({"executable_path": executable} if executable else {})
        )
    except Error:
        await driver.stop()
        pytest.skip("No Chromium build is available for this Playwright version.")
    return driver, browser


async def browser_page(browser, client, *, width=1440, height=900, failures=None):
    """A page whose every request goes to the app in this process."""
    context = await browser.new_context(
        viewport={"width": width, "height": height}, reduced_motion="reduce"
    )
    page = await context.new_page()
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    async def forward(route):
        request = route.request
        parts = urlsplit(request.url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        requests.append((request.method, target))
        for fragment, status in (failures or {}).items():
            if fragment in target:
                await route.fulfill(status=status, body=b"{}")
                return
        if "/sim/" in parts.path:
            await route.fulfill(status=200, body=b"site", content_type="text/plain")
            return
        response = await client.request(
            request.method,
            target,
            content=request.post_data_buffer,
            headers={"Content-Type": request.headers.get("content-type", "application/json")},
        )
        await route.fulfill(
            status=response.status_code,
            body=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
        )

    await context.route("http://portal.test/**", forward)
    return page, errors, requests


async def no_page_overflow(page):
    return await page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


async def seed_browser_run(client, pool):
    manifest, run_id = await create_run(pool)
    alice, bob = manifest["participants"]
    cluster = manifest["cluster_id"]
    await move_clock(pool, run_id, 3 * HOUR)
    async with pool.connection() as conn:
        await clear_queue(conn, run_id)
        await set_process(conn, run_id, "owner-1", "actor", 4 * HOUR, "busy")
        await set_process(conn, run_id, "owner-1", "agent", 3 * HOUR, "waiting")
        await set_process(conn, run_id, "owner-2", "actor", 3 * HOUR, "idle")
        await set_process(conn, run_id, "owner-2", "agent", 2 * HOUR, "dead")
        walk = await add_action(
            conn, run_id, "owner-1", "actor", "Walk <b>the dog</b>", 2 * HOUR, 7200, status="active"
        )
        await add_draw(conn, run_id, walk, "extreme", 2 * HOUR + 600, u=0.01, v=0.1)
        lunch = await add_action(conn, run_id, "owner-2", "actor", "Lunch", PAGE + 300, 1800)
        await add_draw(conn, run_id, lunch, "mild", PAGE + 900)
        await add_action(conn, run_id, "owner-1", "director", "Move the bus", PAGE, 60)
        await add_action(conn, run_id, "owner-1", "agent", "search", PAGE + 100, 30)
        for seq in range(3):
            await add_job(conn, run_id, "owner-1", "actor", 1, seq, PAGE + seq * 600)
            await add_job(conn, run_id, "owner-1", "agent", 1, seq + 10, PAGE + seq * 600 + 30)
        await save_message(conn, run_id, "owner-1", "owner", "Please look <b>now</b>", uuid4(), 1)
        await save_message(conn, run_id, "owner-1", "agent", "On it.", uuid4(), 1)
        await emit(
            conn,
            run_id,
            "assistant_tool_call",
            1,
            {"tool": "web_search", "arguments": {"query": "bus"}, "result": {"ok": True}},
            "browser-tool",
            "owner-1",
        )
        await emit(conn, run_id, "resource_updated", 1, {"resource_id": "r1"}, "rec", "owner-1")
        await conn.execute(
            """INSERT INTO web_sites(run_id,site_id,name,kind,spec,source,search_doc,sim_s)
            VALUES(%s,'agentcare','AgentCare','portal','{}','plot',to_tsvector('x'),0)""",
            (run_id,),
        )
        await schedule(
            conn, run_id, 5 * HOUR, "beat", payload={"cue": "Rain starts."}, key="beat-1"
        )
    await post(client, cluster, alice, "Open note", "<img src=x onerror=window.hacked=1>")
    secret = await direct(client, cluster, alice, [bob["owner_id"]], "Quiet word", "Only us two")
    await reply(client, cluster, bob, secret["id"], "Understood")
    return manifest, run_id


async def ready_page(page, heading):
    await page.wait_for_function(
        """expected => document.querySelector("#run-content").getAttribute("aria-busy") === "false"
        && document.querySelector("#turn-heading").textContent === expected""",
        arg=heading,
    )


async def test_browser_clock_panel_board_and_refusal_on_a_clocked_run(client, pool):
    driver, browser = await open_browser()
    try:
        _, run_id = await seed_browser_run(client, pool)
        page, errors, requests = await browser_page(browser, client)
        await page.goto(f"http://portal.test/console?run={run_id}&turn=1")
        await ready_page(page, "Mon 08:00")
        # The story time replaces Turn NN, and the turn parameter still opens the page.
        assert await page.locator("#turn-word").text_content() == "PAGE"
        assert "turn=1" in page.url
        assert "Turn" not in await page.locator("#turn-heading").text_content()
        await page.wait_for_selector(".clock-lane .clock-track")
        assert await page.locator("#clock-now").text_content() == "Mon 09:00"
        assert await page.locator(".clock-lanes .clock-lane").count() == 5
        assert await page.locator(".clock-track .clock-block.actor").count() == 2
        assert await page.locator(".clock-track .clock-block.director").count() == 1
        mild = page.locator(".clock-pert.mild")
        extreme = page.locator(".clock-pert.extreme")
        assert await mild.count() == 1 and await extreme.count() == 1
        colours = await page.evaluate(
            """[".clock-pert.mild", ".clock-pert.extreme"].map(name =>
            getComputedStyle(document.querySelector(name)).backgroundColor)"""
        )
        assert colours[0] != colours[1]
        link = page.locator("#clock-sites a")
        assert await link.text_content() == "AgentCare"
        assert await link.get_attribute("href") == f"/sim/{run_id}/agentcare/"
        assert await link.get_attribute("rel") == "noopener noreferrer"

        # Each record kind is on the page, in story order, with a story time.
        assert (await page.locator("#log-list .log-time").first.text_content()).startswith("Mon")
        await page.locator('[data-channel="jobs"]').click()
        assert await page.locator("#log-list .log-row").count() == 6
        await page.locator('[data-channel="actions"]').click()
        assert await page.locator("#log-list .log-row").count() >= 2
        await page.locator('[data-channel="records"]').click()
        assert await page.locator("#log-list .log-row").count() == 1
        await page.locator('[data-channel="tools"]').click()
        assert await page.locator("#log-list .log-row").count() == 1
        await page.locator('[data-channel="board"]').click()
        assert await page.locator("#log-list .log-row").count() == 3
        assert "Direct" in await page.locator("#log-list").text_content()
        await page.locator('[data-channel="all"]').click()
        times = await page.evaluate(
            """[...document.querySelectorAll("#log-list .log-time")].map(n => n.textContent)"""
        )
        assert all(text.startswith("Mon") for text in times)

        # The first-match inspector is gone: every Actor and Agent job can be selected.
        persona = page.locator(".job-picker button")
        assert await persona.count() == 3
        await persona.nth(2).click()
        assert "cue 2" in await page.locator("#inspector-body").text_content()
        await page.locator('[data-inspect="agent"]').click()
        assert await page.locator(".job-picker button").count() == 3
        await page.locator(".job-picker button").nth(1).click()
        assert "agent output 11" in await page.locator("#inspector-body").text_content()

        # Selecting a point on a lane opens that page.
        await page.locator(".clock-track").nth(1).click(position={"x": 1, "y": 9})
        await ready_page(page, "Mon 06:00")
        assert "turn=0" in page.url

        # A perturbation mark opens its page and its saved draw.
        await extreme.click()
        await ready_page(page, "Mon 08:00")
        await page.wait_for_selector("#detail-dialog[open]")
        assert '"extreme"' in await page.locator("#detail-content").text_content()
        await page.locator("#close-detail").click()

        # The Board tab names who took part. Text stays text.
        await page.locator('[data-clock-tab="board"]').click()
        await page.wait_for_selector(".board-thread")
        assert await page.locator(".board-thread").count() == 2
        direct_thread = page.locator(".board-thread.is-direct")
        assert "DIRECT" in await direct_thread.text_content()
        assert "Between:" in await direct_thread.text_content()
        people = await direct_thread.locator(".board-people").text_content()
        assert "Agent" in people or "assistant" in people.lower() or "," in people
        assert "Understood" in await direct_thread.text_content()
        public = page.locator(".board-thread:not(.is-direct) .board-body")
        assert await public.text_content() == "<img src=x onerror=window.hacked=1>"
        assert await page.evaluate("window.hacked") is None
        assert await page.locator(".board-body img").count() == 0
        await page.locator('[data-board-filter="direct"]').click()
        assert await page.locator(".board-thread").count() == 1

        await page.locator('[data-clock-tab="queue"]').click()
        assert "Rain starts." in await page.locator("#clock-queue").text_content()
        await page.locator('[data-clock-tab="perturbations"]').click()
        assert await page.locator("#clock-perturbations .log-row").count() == 2
        await page.locator('[data-clock-tab="week"]').click()

        # Hiding the clock leaves the replay, and the language switch keeps the panel.
        await page.locator("#clock-collapse").click()
        assert await page.locator("#clock-body").is_hidden()
        assert await page.locator("#clock-collapse").text_content() == "Show clock"
        await page.locator("#clock-collapse").click()
        assert await page.locator(".clock-lanes").is_visible()
        await page.locator("#language-toggle").click()
        await page.locator("#language-toggle").click()
        assert await page.locator(".clock-lanes .clock-lane").count() == 5

        # Step and Continue are disabled with a note. Pause stays with its own rule.
        await page.locator("#mode-live").click()
        await page.wait_for_function(
            """document.querySelector("#execution-reason").textContent.includes("run script")"""
        )
        assert await page.locator("#live-step").is_disabled()
        assert await page.locator("#live-continue").is_disabled()
        assert await page.locator("#send-prompt").is_disabled()
        assert await page.locator("#branch-turn").is_disabled()
        await page.locator("#mode-replay").click()

        assert await no_page_overflow(page)
        screenshots = os.environ.get("AGENTRPG_TEST_SCREENSHOTS")
        if screenshots:
            await page.screenshot(path=f"{screenshots}/clock-desktop.png", full_page=True)

        assert errors == []
        assert not [item for item in requests if item[0] != "GET"]
        await page.context.close()
    finally:
        await browser.close()
        await driver.stop()


@pytest.mark.parametrize("width,height", [(390, 844), (320, 640), (800, 1000)])
async def test_browser_clock_panel_fits_narrow_screens(client, pool, width, height):
    driver, browser = await open_browser()
    try:
        _, run_id = await seed_browser_run(client, pool)
        page, errors, _ = await browser_page(browser, client, width=width, height=height)
        await page.goto(f"http://portal.test/console?run={run_id}&turn=1")
        await ready_page(page, "Mon 08:00")
        await page.wait_for_selector(".clock-lane .clock-track")
        assert await no_page_overflow(page)
        await page.locator('[data-clock-tab="board"]').click()
        await page.wait_for_selector(".board-thread")
        assert await no_page_overflow(page)
        await page.locator('[data-clock-tab="perturbations"]').click()
        assert await no_page_overflow(page)
        await page.locator('[data-clock-tab="queue"]').click()
        assert await no_page_overflow(page)
        await page.locator('[data-clock-tab="week"]').click()
        screenshots = os.environ.get("AGENTRPG_TEST_SCREENSHOTS")
        if screenshots:
            await page.screenshot(path=f"{screenshots}/clock-{width}.png", full_page=True)
        assert errors == []
        await page.context.close()
    finally:
        await browser.close()
        await driver.stop()


async def test_browser_fifteen_owners_and_a_full_week_fit_the_panel(client, pool):
    driver, browser = await open_browser()
    try:
        _, run_id = await create_run(pool, 15)
        now = 5 * 24 * HOUR
        await move_clock(pool, run_id, now)
        async with pool.connection() as conn:
            for owner_index in range(1, 16):
                owner = f"owner-{owner_index}"
                await set_process(conn, run_id, owner, "actor", now + HOUR, "busy")
                await set_process(conn, run_id, owner, "agent", now, "waiting", now + HOUR)
                for step in range(40):
                    start = step * 3 * HOUR + owner_index * 60
                    if start >= now:
                        break
                    action = await add_action(
                        conn, run_id, owner, "actor", f"Errand {step} of {owner}", start, 2 * HOUR
                    )
                    if step % 9 == owner_index % 9:
                        outcome = "extreme" if step % 2 else "mild"
                        await add_draw(conn, run_id, action, outcome, start + HOUR)
                    for call in range(6):
                        await add_action(
                            conn, run_id, owner, "agent", f"tool {call}", start + call * 40, 30
                        )
            for seq in range(12):
                await add_job(conn, run_id, "owner-3", "agent", 60, seq, now - 500 + seq)
        # The panel follows a running clock without losing its scroll position.
        follower, errors, _ = await browser_page(browser, client)
        await follower.goto(f"http://portal.test/console?run={run_id}&turn=60")
        await ready_page(follower, "Sat 06:00")
        await follower.wait_for_selector(".clock-lane .clock-track")
        assert await follower.locator("#clock-now").text_content() == "Sat 06:00"
        await follower.evaluate('document.querySelector(".clock-lanes").scrollTop = 120')
        await move_clock(pool, run_id, now + 600)
        await follower.wait_for_function(
            """document.querySelector("#clock-now").textContent === "Sat 06:10" """, timeout=15000
        )
        assert await follower.evaluate('document.querySelector(".clock-lanes").scrollTop') == 120
        assert errors == []
        await follower.context.close()

        for width, height in ((1440, 900), (390, 844)):
            page, errors, _ = await browser_page(browser, client, width=width, height=height)
            await page.goto(f"http://portal.test/console?run={run_id}&turn=60")
            await ready_page(page, "Sat 06:00")
            await page.wait_for_selector(".clock-lane .clock-track")
            assert await page.locator(".clock-lanes .clock-lane").count() == 30
            assert await page.locator(".clock-pert").count() > 10
            assert await page.locator(".turn-marker").count() == 61
            assert await no_page_overflow(page)
            await page.locator('.cast-button[data-owner="owner-3"]').click()
            await page.locator('[data-inspect="agent"]').click()
            assert await page.locator(".job-picker button").count() == 12
            screenshots = os.environ.get("AGENTRPG_TEST_SCREENSHOTS")
            if screenshots:
                await page.screenshot(path=f"{screenshots}/clock-15-{width}.png")
            assert errors == []
            await page.context.close()
    finally:
        await browser.close()
        await driver.stop()


async def test_browser_reads_every_page_of_a_real_engine_run(app, client, test_database_url):
    driver, browser = await open_browser()
    try:
        run, *_ = await engine_control().run_control(
            app, test_database_url, latency_seed=0, parallel=8, max_latency=0
        )
        assert run["status"] == "completed", run["result"]
        page, errors, _ = await browser_page(browser, client)
        await page.goto(f"http://portal.test/console?run={run['id']}&turn=0")
        await ready_page(page, "Mon 06:00")
        await page.wait_for_selector(".clock-lane .clock-track")
        assert await page.locator(".clock-lanes .clock-lane").count() == 6
        assert await page.locator(".clock-track .clock-block").count() > 0
        # This control has one-hour pages. The labels follow the saved clock section.
        assert await page.locator("#turn-note").text_content() == "Replay · one-hour pages"
        assert "6 one-hour pages" in await page.locator("#run-summary").text_content()
        # A real Actor view holds a clock object. It must read as text, not as an object name.
        persona = await page.locator("#inspector-body").text_content()
        assert "[object Object]" not in persona and "now:" in persona
        rows = 0
        for window, heading in enumerate(
            ("Mon 06:00", "Mon 07:00", "Mon 08:00", "Mon 09:00", "Mon 10:00", "Mon 11:00")
        ):
            await page.locator(f'[data-turn="{window}"]').click()
            await ready_page(page, heading)
            assert f"turn={window}" in page.url
            rows += await page.locator("#log-list .log-row").count()
            times = await page.evaluate(
                """[...document.querySelectorAll("#log-list .log-time")].map(n => n.textContent)"""
            )
            assert all(text.startswith("Mon") for text in times)
        assert rows > 0
        await page.locator('[data-channel="tools"]').click()
        await page.locator("[data-turn='0']").click()
        await ready_page(page, "Mon 06:00")
        assert await no_page_overflow(page)
        screenshots = os.environ.get("AGENTRPG_TEST_SCREENSHOTS")
        if screenshots:
            await page.screenshot(path=f"{screenshots}/clock-engine.png", full_page=True)
        assert errors == []
        await page.context.close()
    finally:
        await browser.close()
        await driver.stop()


@pytest.mark.skipif(not WEEK_SCENARIO.exists(), reason="The week scenario file is not present.")
async def test_browser_shows_the_week_scenario_before_it_starts(client, pool):
    driver, browser = await open_browser()
    try:
        spec = ScenarioSpec.model_validate(json.loads(WEEK_SCENARIO.read_text()))
        manifest = await Director(pool).create_run(spec)
        page, errors, _ = await browser_page(browser, client)
        await page.goto(f"http://portal.test/console?run={manifest['run_id']}&turn=0")
        await ready_page(page, "Mon 06:00")
        await page.wait_for_function(
            """document.querySelector("#clock-week").textContent.includes("has not started")"""
        )
        assert await page.locator("#clock-sites a").count() == len(spec.clock.sites)
        stats = await page.locator("#clock-stats").text_content()
        assert spec.clock.lifecycle[0].bill_id in stats
        await page.locator('[data-clock-tab="queue"]').click()
        assert await page.locator("#clock-queue .log-row").count() == 20
        await page.locator('[data-clock-tab="board"]').click()
        await page.wait_for_selector(".board-thread")
        assert await page.locator(".board-thread").count() == len(spec.clock.seed_board)
        assert "before the start" in await page.locator(".board-time").first.text_content()
        screenshots = os.environ.get("AGENTRPG_TEST_SCREENSHOTS")
        if screenshots:
            await page.screenshot(path=f"{screenshots}/clock-week-board.png")
        assert errors == []
        await page.context.close()
    finally:
        await browser.close()
        await driver.stop()


async def test_browser_empty_and_error_states_read_sensibly(client, pool):
    driver, browser = await open_browser()
    try:
        _, run_id = await create_run(pool)
        async with pool.connection() as conn:
            await clear_queue(conn, run_id)
        page, errors, _ = await browser_page(browser, client)
        await page.goto(f"http://portal.test/console?run={run_id}&turn=0")
        await ready_page(page, "Mon 06:00")
        await page.wait_for_function(
            """document.querySelector("#clock-week").textContent.includes("has not started")"""
        )
        assert "No sites have been built yet." in await page.locator("#clock-sites").text_content()
        await page.locator('[data-clock-tab="board"]').click()
        await page.wait_for_function(
            """document.querySelector("#clock-board").textContent.includes("No board posts")"""
        )
        await page.locator('[data-board-filter="direct"]').click()
        assert "No direct messages" in await page.locator("#clock-board").text_content()
        await page.locator('[data-clock-tab="queue"]').click()
        assert "Nothing is queued." in await page.locator("#clock-queue").text_content()
        await page.locator('[data-clock-tab="perturbations"]').click()
        assert "Nothing has interfered" in await page.locator("#clock-perturbations").text_content()
        assert errors == []
        await page.context.close()

        # A failed clock read leaves the replay usable and says what happened.
        failing, errors, _ = await browser_page(
            browser, client, failures={"/clock": 500, "/board": 500}
        )
        await failing.goto(f"http://portal.test/console?run={run_id}&turn=0")
        await ready_page(failing, "Mon 06:00")
        await failing.wait_for_function(
            """document.querySelector("#clock-week").textContent.includes("could not load")"""
        )
        assert await failing.locator("#error-banner").is_hidden()
        await failing.locator('[data-clock-tab="board"]').click()
        await failing.wait_for_function(
            """document.querySelector("#clock-board").textContent.includes("could not load")"""
        )
        assert errors == []
        await failing.context.close()
    finally:
        await browser.close()
        await driver.stop()


async def test_browser_window_run_looks_as_before(client, pool):
    driver, browser = await open_browser()
    try:
        plain = await Director(pool).create_run(vehicle_scenario(2))
        run_id = plain["run_id"]
        async with pool.connection() as conn:
            await save_message(conn, run_id, "owner-1", "owner", "Hello", uuid4(), 0)
            await add_job(conn, UUID(run_id), "owner-1", "actor", 0, 0, None)
        page, errors, requests = await browser_page(browser, client)
        await page.goto(f"http://portal.test/console?run={run_id}&turn=0")
        await ready_page(page, "Turn 01")
        assert await page.locator("#clock-panel").is_hidden()
        assert await page.locator("#turn-word").text_content() == "TURN"
        assert await page.locator("#turn-note").text_content() == "Replay · saved turns"
        assert await page.locator("#turn-now").text_content() == "01"
        for channel in ("jobs", "actions", "records"):
            assert await page.locator(f'[data-channel="{channel}"]').is_hidden()
        assert await page.evaluate('document.body.classList.contains("is-clocked")') is False
        assert await page.evaluate("getComputedStyle(document.body).overflowY") == "hidden"
        assert await page.locator(".job-picker").count() == 0
        assert not [path for _, path in requests if "/clock" in path or "/board" in path]
        assert errors == []
        await page.context.close()
    finally:
        await browser.close()
        await driver.stop()


def test_console_script_never_builds_markup_from_text():
    code = (ROOT / "src/agentrpg/static/run-console.js").read_text()
    assert "innerHTML" not in code and "insertAdjacentHTML" not in code
