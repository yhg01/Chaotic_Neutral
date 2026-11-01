"""Model tool contracts, real board integration, and bounded outage retries."""

import asyncio
import json
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
import pytest_asyncio

from agentrpg.agent_tools import BoardTools


@pytest_asyncio.fixture
async def tool_factory(app):
    clients = []

    def create(cluster, agent, namespace=None):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1",
            headers={"Authorization": f"Bearer {agent['token']}"},
        )
        clients.append(client)
        return BoardTools(client, cluster["id"], namespace)

    yield create
    for client in clients:
        await client.aclose()


def data(result):
    assert result["ok"] is True, result
    return result["data"]


async def test_tools_post_read_reply_updates_and_acknowledge(provision, tool_factory):
    cluster, agents = await provision()
    first = tool_factory(cluster, agents[0])
    second = tool_factory(cluster, agents[1])
    assert data(await first.execute("list_threads", {}, "empty"))["items"] == []
    assert data(await first.execute("get_updates", {}, "empty-updates")) == {
        "items": [],
        "has_more": False,
    }
    root = data(
        await first.execute(
            "post_thread", {"title": "  Meeting  ", "body": "  <b>Discuss plans</b>  "}, "post-1"
        )
    )
    assert root["title"] == "Meeting"
    assert root["body"] == "<b>Discuss plans</b>"
    assert root["author_id"] == agents[0]["agent"]["id"]
    assert root["cluster_id"] == cluster["id"]
    reply = data(
        await second.execute("reply", {"thread_id": root["id"], "body": "  Agreed  "}, "reply-1")
    )
    assert reply["body"] == "Agreed"
    assert reply["author_id"] == agents[1]["agent"]["id"]
    assert reply["parent_id"] == root["id"]
    listing = data(await second.execute("list_threads", {"limit": 1}, "list"))
    assert listing == {
        "items": [
            {**{key: value for key, value in root.items() if key != "body"}, "reply_count": 1}
        ],
        "next_cursor": None,
    }
    api_listing = await first.client.get(f"clusters/{cluster['id']}/threads")
    assert api_listing.json()["items"][0]["body"] == root["body"]
    detail = data(await first.execute("read_thread", {"thread_id": root["id"]}, "read"))
    assert detail == {"thread": root, "replies": [reply], "next_cursor": None}
    unread = data(await second.execute("get_updates", {}, "updates"))
    assert unread["items"] == [root, reply]
    assert data(await second.execute("get_updates", {}, "updates-again")) == unread
    messages = {"message_ids": [root["id"], reply["id"], root["id"]]}
    assert data(await second.execute("acknowledge", messages, "ack")) == {"acknowledged": 2}
    assert data(await second.execute("acknowledge", messages, "ack-again")) == {"acknowledged": 2}
    assert data(await second.execute("get_updates", {}, "after-ack"))["items"] == []
    assert data(await first.execute("get_updates", {}, "own-receipts")) == unread


async def test_tools_paginate_threads_replies_and_updates(provision, tool_factory):
    cluster, agents = await provision(count=1)
    tools = tool_factory(cluster, agents[0])
    roots = [
        data(await tools.execute("post_thread", {"title": str(i), "body": "Body"}, f"post-{i}"))
        for i in range(3)
    ]
    first_page = data(await tools.execute("list_threads", {"limit": 2}, "list-1"))
    second_page = data(
        await tools.execute(
            "list_threads", {"limit": 2, "cursor": first_page["next_cursor"]}, "list-2"
        )
    )
    assert first_page["items"] + second_page["items"] == [
        {**{key: value for key, value in root.items() if key != "body"}, "reply_count": 0}
        for root in reversed(roots)
    ]
    assert second_page["next_cursor"] is None
    replies = [
        data(
            await tools.execute(
                "reply", {"thread_id": roots[0]["id"], "body": str(i)}, f"reply-{i}"
            )
        )
        for i in range(3)
    ]
    first_page = data(
        await tools.execute("read_thread", {"thread_id": roots[0]["id"], "limit": 2}, "read-1")
    )
    second_page = data(
        await tools.execute(
            "read_thread",
            {"thread_id": roots[0]["id"], "limit": 2, "cursor": first_page["next_cursor"]},
            "read-2",
        )
    )
    assert first_page["replies"] + second_page["replies"] == replies
    assert first_page["thread"] == second_page["thread"] == roots[0]
    assert second_page["next_cursor"] is None
    invalid_cursor = await tools.execute("list_threads", {"cursor": "malformed"}, "bad-cursor")
    assert invalid_cursor == {
        "ok": False,
        "error": {"status": 422, "detail": "Invalid history cursor."},
    }
    first_updates = data(await tools.execute("get_updates", {"limit": 2}, "updates-1"))
    assert first_updates["has_more"] is True
    assert first_updates["items"] == roots[:2]
    data(
        await tools.execute(
            "acknowledge", {"message_ids": [row["id"] for row in roots[:2]]}, "ack-page"
        )
    )
    remaining = data(await tools.execute("get_updates", {"limit": 100}, "updates-2"))
    assert remaining["items"] == [roots[2], *replies]
    assert remaining["has_more"] is False


async def test_write_replay_conflicts_and_restored_namespace(provision, tool_factory):
    cluster, agents = await provision(count=1)
    namespace = uuid4()
    tools = tool_factory(cluster, agents[0], namespace)
    arguments = {"title": "One thread", "body": "One body"}
    results = await asyncio.gather(
        *(tools.execute("post_thread", arguments, "same-call") for _ in range(8))
    )
    root = data(results[0])
    assert all(data(result) == root for result in results)
    restored = BoardTools(tools.client, cluster["id"], namespace)
    assert data(await restored.execute("post_thread", arguments, "same-call")) == root
    conflict = await restored.execute(
        "post_thread", {**arguments, "body": "Different body"}, "same-call"
    )
    assert conflict["error"]["status"] == 409
    operation_conflict = await restored.execute(
        "reply", {"thread_id": root["id"], "body": "One body"}, "same-call"
    )
    assert operation_conflict["error"]["status"] == 409
    reply_arguments = {"thread_id": root["id"], "body": "Reply body"}
    reply = data(await tools.execute("reply", reply_arguments, "reply-call"))
    assert data(await restored.execute("reply", reply_arguments, "reply-call")) == reply
    separate_run = BoardTools(tools.client, cluster["id"])
    assert separate_run.operation_namespace != namespace
    other = data(await separate_run.execute("post_thread", arguments, "same-call"))
    assert other["id"] != root["id"]
    changed_thread = await restored.execute(
        "reply", {**reply_arguments, "thread_id": other["id"]}, "reply-call"
    )
    assert changed_thread["error"]["status"] == 409
    assert len(data(await tools.execute("list_threads", {}, "list"))["items"]) == 2


async def test_cluster_boundaries_and_atomic_acknowledgements(provision, tool_factory):
    cluster, agents = await provision(name="First", count=1)
    other, others = await provision(name="Other", count=1)
    tools = tool_factory(cluster, agents[0])
    foreign_tools = tool_factory(other, others[0])
    root = data(await tools.execute("post_thread", {"title": "Own", "body": "Own"}, "own"))
    foreign = data(
        await foreign_tools.execute("post_thread", {"title": "Other", "body": "Other"}, "other")
    )
    wrong_cluster = BoardTools(tools.client, other["id"])
    calls = [
        ("list_threads", {}),
        ("read_thread", {"thread_id": foreign["id"]}),
        ("post_thread", {"title": "Wrong", "body": "Wrong"}),
        ("reply", {"thread_id": foreign["id"], "body": "Wrong"}),
        ("get_updates", {}),
        ("acknowledge", {"message_ids": [foreign["id"]]}),
    ]
    for name, arguments in calls:
        result = await wrong_cluster.execute(name, arguments, name)
        assert result["ok"] is False
        assert result["error"]["status"] == 404
    for name, arguments in [
        ("read_thread", {"thread_id": foreign["id"]}),
        ("reply", {"thread_id": foreign["id"], "body": "Wrong"}),
        ("acknowledge", {"message_ids": [root["id"], foreign["id"]]}),
    ]:
        result = await tools.execute(name, arguments, f"foreign-{name}")
        assert result["error"]["status"] == 404
    assert data(await tools.execute("get_updates", {}, "after-bad-ack"))["items"] == [root]


async def test_fifteen_adapters_share_board_concurrently(provision, tool_factory):
    cluster, agents = await provision(count=15)
    adapters = [tool_factory(cluster, agent) for agent in agents]
    root = data(
        await adapters[0].execute("post_thread", {"title": "Shared", "body": "Discuss"}, "root")
    )

    async def post_and_read(index, adapter):
        posted, updates = await asyncio.gather(
            adapter.execute("reply", {"thread_id": root["id"], "body": str(index)}, "reply"),
            adapter.execute("get_updates", {"limit": 100}, "updates"),
        )
        assert root["id"] in {row["id"] for row in data(updates)["items"]}
        return data(posted)

    replies = await asyncio.wait_for(
        asyncio.gather(*(post_and_read(i, adapter) for i, adapter in enumerate(adapters))),
        timeout=20,
    )
    assert len({row["id"] for row in replies}) == 15
    assert {row["author_id"] for row in replies} == {agent["agent"]["id"] for agent in agents}
    detail = data(
        await adapters[0].execute("read_thread", {"thread_id": root["id"], "limit": 100}, "read")
    )
    assert {row["id"] for row in detail["replies"]} == {row["id"] for row in replies}


async def test_definitions_exclude_identity_and_operation_controls():
    async with httpx.AsyncClient() as client:
        tools = BoardTools(client, str(uuid4()))
        definitions = tools.definitions()
        assert {item["function"]["name"] for item in definitions} == {
            "list_threads",
            "read_thread",
            "post_thread",
            "reply",
            "get_updates",
            "acknowledge",
        }
        for definition in definitions:
            assert definition["type"] == "function"
            parameters = definition["function"]["parameters"]
            assert parameters["additionalProperties"] is False
            assert (
                not {"cluster_id", "author_id", "token", "request_id"}
                & parameters["properties"].keys()
            )
        schema = next(item for item in definitions if item["function"]["name"] == "reply")
        assert schema["function"]["parameters"]["properties"]["thread_id"]["format"] == "uuid"
        definitions[0]["function"]["name"] = "changed"
        assert tools.definitions()[0]["function"]["name"] == "list_threads"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("unknown", {}),
        ("list_threads", {"limit": 0}),
        ("list_threads", {"limit": 101}),
        ("list_threads", {"limit": True}),
        ("list_threads", {"limit": "10"}),
        ("list_threads", {"limit": 1.5}),
        ("list_threads", {"cursor": " "}),
        ("list_threads", {"cursor": "a" * 513}),
        ("read_thread", {"thread_id": "../outside"}),
        ("read_thread", {"thread_id": 123}),
        ("read_thread", {}),
        ("post_thread", {"title": " ", "body": "Body"}),
        ("post_thread", {"title": "a" * 201, "body": "Body"}),
        ("post_thread", {"title": "Title", "body": "a" * 20001}),
        ("post_thread", {"title": "Title", "body": "\ud800"}),
        ("post_thread", {"title": "Title", "body": "\x00"}),
        ("post_thread", {"title": "Title", "body": 123}),
        ("post_thread", {"title": "Title"}),
        ("post_thread", {"title": "Title", "body": "Body", "author_id": str(uuid4())}),
        ("post_thread", {"title": "Title", "body": "Body", "request_id": str(uuid4())}),
        ("reply", {"thread_id": str(uuid4()), "body": "\t\n"}),
        ("reply", {"thread_id": str(uuid4()), "body": "Body", "token": "secret"}),
        ("get_updates", {"cluster_id": str(uuid4())}),
        ("get_updates", {"limit": None}),
        ("acknowledge", {"message_ids": []}),
        ("acknowledge", {"message_ids": [str(uuid4())] * 101}),
        ("acknowledge", {"message_ids": ["invalid"]}),
        ("acknowledge", {"message_ids": str(uuid4())}),
        ("acknowledge", {"message_ids": [None]}),
        ("acknowledge", {"message_ids": (str(uuid4()),)}),
        ("list_threads", []),
        ("list_threads", None),
    ],
)
async def test_invalid_arguments_are_rejected_before_a_request(name, arguments):
    # No base URL is provided: any accidental request would fail with 503.
    async with httpx.AsyncClient() as client:
        result = await BoardTools(client, str(uuid4())).execute(name, arguments, "valid-call")
    assert result["ok"] is False
    assert result["error"]["status"] == 422
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("call_id", [None, 1, "", " ", "a" * 513, "\ud800", "\x00"])
async def test_invalid_call_ids_are_rejected(call_id):
    async with httpx.AsyncClient() as client:
        result = await BoardTools(client, str(uuid4())).execute("list_threads", {}, call_id)
    assert result["error"]["status"] == 422


async def test_constructor_rejects_invalid_cluster_and_namespace():
    async with httpx.AsyncClient() as client:
        for cluster in ("../outside", "", None, 123):
            with pytest.raises(ValueError):
                BoardTools(client, cluster)
        with pytest.raises(ValueError):
            BoardTools(client, str(uuid4()), "not-a-uuid")


@pytest.mark.parametrize("failure", ["network", "503"])
async def test_retry_preserves_request_id_and_payload(failure):
    requests = []
    namespace = uuid4()

    async def respond(request):
        requests.append(request)
        if len(requests) == 1:
            if failure == "network":
                raise httpx.ReadError("Bearer secret at http://private-host", request=request)
            return httpx.Response(503, text="<html>Bearer secret</html>")
        return httpx.Response(201, json={"id": str(uuid4())})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test/api/v1"
    ) as client:
        tools = BoardTools(client, str(uuid4()), namespace)
        result = await tools.execute("post_thread", {"title": "Title", "body": "Body"}, "call-1")
    assert result["ok"] is True
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert json.loads(requests[0].content)["request_id"] == str(uuid5(namespace, "call-1"))
    assert requests[0].url.path == f"/api/v1/clusters/{tools.cluster_id}/threads"


@pytest.mark.parametrize("failure", ["network", "503"])
async def test_outage_has_bounded_retries_and_safe_error(failure):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "network":
            raise httpx.ConnectError("Bearer secret at http://private-host", request=request)
        return httpx.Response(
            503, json={"detail": "<html>Bearer secret http://private-host</html>"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test/api/v1"
    ) as client:
        result = await BoardTools(client, str(uuid4())).execute("get_updates", {}, "call")
    assert len(requests) == 3
    assert result["ok"] is False
    assert result["error"]["status"] == 503
    for forbidden in ("secret", "http://", "private-host", "<html>"):
        assert forbidden not in json.dumps(result)


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 500])
async def test_other_http_failures_are_not_retried_and_do_not_copy_input(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": [{"input": "Bearer secret"}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test/api/v1"
    ) as client:
        result = await BoardTools(client, str(uuid4())).execute("list_threads", {}, "call")
    assert len(requests) == 1
    assert result["error"]["status"] == status
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("body", [b"<html>secret</html>", b"[]", b"null"])
async def test_invalid_success_response_is_safe(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
        base_url="http://test/api/v1",
    ) as client:
        result = await BoardTools(client, str(uuid4())).execute("list_threads", {}, "call")
    assert result == {
        "ok": False,
        "error": {"status": 502, "detail": "The board returned an invalid response."},
    }


async def test_redirects_are_not_followed_even_when_client_enables_them():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "http://other-host/api/v1/clusters"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="http://test/api/v1",
        follow_redirects=True,
    ) as client:
        result = await BoardTools(client, str(uuid4())).execute("get_updates", {}, "call")
    assert len(requests) == 1
    assert result["error"]["status"] == 307


async def test_transport_retry_after_a_committed_write_uses_real_board(
    app, provision, tool_factory
):
    cluster, agents = await provision(count=1)
    transport = httpx.ASGITransport(app=app)
    attempts = []

    async def respond(request):
        attempts.append(request)
        response = await transport.handle_async_request(request)
        await response.aread()
        if len(attempts) == 1:
            await response.aclose()
            raise httpx.ReadError("The committed response was lost.", request=request)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="http://test/api/v1",
        headers={"Authorization": f"Bearer {agents[0]['token']}"},
    ) as client:
        tools = BoardTools(client, cluster["id"])
        root = data(
            await tools.execute("post_thread", {"title": "Once", "body": "One post"}, "lost")
        )
    assert len(attempts) == 2
    observer = tool_factory(cluster, agents[0])
    listing = data(await observer.execute("list_threads", {}, "list"))
    assert [row["id"] for row in listing["items"]] == [root["id"]]
    assert UUID(root["id"])
