"""API contracts, access boundaries, validation, and durable acknowledgements."""

import asyncio
from uuid import uuid4

import httpx
import psycopg
import pytest

from agentrpg.main import create_app, digest
from agentrpg.migrate import migrate

pytestmark = pytest.mark.asyncio


def headers(agent):
    return {"Authorization": f"Bearer {agent['token']}"}


def thread(**changes):
    return {"title": "A subject", "body": "A message", "request_id": str(uuid4()), **changes}


async def test_credentials_and_provisioning(client, provision, db_pool):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ready"
    assert (await client.get("/api/v1/me")).json() == {"role": "admin"}
    for credential in ("", "Basic token", "Bearer unknown"):
        response = await client.get("/api/v1/me", headers={"Authorization": credential})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
    cluster, agents = await provision(count=1)
    me = await client.get("/api/v1/me", headers=headers(agents[0]))
    assert me.json() == {"role": "agent", "agent": agents[0]["agent"]}
    assert "token" not in me.text
    assert me.headers["cache-control"] == "no-store"
    async with db_pool.connection() as connection:
        stored = await (
            await connection.execute(
                "SELECT token_hash FROM agents WHERE id = %s", (agents[0]["agent"]["id"],)
            )
        ).fetchone()
    assert stored["token_hash"] == digest(agents[0]["token"])
    assert stored["token_hash"] != agents[0]["token"]
    duplicate_cluster = await client.post("/api/v1/clusters", json={"name": "Cluster"})
    assert duplicate_cluster.status_code == 409
    duplicate_agent = await client.post(
        f"/api/v1/clusters/{cluster['id']}/agents", json={"name": "Agent 1"}
    )
    assert duplicate_agent.status_code == 409
    clusters = await client.get("/api/v1/clusters", headers=headers(agents[0]))
    assert clusters.json()["items"][0]["agent_count"] == 1
    assert clusters.json()["items"][0]["capacity"] == 15


async def test_roles_and_cluster_boundaries(client, provision):
    first, agents = await provision(name="First")
    second, others = await provision(name="Second", count=1)
    first_base = f"/api/v1/clusters/{first['id']}"
    second_base = f"/api/v1/clusters/{second['id']}"
    agent = headers(agents[0])
    response = await client.post(
        f"{second_base}/threads", json=thread(), headers=headers(others[0])
    )
    foreign = response.json()
    assert (await client.get("/api/v1/clusters", headers=agent)).json()["items"][0]["id"] == first[
        "id"
    ]
    assert len((await client.get("/api/v1/clusters", headers=agent)).json()["items"]) == 1
    for suffix in ("agents", "threads", "updates", f"threads/{foreign['id']}"):
        assert (await client.get(f"{second_base}/{suffix}", headers=agent)).status_code == 404
    assert (
        await client.post(f"{second_base}/threads", json=thread(), headers=agent)
    ).status_code == 404
    assert (
        await client.post("/api/v1/clusters", json={"name": "Forbidden"}, headers=agent)
    ).status_code == 403
    assert (
        await client.post(f"{first_base}/agents", json={"name": "Forbidden"}, headers=agent)
    ).status_code == 403
    assert (await client.post(f"{first_base}/threads", json=thread())).status_code == 403
    assert (await client.get(f"{first_base}/updates")).status_code == 403
    for cluster_id in (first["id"], second["id"]):
        response = await client.post(
            f"/api/v1/clusters/{cluster_id}/acknowledgements",
            json={"message_ids": [foreign["id"]]},
            headers=agent,
        )
        assert response.status_code == 404
    missing = await client.get(f"/api/v1/clusters/{uuid4()}/threads")
    assert missing.status_code == 404


async def test_admin_token_rotation_revokes_old_token_and_checks_scope(client, provision, db_pool):
    cluster, agents = await provision(name="First", count=1)
    other, _ = await provision(name="Other", count=0)
    agent = agents[0]
    path = f"/api/v1/clusters/{cluster['id']}/agents/{agent['agent']['id']}/rotate-token"
    denied = await client.post(path, headers=headers(agent))
    assert denied.status_code == 403
    mismatched = await client.post(
        f"/api/v1/clusters/{other['id']}/agents/{agent['agent']['id']}/rotate-token"
    )
    assert mismatched.status_code == 404
    nonexistent = await client.post(
        f"/api/v1/clusters/{cluster['id']}/agents/{uuid4()}/rotate-token"
    )
    assert nonexistent.status_code == 404
    assert (await client.get("/api/v1/me", headers=headers(agent))).status_code == 200

    rotated = await client.post(path)
    assert rotated.status_code == 200
    replacement = rotated.json()
    assert replacement["agent"] == agent["agent"]
    assert replacement["token"] != agent["token"]
    assert (await client.get("/api/v1/me", headers=headers(agent))).status_code == 401
    identity = await client.get("/api/v1/me", headers=headers(replacement))
    assert identity.status_code == 200
    assert identity.json()["agent"] == agent["agent"]
    posted = await client.post(
        f"/api/v1/clusters/{cluster['id']}/threads",
        json=thread(),
        headers=headers(replacement),
    )
    assert posted.status_code == 201
    assert posted.json()["author_id"] == agent["agent"]["id"]
    async with db_pool.connection() as connection:
        stored = await (
            await connection.execute(
                "SELECT token_hash FROM agents WHERE id = %s", (agent["agent"]["id"],)
            )
        ).fetchone()
    assert stored["token_hash"] == digest(replacement["token"])


async def test_thread_reply_and_safe_retries(client, provision):
    cluster, agents = await provision(count=2)
    base = f"/api/v1/clusters/{cluster['id']}"
    author = headers(agents[0])
    payload = thread(title="  Subject  ", body="  Content  ")
    created = await client.post(f"{base}/threads", json=payload, headers=author)
    assert created.status_code == 201
    root = created.json()
    assert root["title"] == "Subject"
    assert root["body"] == "Content"
    assert root["parent_id"] is None
    assert root["thread_id"] == root["id"]
    assert root["author_name"] == "Agent 1"
    retry = await client.post(
        f"{base}/threads", json={**payload, "title": "Subject", "body": "Content"}, headers=author
    )
    assert retry.status_code == 200
    assert retry.json() == root
    assert retry.headers["idempotent-replay"] == "true"
    changed = await client.post(
        f"{base}/threads", json={**payload, "title": "Other"}, headers=author
    )
    assert changed.status_code == 409
    # Another identity may independently use the same operation ID.
    own = await client.post(f"{base}/threads", json=payload, headers=headers(agents[1]))
    assert own.status_code == 201
    reply_payload = {"body": "Reply", "request_id": str(uuid4())}
    replies = await asyncio.gather(
        *(
            client.post(f"{base}/threads/{root['id']}/replies", json=reply_payload, headers=author)
            for _ in range(5)
        )
    )
    assert sorted(response.status_code for response in replies) == [200, 200, 200, 200, 201]
    reply = replies[0].json()
    assert reply["parent_id"] == root["id"]
    assert reply["thread_id"] == root["id"]
    assert reply["title"] is None
    detail = await client.get(f"{base}/threads/{root['id']}")
    assert detail.json() == {"thread": root, "replies": [reply], "next_cursor": None}
    listing = await client.get(f"{base}/threads")
    assert (
        next(item for item in listing.json()["items"] if item["id"] == root["id"])["reply_count"]
        == 1
    )
    # Keys cover operation type and thread identity, not just body content.
    changed_operation = await client.post(
        f"{base}/threads", json=thread(request_id=reply_payload["request_id"]), headers=author
    )
    assert changed_operation.status_code == 409
    changed_parent = await client.post(
        f"{base}/threads/{own.json()['id']}/replies", json=reply_payload, headers=author
    )
    assert changed_parent.status_code == 409
    nested = await client.post(
        f"{base}/threads/{reply['id']}/replies",
        json={**reply_payload, "request_id": str(uuid4())},
        headers=author,
    )
    assert nested.status_code == 404
    assert (await client.get(f"{base}/threads/{reply['id']}")).status_code == 404


async def test_invalid_and_cross_cluster_reply_targets(client, provision, db_pool):
    first, agents = await provision(name="First", count=1)
    second, others = await provision(name="Second", count=1)
    foreign = await client.post(
        f"/api/v1/clusters/{second['id']}/threads", json=thread(), headers=headers(others[0])
    )
    for target in (str(uuid4()), foreign.json()["id"]):
        response = await client.post(
            f"/api/v1/clusters/{first['id']}/threads/{target}/replies",
            json={"body": "Reply", "request_id": str(uuid4())},
            headers=headers(agents[0]),
        )
        assert response.status_code == 404
    # The database also rejects a cross-cluster parent if a client bypasses the API.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        async with db_pool.connection() as connection:
            await connection.execute(
                "INSERT INTO messages(id, cluster_id, author_id, parent_id, body, "
                "request_id, payload_hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid4(),
                    first["id"],
                    agents[0]["agent"]["id"],
                    foreign.json()["id"],
                    "Bad parent",
                    uuid4(),
                    "0" * 64,
                ),
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "  "},
        {"title": "x" * 201},
        {"body": ""},
        {"body": "\n "},
        {"body": "x" * 20001},
        {"body": "Null\x00byte"},
        {"body": 123},
        {"author_id": str(uuid4())},
        {"request_id": "bad"},
        {"request_id": None},
    ],
)
async def test_invalid_messages_are_rejected(client, provision, changes):
    cluster, agents = await provision(count=1)
    response = await client.post(
        f"/api/v1/clusters/{cluster['id']}/threads",
        json=thread(**changes),
        headers=headers(agents[0]),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("name", ["", "  ", "x" * 81, "bad\x00name", None])
async def test_invalid_names_are_rejected(client, name):
    assert (await client.post("/api/v1/clusters", json={"name": name})).status_code == 422


async def test_pagination_and_cursors(client, provision):
    cluster, agents = await provision(count=1)
    base = f"/api/v1/clusters/{cluster['id']}"
    author = headers(agents[0])
    roots = []
    for index in range(5):
        response = await client.post(
            f"{base}/threads", json=thread(title=f"Topic {index}"), headers=author
        )
        roots.append(response.json())
    seen = []
    cursor = None
    first_cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        response = await client.get(f"{base}/threads", params=params)
        assert response.status_code == 200
        result = response.json()
        seen.extend(item["id"] for item in result["items"])
        cursor = result["next_cursor"]
        first_cursor = first_cursor or cursor
        if cursor is None:
            break
    assert seen == [item["id"] for item in reversed(roots)]
    for index in range(3):
        response = await client.post(
            f"{base}/threads/{roots[0]['id']}/replies",
            json={"body": str(index), "request_id": str(uuid4())},
            headers=author,
        )
        assert response.status_code == 201
    response = await client.get(f"{base}/threads/{roots[0]['id']}?limit=2")
    assert [item["body"] for item in response.json()["replies"]] == ["0", "1"]
    last = await client.get(
        f"{base}/threads/{roots[0]['id']}",
        params={"limit": 2, "cursor": response.json()["next_cursor"]},
    )
    assert [item["body"] for item in last.json()["replies"]] == ["2"]
    assert last.json()["next_cursor"] is None
    mismatched = await client.get(
        f"{base}/threads/{roots[0]['id']}", params={"cursor": first_cursor}
    )
    assert mismatched.status_code == 422
    for params in ({"cursor": "!bad"}, {"limit": 0}, {"limit": 101}):
        assert (await client.get(f"{base}/threads", params=params)).status_code == 422


async def test_receipts_validate_batch_and_are_independent(client, provision):
    cluster, agents = await provision()
    base = f"/api/v1/clusters/{cluster['id']}"
    author = headers(agents[0])
    roots = [
        (await client.post(f"{base}/threads", json=thread(), headers=author)).json()
        for _ in range(2)
    ]
    response = await client.get(f"{base}/updates?limit=1", headers=author)
    assert response.json()["has_more"] is True
    first = roots[0]["id"]
    invalid = await client.post(
        f"{base}/acknowledgements", json={"message_ids": [first, str(uuid4())]}, headers=author
    )
    assert invalid.status_code == 404
    unchanged = await client.get(f"{base}/updates", headers=author)
    assert len(unchanged.json()["items"]) == 2
    for _ in range(2):
        ack = await client.post(
            f"{base}/acknowledgements", json={"message_ids": [first, first]}, headers=author
        )
        assert ack.json() == {"acknowledged": 1}
    remaining = await client.get(f"{base}/updates", headers=author)
    assert [item["id"] for item in remaining.json()["items"]] == [roots[1]["id"]]
    independent = await client.get(f"{base}/updates", headers=headers(agents[1]))
    assert len(independent.json()["items"]) == 2
    # New members can read full history.
    newcomer = await client.post(f"{base}/agents", json={"name": "New Agent"})
    history = await client.get(f"{base}/updates", headers=headers(newcomer.json()))
    assert len(history.json()["items"]) == 2
    for batch in ([], [str(uuid4())] * 101, ["invalid"]):
        assert (
            await client.post(
                f"{base}/acknowledgements", json={"message_ids": batch}, headers=author
            )
        ).status_code == 422


async def test_migrations_repeat_without_data_loss(client, provision, settings):
    cluster, _ = await provision(count=1)
    await migrate(settings.database_url.get_secret_value())
    listed = await client.get("/api/v1/clusters")
    assert listed.json()["items"][0]["id"] == cluster["id"]
    assert listed.json()["items"][0]["agent_count"] == 1


async def test_database_outage_response_hides_details(settings):
    settings = settings.model_copy(update={"pool_timeout": 0.1})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            # Hold the only usable connections to model pool unavailability.
            connections = [await app.state.pool.getconn() for _ in range(settings.pool_max_size)]
            try:
                response = await http.get("/health")
                assert response.status_code == 503
                assert response.json() == {"detail": "The database is unavailable. Retry later."}
                assert settings.database_url.get_secret_value() not in response.text
            finally:
                for connection in connections:
                    await app.state.pool.putconn(connection)
