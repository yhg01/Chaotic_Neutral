"""Concurrency and durability checks against the isolated PostgreSQL test schema."""

import asyncio
from uuid import uuid4

import httpx
import pytest

from agentrpg.main import create_app

pytestmark = pytest.mark.asyncio


def agent_headers(agent):
    return {"Authorization": f"Bearer {agent['token']}"}


def thread_payload(title="Concurrent thread", body="A complete message."):
    return {"title": title, "body": body, "request_id": str(uuid4())}


async def test_fifteen_agents_post_reply_and_read_concurrently(client, provision):
    cluster, agents = await provision(count=15)
    base = f"/api/v1/clusters/{cluster['id']}"
    root = await client.post(
        f"{base}/threads", json=thread_payload(), headers=agent_headers(agents[0])
    )
    assert root.status_code == 201, root.text
    root_id = root.json()["id"]
    start = asyncio.Event()

    async def use_board(index, agent):
        headers = agent_headers(agent)
        await start.wait()
        posted, reading = await asyncio.gather(
            client.post(
                f"{base}/threads",
                json=thread_payload(title=f"Agent {index} thread"),
                headers=headers,
            ),
            client.get(f"{base}/updates", headers=headers),
        )
        assert posted.status_code == 201, posted.text
        assert reading.status_code == 200, reading.text
        assert root_id in {message["id"] for message in reading.json()["items"]}
        replied, history = await asyncio.gather(
            client.post(
                f"{base}/threads/{root_id}/replies",
                json={"body": f"Reply from Agent {index}", "request_id": str(uuid4())},
                headers=headers,
            ),
            client.get(f"{base}/threads", headers=headers),
        )
        assert replied.status_code == 201, replied.text
        assert history.status_code == 200, history.text
        assert posted.json()["author_id"] == agent["agent"]["id"]
        assert replied.json()["author_id"] == agent["agent"]["id"]
        return posted.json()["id"], replied.json()["id"]

    tasks = [
        asyncio.create_task(use_board(index, agent)) for index, agent in enumerate(agents, start=1)
    ]
    start.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    expected = {root_id, *(message_id for pair in results for message_id in pair)}
    assert len(expected) == 31

    detail = await client.get(f"{base}/threads/{root_id}?limit=100")
    assert detail.status_code == 200, detail.text
    assert {reply["id"] for reply in detail.json()["replies"]} == {pair[1] for pair in results}

    async def read_all(agent):
        response = await client.get(f"{base}/updates?limit=100", headers=agent_headers(agent))
        assert response.status_code == 200, response.text
        assert {message["id"] for message in response.json()["items"]} == expected
        assert response.json()["has_more"] is False

    await asyncio.gather(*(read_all(agent) for agent in agents))


async def test_thirty_registrations_admit_exactly_fifteen(client, provision):
    cluster, _ = await provision(count=0)
    path = f"/api/v1/clusters/{cluster['id']}/agents"
    responses = await asyncio.wait_for(
        asyncio.gather(
            *(client.post(path, json={"name": f"Agent {index}"}) for index in range(30))
        ),
        timeout=20,
    )
    assert [response.status_code for response in responses].count(201) == 15
    assert [response.status_code for response in responses].count(409) == 15
    accepted = [response.json() for response in responses if response.status_code == 201]
    assert len({item["agent"]["id"] for item in accepted}) == 15
    assert len({item["token"] for item in accepted}) == 15
    membership = await client.get(path)
    assert membership.status_code == 200, membership.text
    assert len(membership.json()["items"]) == 15


async def test_concurrent_retries_create_one_message(client, provision):
    cluster, agents = await provision(count=1)
    base = f"/api/v1/clusters/{cluster['id']}"
    payload = thread_payload()
    headers = agent_headers(agents[0])
    responses = await asyncio.wait_for(
        asyncio.gather(
            *(client.post(f"{base}/threads", json=payload, headers=headers) for _ in range(15))
        ),
        timeout=20,
    )
    assert [response.status_code for response in responses].count(201) == 1
    assert [response.status_code for response in responses].count(200) == 14
    assert len({response.json()["id"] for response in responses}) == 1
    for response in responses:
        if response.status_code == 200:
            assert response.headers.get("Idempotent-Replay") == "true"
    board = await client.get(f"{base}/threads")
    assert board.status_code == 200, board.text
    assert len(board.json()["items"]) == 1


async def test_concurrent_changed_payload_returns_conflict(client, provision):
    cluster, agents = await provision(count=1)
    path = f"/api/v1/clusters/{cluster['id']}/threads"
    headers = agent_headers(agents[0])
    first = thread_payload(body="First candidate")
    second = {**first, "body": "Second candidate"}
    responses = await asyncio.gather(
        client.post(path, json=first, headers=headers),
        client.post(path, json=second, headers=headers),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response.json() for response in responses if response.status_code == 201)
    retry = await client.post(path, json={**first, "body": winner["body"]}, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == winner["id"]
    other = second if winner["body"] == first["body"] else first
    conflict = await client.post(path, json=other, headers=headers)
    assert conflict.status_code == 409, conflict.text


async def test_uncommitted_api_write_does_not_block_other_writes_or_reads(
    client, provision, db_pool
):
    """Pause one real API transaction inside PostgreSQL, then complete another."""
    cluster, agents = await provision(count=2)
    base = f"/api/v1/clusters/{cluster['id']}"
    paused_payload = thread_payload(title="Paused transaction")
    lock_key = uuid4().int % (2**31 - 1)
    # These generated integer/UUID literals cannot contain SQL syntax. The trigger
    # exists only in this test's disposable schema, without application test hooks.
    async with db_pool.connection() as setup:
        await setup.execute(
            f"""
            CREATE FUNCTION hold_selected_message() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.request_id = '{paused_payload["request_id"]}'::uuid THEN
                    PERFORM pg_advisory_xact_lock({lock_key});
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        await setup.execute(
            "CREATE TRIGGER hold_selected_message BEFORE INSERT ON messages "
            "FOR EACH ROW EXECUTE FUNCTION hold_selected_message()"
        )

    async with db_pool.connection() as gate:
        await gate.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        pending = asyncio.create_task(
            client.post(f"{base}/threads", json=paused_payload, headers=agent_headers(agents[0]))
        )
        try:

            async def wait_for_database_block():
                while True:
                    cursor = await gate.execute(
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                        "AND objid = %s AND NOT granted",
                        (lock_key,),
                    )
                    if await cursor.fetchone():
                        return
                    assert not pending.done(), "The selected API write did not enter the trigger"
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_database_block(), timeout=5)
            completed = await asyncio.wait_for(
                client.post(
                    f"{base}/threads",
                    json=thread_payload(title="Independent transaction"),
                    headers=agent_headers(agents[1]),
                ),
                timeout=5,
            )
            assert completed.status_code == 201, completed.text
            reading = await asyncio.wait_for(client.get(f"{base}/threads"), timeout=5)
            assert reading.status_code == 200, reading.text
            assert {message["id"] for message in reading.json()["items"]} == {
                completed.json()["id"]
            }
            assert not pending.done(), "The paused transaction unexpectedly completed"
        finally:
            await gate.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            paused = await asyncio.wait_for(pending, timeout=5)
        assert paused.status_code == 201, paused.text


async def test_late_commit_is_delivered_after_newer_message_acknowledgement(
    client, provision, db_pool
):
    cluster, agents = await provision(count=2)
    base = f"/api/v1/clusters/{cluster['id']}"
    reader = agent_headers(agents[1])
    late_id = uuid4()
    async with db_pool.connection() as held:
        await held.execute(
            "INSERT INTO messages "
            "(id, cluster_id, author_id, title, body, request_id, payload_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                late_id,
                cluster["id"],
                agents[0]["agent"]["id"],
                "Earlier insert, later commit",
                "This message must remain available after the other message is acknowledged.",
                uuid4(),
                "0" * 64,
            ),
        )
        later = await asyncio.wait_for(
            client.post(
                f"{base}/threads",
                json=thread_payload(title="Later insert, earlier commit"),
                headers=agent_headers(agents[0]),
            ),
            timeout=5,
        )
        assert later.status_code == 201, later.text
        visible = await client.get(f"{base}/updates?limit=1", headers=reader)
        assert visible.status_code == 200, visible.text
        assert [message["id"] for message in visible.json()["items"]] == [later.json()["id"]]
        acknowledged = await client.post(
            f"{base}/acknowledgements",
            json={"message_ids": [later.json()["id"]]},
            headers=reader,
        )
        assert acknowledged.status_code == 200, acknowledged.text
        before_commit = await client.get(f"{base}/updates", headers=reader)
        assert before_commit.status_code == 200, before_commit.text
        assert before_commit.json()["items"] == []
    # Returning the connection commits the earlier transaction after the receipt.
    after_commit = await client.get(f"{base}/updates?limit=1", headers=reader)
    assert after_commit.status_code == 200, after_commit.text
    assert [message["id"] for message in after_commit.json()["items"]] == [str(late_id)]
    repeated_read = await client.get(f"{base}/updates?limit=1", headers=reader)
    assert repeated_read.json()["items"] == after_commit.json()["items"]


async def test_messages_and_receipts_survive_application_recreation(
    client, provision, db_pool, settings
):
    cluster, agents = await provision(count=2)
    base = f"/api/v1/clusters/{cluster['id']}"
    writer = agent_headers(agents[0])
    reader = agent_headers(agents[1])
    posted = await client.post(f"{base}/threads", json=thread_payload(), headers=writer)
    assert posted.status_code == 201, posted.text
    message = posted.json()
    acknowledged = await client.post(
        f"{base}/acknowledgements",
        json={"message_ids": [message["id"]]},
        headers=reader,
    )
    assert acknowledged.status_code == 200, acknowledged.text
    await db_pool.close()

    fresh_app = create_app(settings)
    async with fresh_app.router.lifespan_context(fresh_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fresh_app), base_url="http://test"
        ) as restarted:
            history = await restarted.get(f"{base}/threads/{message['id']}", headers=reader)
            assert history.status_code == 200, history.text
            assert history.json()["thread"] == message
            updates = await restarted.get(f"{base}/updates", headers=reader)
            assert updates.status_code == 200, updates.text
            assert updates.json()["items"] == []
            independent = await restarted.get(f"{base}/updates", headers=writer)
            assert independent.status_code == 200, independent.text
            assert [item["id"] for item in independent.json()["items"]] == [message["id"]]
