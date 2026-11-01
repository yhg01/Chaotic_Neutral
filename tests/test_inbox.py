"""Private mail integration tests against real PostgreSQL."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from agentrpg.director import Director, row, rows
from agentrpg.inbox import InboxError, InboxService
from agentrpg.inbox_api import router
from agentrpg.mail_schema import InitialMailSpec
from agentrpg.scenario_schema import vehicle_scenario


def bearer(participant):
    return {"Authorization": f"Bearer {participant['token']}"}


@pytest.fixture
async def mail_client(app):
    if not any(getattr(route, "path", "").endswith("/inbox") for route in app.routes):
        app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def start(pool, count=2):
    director = Director(pool)
    run = await director.create_run(vehicle_scenario(count))
    await director.control(run["run_id"], "start")
    return run


def mail_payload(recipient="owner-2", **overrides):
    return {
        "request_id": str(uuid4()),
        "recipient_owner_id": recipient,
        "subject": "Private proposal",
        "body": "Please confirm the meeting.",
        **overrides,
    }


@pytest.mark.parametrize("count", [2, 15])
async def test_concurrent_mail_private_receipts_and_replies(pool, mail_client, count):
    run = await start(pool, count)
    path = f"/api/v1/runs/{run['run_id']}/inbox"
    participants = run["participants"]
    responses = await asyncio.gather(
        *(
            mail_client.post(
                path,
                headers=bearer(sender),
                json=mail_payload(
                    participants[(index + 1) % count]["owner_id"], body=f"Private {index}"
                ),
            )
            for index, sender in enumerate(participants)
        )
    )
    assert all(response.status_code == 201 for response in responses)
    inboxes = await asyncio.gather(
        *(mail_client.get(path, headers=bearer(participant)) for participant in participants)
    )
    for index, participant in enumerate(participants):
        headers = bearer(participant)
        inbox = inboxes[index].json()
        assert len(inbox["items"]) == 1
        received = inbox["items"][0]
        assert received["body"] == f"Private {(index - 1) % count}"
        assert received["sender_source"] == "agent"
        assert received["read_at"] is None
        read = await mail_client.get(f"{path}/{received['id']}", headers=headers)
        assert read.status_code == 200
        assert read.json()["read_at"] is None
        receipt = await mail_client.post(
            path + "/receipts",
            headers=headers,
            json={"message_ids": [received["id"]]},
        )
        assert receipt.status_code == 200
        unread = (
            await mail_client.get(path, headers=headers, params={"unread_only": True})
        ).json()["items"]
        assert received["id"] not in {item["id"] for item in unread}
        sent = (await mail_client.get(path, headers=headers, params={"mailbox": "sent"})).json()
        assert len(sent["items"]) == 1
        reply = await mail_client.post(
            path,
            headers=headers,
            json=mail_payload(
                received["sender_owner_id"],
                in_reply_to=received["id"],
                subject="Re: proposal",
            ),
        )
        assert reply.status_code == 201
    events = [
        event
        for event in await Director(pool).trace(run["run_id"])
        if event["kind"] == "inbox_received"
    ]
    assert len(events) == count * 2
    assert all(event["owner_id"] == event["payload"]["recipient_owner_id"] for event in events)
    assert all("body" not in event["payload"] for event in events)
    receipt_view = await mail_client.get(
        f"{path}/{responses[0].json()['id']}",
        headers=bearer(participants[0]),
    )
    assert receipt_view.json()["read_at"] is not None


async def test_mail_privacy_and_authentication(pool, mail_client, client):
    run = await start(pool, 3)
    other = await start(pool, 3)
    path = f"/api/v1/runs/{run['run_id']}/inbox"
    one, two, three = run["participants"]
    message = (await mail_client.post(path, headers=bearer(one), json=mail_payload())).json()
    assert (await mail_client.get(path)).status_code == 401
    assert (await client.get(path)).status_code == 403
    assert (
        await mail_client.get(path, headers=bearer(other["participants"][0]))
    ).status_code == 404
    assert (
        await mail_client.get(f"{path}/{message['id']}", headers=bearer(three))
    ).status_code == 404
    assert (await mail_client.get(path, headers=bearer(three))).json()["items"] == []
    for forbidden in ({"sender_owner_id": "owner-2"}, {"source": "actor"}, {"owner_id": "owner-2"}):
        assert (
            await mail_client.post(path, headers=bearer(one), json=mail_payload(**forbidden))
        ).status_code == 422
    for participant in (one, three):
        assert (
            await mail_client.post(
                path + "/receipts",
                headers=bearer(participant),
                json={"message_ids": [message["id"]]},
            )
        ).status_code == 404
    assert (
        await mail_client.post(
            path,
            headers=bearer(three),
            json=mail_payload(in_reply_to=message["id"]),
        )
    ).status_code == 404
    assert (
        await mail_client.post(
            path,
            headers=bearer(two),
            json=mail_payload("owner-3", in_reply_to=message["id"]),
        )
    ).status_code == 422
    assert (
        await mail_client.post(
            path,
            headers=bearer(one),
            json=mail_payload("no-such-owner"),
        )
    ).status_code == 404


async def test_mail_concurrent_retry_conflict_and_terminal_replay(pool, mail_client):
    run = await start(pool)
    path = f"/api/v1/runs/{run['run_id']}/inbox"
    headers = bearer(run["participants"][0])
    payload = mail_payload()
    responses = await asyncio.gather(
        *(mail_client.post(path, headers=headers, json=payload) for _ in range(15))
    )
    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["id"] for response in responses}) == 1
    await Director(pool).control(run["run_id"], "pause")
    assert (await mail_client.post(path, headers=headers, json=payload)).json() == responses[
        0
    ].json()
    assert (
        await mail_client.post(path, headers=headers, json={**payload, "body": "different"})
    ).status_code == 409
    assert (await mail_client.post(path, headers=headers, json=mail_payload())).status_code == 409
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET status='completed' WHERE id=%s", (run["run_id"],)
        )
    assert (await mail_client.post(path, headers=headers, json=payload)).json() == responses[
        0
    ].json()
    events = [
        event
        for event in await Director(pool).trace(run["run_id"])
        if event["kind"] == "inbox_received"
    ]
    assert len(events) == 1


async def test_mail_late_commit_is_not_lost_from_unread_queue(pool):
    run = await start(pool, 3)
    inbox = InboxService(pool)
    async with pool.connection() as delayed:
        async with delayed.transaction():
            first = await inbox.send(
                run["run_id"], "owner-1", "owner-3", "Slow", "First", uuid4(), connection=delayed
            )
            later = await inbox.send(run["run_id"], "owner-2", "owner-3", "Fast", "Second", uuid4())
            observed = await inbox.list_messages(run["run_id"], "owner-3", unread_only=True)
            assert [item["id"] for item in observed["items"]] == [later["id"]]
            await inbox.acknowledge(run["run_id"], "owner-3", [later["id"]])
    observed = await inbox.list_messages(run["run_id"], "owner-3", unread_only=True)
    assert [item["id"] for item in observed["items"]] == [first["id"]]
    await inbox.acknowledge(run["run_id"], "owner-3", [first["id"], first["id"]])
    assert (await inbox.list_messages(run["run_id"], "owner-3", unread_only=True))["items"] == []


async def test_mail_seed_actor_transaction_and_rollback(pool):
    director, inbox = Director(pool), InboxService(pool)
    created = await director.create_run(vehicle_scenario())
    seed = InitialMailSpec(
        id="notice", recipient_owner_id="owner-1", subject="Notice", body="Director notice"
    )
    async with pool.connection() as conn:
        await inbox.initialize(created["run_id"], [seed], connection=conn)
    assert (await inbox.actor_view(created["run_id"], "owner-1"))["items"][0][
        "sender_source"
    ] == "director"
    assert (await inbox.actor_view(created["run_id"], "owner-2"))["items"] == []
    await director.control(created["run_id"], "start")
    with pytest.raises(RuntimeError):
        async with pool.connection() as conn:
            async with conn.transaction():
                await inbox.send(
                    created["run_id"],
                    "owner-1",
                    "owner-2",
                    "Actor note",
                    "Hello",
                    uuid4(),
                    source="actor",
                    connection=conn,
                )
                raise RuntimeError("Roll back the whole Actor turn")
    assert (await inbox.actor_view(created["run_id"], "owner-2"))["items"] == []
    async with pool.connection() as conn:
        sent = await inbox.send(
            created["run_id"],
            "owner-1",
            "owner-2",
            "Actor note",
            "Hello",
            uuid4(),
            source="actor",
            connection=conn,
        )
    assert sent["sender_source"] == "actor"
    assert (
        len([e for e in await director.trace(created["run_id"]) if e["kind"] == "inbox_received"])
        == 2
    )


async def test_mail_history_search_cursor_and_receipt_atomicity(pool):
    run = await start(pool, 3)
    inbox = InboxService(pool)
    messages = []
    for index in range(5):
        messages.append(
            await inbox.send(
                run["run_id"],
                "owner-1",
                "owner-2",
                f"Number {index}",
                "100%_done" if index == 1 else "ordinary",
                uuid4(),
            )
        )
    first = await inbox.list_messages(run["run_id"], "owner-2", limit=2)
    second = await inbox.list_messages(
        run["run_id"], "owner-2", limit=2, cursor=first["next_cursor"]
    )
    third = await inbox.list_messages(
        run["run_id"], "owner-2", limit=2, cursor=second["next_cursor"]
    )
    ids = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert ids == [item["id"] for item in reversed(messages)]
    assert third["next_cursor"] is None
    assert len((await inbox.list_messages(run["run_id"], "owner-2", query="%_"))["items"]) == 1
    with pytest.raises(InboxError, match="cursor"):
        await inbox.list_messages(run["run_id"], "owner-3", cursor=first["next_cursor"])
    with pytest.raises(InboxError, match="cursor"):
        await inbox.list_messages(
            run["run_id"], "owner-2", query="different", cursor=first["next_cursor"]
        )
    with pytest.raises(InboxError):
        await inbox.acknowledge(run["run_id"], "owner-2", [messages[0]["id"], uuid4()])
    assert (
        len((await inbox.list_messages(run["run_id"], "owner-2", unread_only=True))["items"]) == 5
    )
    async with pool.connection() as conn:
        receipts = await rows(conn, "SELECT * FROM inbox_receipts")
    assert receipts == []


@pytest.mark.parametrize(
    "changes",
    [
        {"body": ""},
        {"body": "\x00"},
        {"body": "\ud800"},
        {"body": "a" * 12001},
        {"subject": "a" * 241},
        {"recipient_owner_id": "external@example.com"},
        {"request_id": "bad"},
        {"in_reply_to": "bad"},
    ],
)
async def test_mail_input_validation(pool, mail_client, changes):
    run = await start(pool)
    response = await mail_client.post(
        f"/api/v1/runs/{run['run_id']}/inbox",
        headers={**bearer(run["participants"][0]), "Content-Type": "application/json"},
        content=json.dumps(mail_payload(**changes)),
    )
    assert response.status_code == 422


async def test_mail_pause_fences_inflight_write(pool):
    run = await start(pool)
    inbox = InboxService(pool)
    async with pool.connection() as conn:
        async with conn.transaction():
            await inbox.send(
                run["run_id"], "owner-1", "owner-2", "First", "Body", uuid4(), connection=conn
            )
            pause = asyncio.create_task(Director(pool).control(run["run_id"], "pause"))
            await asyncio.sleep(0.03)
            assert not pause.done()
    assert (await pause)["status"] == "paused"
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM inbox_messages"))["n"] == 1


async def test_actor_mail_view_bounds_preserve_originals_and_history_cursor(pool):
    run = await start(pool)
    inbox = InboxService(pool)
    for index in range(21):
        await inbox.send(
            run["run_id"],
            "owner-1",
            "owner-2",
            f"Long mail {index}",
            "🌦" * 12000,
            uuid4(),
        )
    view = await inbox.actor_view(run["run_id"], "owner-2")
    assert len(view["items"]) == 20
    assert len(json.dumps(view, default=str)) <= 24000
    assert all(len(item["body"]) <= 4000 and item["body_truncated"] for item in view["items"])
    full = await inbox.list_messages(run["run_id"], "owner-2")
    assert view["next_cursor"] == full["next_cursor"]
    assert [item["id"] for item in view["items"]] == [item["id"] for item in full["items"]]
    assert all(len(item["body"]) == 12000 for item in full["items"])
    assert (
        len((await inbox.read(run["run_id"], "owner-2", view["items"][0]["id"]))["body"]) == 12000
    )
    short = await inbox.send(run["run_id"], "owner-1", "owner-2", "Short", "Whole body", uuid4())
    view = await inbox.actor_view(run["run_id"], "owner-2", limit=1)
    assert view["items"][0]["id"] == short["id"]
    assert not view["items"][0]["body_truncated"]
    assert view["items"][0]["body"] == "Whole body"
