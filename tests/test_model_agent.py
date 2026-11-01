"""Model protocol checks without provider or board network requests."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from agentrpg.model_agent import OPENROUTER_URL, AgentRunError, ModelAgent, ModelUsage


class StubBoard:
    """Record calls and supply the board adapter's public result format."""

    def __init__(self):
        self.calls = []

    def definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_threads",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute(self, name, arguments, call_id):
        self.calls.append((name, arguments, call_id))
        if name != "list_threads":
            return {"ok": False, "error": {"status": 422, "detail": "Unknown board tool."}}
        return {"ok": True, "data": {"items": [{"id": "thread-1", "title": "Meeting"}]}}


def tool_call(arguments="{}", name="list_threads", call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def completion(content="Done.", *, calls=None, finish_reason="stop", usage=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {
        "model": "provider/test-model",
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.0002},
    }


def provider_client(handler):
    return httpx.AsyncClient(
        base_url=OPENROUTER_URL,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-provider-secret"},
    )


async def test_tool_loop_returns_result_and_records_usage():
    board = StubBoard()
    requests = []
    reasoning = [{"type": "reasoning.text", "text": "Read the board first."}]

    def handler(request):
        assert str(request.url) == OPENROUTER_URL + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-provider-secret"
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            response = completion(None, calls=[tool_call('{"limit":2}')])
            response["choices"][0]["message"]["reasoning_details"] = reasoning
            return httpx.Response(200, json=response)
        return httpx.Response(200, json=completion("The board has a meeting thread."))

    async with provider_client(handler) as provider:
        agent = ModelAgent(provider, board, "test-model", "Use the board.", max_tokens=150)
        assert await agent.run("List the threads.") == "The board has a meeting thread."

    assert board.calls == [("list_threads", {"limit": 2}, "call-1")]
    assert requests[0] == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Use the board."},
            {"role": "user", "content": "List the threads."},
        ],
        "tools": board.definitions(),
        "tool_choice": "auto",
        "max_tokens": 150,
        "temperature": 0,
        "provider": {"require_parameters": True},
    }
    assert requests[1]["messages"][-2]["reasoning_details"] == reasoning
    result = json.loads(requests[1]["messages"][-1]["content"])
    assert result == {"ok": True, "data": {"items": [{"id": "thread-1", "title": "Meeting"}]}}
    assert requests[1]["messages"][-1]["tool_call_id"] == "call-1"
    assert agent.events == [
        {"tool": "list_threads", "arguments": {"limit": 2}, "call_id": "call-1", "result": result}
    ]
    assert "test-provider-secret" not in json.dumps(agent.messages)
    assert agent.usage.report() == {
        "requests": 2,
        "completed": 2,
        "input_tokens": 20,
        "output_tokens": 8,
        "reported_cost_usd": 0.0004,
        "cost_reports": 2,
        "peak_concurrent_model_requests": 1,
        "models": ["provider/test-model"],
    }
    assert agent.usage.in_flight == 0


@pytest.mark.parametrize("arguments", ["{broken json", "[]", None])
async def test_malformed_arguments_are_returned_to_model_without_board_execution(arguments):
    board = StubBoard()
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        response = (
            completion(None, calls=[tool_call(arguments)])
            if len(requests) == 1
            else completion("I could not read the board.")
        )
        return httpx.Response(200, json=response)

    async with provider_client(handler) as provider:
        agent = ModelAgent(provider, board, "test-model", "Use the board.")
        assert await agent.run("List the threads.") == "I could not read the board."

    assert board.calls == []
    stored_call = requests[1]["messages"][-2]["tool_calls"][0]
    assert isinstance(stored_call["function"]["arguments"], str)
    tool_message = requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-1"
    assert json.loads(tool_message["content"]) == {
        "ok": False,
        "error": {"status": 422, "detail": "Arguments must be a JSON object."},
    }
    assert agent.events[0]["arguments"] is None


async def test_unknown_tool_error_is_returned_to_model():
    board = StubBoard()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = (
            completion(None, calls=[tool_call(name="unknown_tool")])
            if len(requests) == 1
            else completion("That tool is unavailable.")
        )
        return httpx.Response(200, json=response)

    async with provider_client(handler) as provider:
        agent = ModelAgent(provider, board, "test-model", "Use the board.")
        assert await agent.run("Use a tool.") == "That tool is unavailable."

    assert board.calls == [("unknown_tool", {}, "call-1")]
    assert json.loads(requests[1]["messages"][-1]["content"]) == {
        "ok": False,
        "error": {"status": 422, "detail": "Unknown board tool."},
    }


@pytest.mark.parametrize(
    ("calls", "message"),
    [
        ([{"function": {"name": "list_threads", "arguments": "{}"}}], "invalid tool call"),
        (
            [tool_call(), {"function": {"name": "list_threads", "arguments": "{}"}}],
            "invalid tool call",
        ),
        ([tool_call(), tool_call()], "invalid tool call"),
        ({"unexpected": "object"}, "invalid tool-call batch"),
        ([tool_call(call_id=f"call-{index}") for index in range(9)], "invalid tool-call batch"),
    ],
)
async def test_invalid_tool_calls_stop_without_board_execution(calls, message):
    board = StubBoard()
    async with provider_client(
        lambda request: httpx.Response(200, json=completion(None, calls=calls))
    ) as provider:
        agent = ModelAgent(provider, board, "test-model", "Use the board.")
        with pytest.raises(AgentRunError, match=message):
            await agent.run("List the threads.")
    assert board.calls == []
    assert agent.events == []
    assert agent.messages == [
        {"role": "system", "content": "Use the board."},
        {"role": "user", "content": "List the threads."},
    ]
    assert agent.usage.requests == 1
    assert agent.usage.in_flight == 0


async def test_step_limit_bounds_repeated_tool_calls():
    board = StubBoard()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json=completion(None, calls=[tool_call(call_id=f"call-{len(requests)}")])
        )

    async with provider_client(handler) as provider:
        agent = ModelAgent(provider, board, "test-model", "Use the board.", max_steps=2)
        with pytest.raises(AgentRunError, match="step limit"):
            await agent.run("Keep reading.")
    assert len(requests) == len(board.calls) == len(agent.events) == 2
    assert agent.usage.requests == agent.usage.completed == 2


async def test_token_limit_stops_before_executing_partial_tool_call():
    board = StubBoard()
    response = completion(None, calls=[tool_call('{"limit":')], finish_reason="length")
    async with provider_client(lambda request: httpx.Response(200, json=response)) as provider:
        agent = ModelAgent(provider, board, "test-model", "Use the board.", max_tokens=12)
        with pytest.raises(AgentRunError, match="token limit"):
            await agent.run("List the threads.")
    assert board.calls == []
    assert agent.events == []
    assert agent.usage.requests == agent.usage.completed == 1
    assert agent.usage.output_tokens == 4


@pytest.mark.parametrize("status", [401, 402])
async def test_provider_credential_and_credit_errors_are_not_retried_or_exposed(status, caplog):
    board = StubBoard()
    secret = "private-provider-response-body"
    async with provider_client(
        lambda request: httpx.Response(status, json={"error": {"message": secret}})
    ) as provider:
        agent = ModelAgent(provider, board, "test-model", "Use the board.")
        with pytest.raises(AgentRunError, match=f"HTTP {status}") as failure:
            await agent.run("List the threads.")
    assert agent.usage.requests == 1
    assert agent.usage.completed == agent.usage.in_flight == 0
    assert board.calls == []
    assert secret not in str(failure.value) + caplog.text + json.dumps(agent.messages)
    assert "test-provider-secret" not in str(failure.value) + caplog.text


@pytest.mark.parametrize("failure_kind", ["status", "transport"])
@pytest.mark.parametrize("recover", [True, False])
async def test_transient_provider_failures_have_bounded_retries(failure_kind, recover, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("agentrpg.model_agent.asyncio.sleep", sleep)
    attempts = []

    def handler(request):
        attempts.append(json.loads(request.content))
        if recover and len(attempts) == 3:
            return httpx.Response(200, json=completion())
        if failure_kind == "transport":
            raise httpx.ConnectError("private-connection-detail", request=request)
        return httpx.Response(503, text="private-response-detail")

    async with provider_client(handler) as provider:
        agent = ModelAgent(provider, StubBoard(), "test-model", "Use the board.")
        if recover:
            assert await agent.run("List the threads.") == "Done."
        else:
            with pytest.raises(AgentRunError, match="three attempts") as failure:
                await agent.run("List the threads.")
            assert "private-" not in str(failure.value)

    assert len(attempts) == agent.usage.requests == 3
    assert attempts[0] == attempts[1] == attempts[2]
    assert agent.usage.completed == int(recover)
    assert agent.usage.in_flight == 0
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]


@pytest.mark.parametrize("invalid_field", ["choices", "usage", "content"])
async def test_invalid_provider_completions_raise_safe_errors(invalid_field):
    response = completion()
    if invalid_field == "choices":
        response["choices"] = []
    elif invalid_field == "usage":
        response["usage"] = ["private-response-detail"]
    else:
        response["choices"][0]["message"]["content"] = {"unexpected": "private-response-detail"}
    async with provider_client(lambda request: httpx.Response(200, json=response)) as provider:
        agent = ModelAgent(provider, StubBoard(), "test-model", "Use the board.")
        with pytest.raises(AgentRunError, match="invalid completion") as failure:
            await agent.run("List the threads.")
    assert "private-response-detail" not in str(failure.value)
    assert agent.usage.requests == 1
    assert agent.usage.in_flight == 0


async def test_separate_agents_overlap_requests_and_keep_history_separate():
    both_started = asyncio.Event()
    release = asyncio.Event()
    requests = []
    usage = ModelUsage()

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 2:
            both_started.set()
        await release.wait()
        return httpx.Response(200, json=completion(payload["messages"][-1]["content"]))

    async with provider_client(handler) as provider:
        first = ModelAgent(provider, StubBoard(), "test-model", "First Agent.", usage)
        second = ModelAgent(provider, StubBoard(), "test-model", "Second Agent.", usage)
        tasks = [
            asyncio.create_task(first.run("First task.")),
            asyncio.create_task(second.run("Second task.")),
        ]
        try:
            await asyncio.wait_for(both_started.wait(), timeout=2)
            assert usage.in_flight == usage.peak_in_flight == 2
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        assert results == ["First task.", "Second task."]
        assert await first.run("First follow-up.") == "First follow-up."

    assert usage.requests == usage.completed == 3
    assert usage.in_flight == 0
    assert usage.report()["peak_concurrent_model_requests"] == 2
    assert usage.input_tokens == 30
    assert usage.output_tokens == 12
    assert first.messages is not second.messages
    assert "Second" not in json.dumps(first.messages)
    assert "First" not in json.dumps(second.messages)
    assert [message["content"] for message in requests[2]["messages"]] == [
        "First Agent.",
        "First task.",
        "First task.",
        "First follow-up.",
    ]


async def test_one_agent_rejects_a_simultaneous_run_and_accepts_the_next_task():
    started = asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        started.set()
        await release.wait()
        return httpx.Response(200, json=completion())

    async with provider_client(handler) as provider:
        agent = ModelAgent(provider, StubBoard(), "test-model", "Use the board.")
        first = asyncio.create_task(agent.run("First task."))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            with pytest.raises(AgentRunError, match="already processing"):
                await agent.run("Rejected task.")
            assert len(requests) == 1
            assert "Rejected task." not in json.dumps(agent.messages)
        finally:
            release.set()
            assert await first == "Done."
        assert await agent.run("Next task.") == "Done."

    assert agent.usage.requests == 2
    assert agent.usage.peak_in_flight == 1
    assert agent.usage.in_flight == 0
