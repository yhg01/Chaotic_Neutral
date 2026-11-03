"""Inbox model tools validate arguments and preserve safe retry identities."""

import json
from uuid import uuid4

import httpx
import pytest

from agentrpg.inbox_tools import InboxTools


async def test_inbox_tools_routes_and_stable_ids():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    run_id, namespace, mail_id = uuid4(), uuid4(), uuid4()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test/api/v1/"
    ) as client:
        tools = InboxTools(client, run_id, namespace)
        assert {value["function"]["name"] for value in tools.definitions()} == {
            "list_mail",
            "read_mail",
            "send_mail",
            "acknowledge_mail",
        }
        arguments = {
            "recipient_owner_id": "owner-2",
            "subject": "Hi",
            "body": "Body",
            "in_reply_to": str(mail_id),
        }
        assert (await tools.execute("send_mail", arguments, "saved-call"))["ok"]
        assert (
            await InboxTools(client, run_id, namespace).execute(
                "send_mail", arguments, "saved-call"
            )
        )["ok"]
        assert json.loads(requests[0].content) == json.loads(requests[1].content)
        assert "sender_owner_id" not in json.loads(requests[0].content)
        await tools.execute("read_mail", {"message_id": str(mail_id)}, "read")
        assert requests[-1].url.path.endswith(f"/inbox/{mail_id}")
        await tools.execute("acknowledge_mail", {"message_ids": [str(mail_id)]}, "receipt")
        assert requests[-1].url.path.endswith("/inbox/receipts")
        await tools.execute(
            "list_mail", {"query": "meeting", "limit": 2, "unread_only": True}, "list"
        )
        assert requests[-1].url.params["query"] == "meeting"
        assert requests[-1].method == "GET"


@pytest.mark.parametrize("mailbox", ["sent", "all"])
async def test_unread_filter_requires_received_inbox_without_http(mailbox):
    def handle(request):
        pytest.fail("An invalid filter combination must not send a request")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test/"
    ) as client:
        result = await InboxTools(client, uuid4()).execute(
            "list_mail", {"mailbox": mailbox, "unread_only": True}, "list"
        )
        assert result["error"]["status"] == 422
        assert "unread_only=true requires mailbox='inbox'" in result["error"]["detail"]
        assert "IDs" not in result["error"]["detail"]


async def test_rejected_mailbox_query_does_not_claim_bad_mail_ids():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(422)),
        base_url="http://test/",
    ) as client:
        result = await InboxTools(client, uuid4()).execute("list_mail", {}, "list")
        assert "query" in result["error"]["detail"]
        assert "IDs" not in result["error"]["detail"]


@pytest.mark.parametrize(
    "name,arguments,call_id",
    [
        ("not-a-tool", {}, "x"),
        ("send_mail", {"sender_owner_id": "spoof"}, "x"),
        ("read_mail", {"message_id": "bad"}, "x"),
        ("list_mail", {"limit": 0}, "x"),
        ("list_mail", {}, ""),
        ("list_mail", {}, "\x00"),
        ("list_mail", {}, "x" * 513),
        ("list_mail", {}, "\ud800"),
        ("list_mail", [], "x"),
        ("send_mail", {"recipient_owner_id": "owner-2", "subject": "Subject", "body": "\x00"}, "x"),
    ],
)
async def test_inbox_tools_invalid_input_does_not_send(name, arguments, call_id):
    def handle(request):
        pytest.fail("Invalid tool input must not make an HTTP request")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test/"
    ) as client:
        result = await InboxTools(client, uuid4()).execute(name, arguments, call_id)
        assert result["error"]["status"] == 422


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 500, 503])
async def test_inbox_tools_never_echo_remote_error_body(status):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json={"detail": "SECRET-CONFIGURATION"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test/"
    ) as client:
        result = await InboxTools(client, uuid4()).execute("list_mail", {}, "list")
    assert "SECRET-CONFIGURATION" not in json.dumps(result)
    assert count == (3 if status == 503 else 1)


@pytest.mark.parametrize("body", [b"not-json", b"[]"])
async def test_inbox_tools_reject_invalid_server_response(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
        base_url="http://test/",
    ) as client:
        result = await InboxTools(client, uuid4()).execute("list_mail", {}, "list")
    assert result["error"]["status"] == 502


async def test_inbox_tools_transport_retries_keep_write_id():
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) < 3:
            raise httpx.ConnectError("PRIVATE-CONFIGURATION", request=request)
        return httpx.Response(201, json={"id": str(uuid4())})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test/"
    ) as client:
        result = await InboxTools(client, uuid4()).execute(
            "send_mail",
            {"recipient_owner_id": "owner-2", "subject": "Subject", "body": "Body"},
            "send",
        )
    assert result["ok"]
    assert len({json.loads(request.content)["request_id"] for request in requests}) == 1
