"""A bounded model loop that gives one Agent access to its message board tools."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from agentrpg.agent_tools import BoardTools

OPENROUTER_URL = "https://openrouter.ai/api/v1"


class AgentRunError(RuntimeError):
    """A safe error message which does not contain credentials or response bodies."""


@dataclass
class ModelUsage:
    requests: int = 0
    completed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reported_cost_usd: float = 0
    cost_reports: int = 0
    in_flight: int = 0
    peak_in_flight: int = 0
    models: set[str] = field(default_factory=set)

    def report(self) -> dict:
        return {
            "requests": self.requests,
            "completed": self.completed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reported_cost_usd": round(self.reported_cost_usd, 8),
            "cost_reports": self.cost_reports,
            "peak_concurrent_model_requests": self.peak_in_flight,
            "models": sorted(self.models),
        }


class ModelAgent:
    """Keep model history and board identity separate for each Agent.

    The provider client owns the provider credential. The BoardTools client owns
    the Agent credential. Neither credential is included in model messages.
    Clients remain owned by the caller. Run one task at a time per instance;
    different instances can run concurrently.
    """

    def __init__(
        self,
        provider: httpx.AsyncClient,
        board: BoardTools,
        model: str,
        instructions: str,
        usage: ModelUsage | None = None,
        max_steps: int = 10,
        max_tokens: int = 800,
    ):
        if not 1 <= max_steps <= 20 or not 1 <= max_tokens <= 4096:
            raise ValueError("Model step and output limits are outside the allowed range.")
        self.provider = provider
        self.board = board
        self.model = model
        self.usage = usage if usage is not None else ModelUsage()
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": instructions}]
        self.events: list[dict[str, Any]] = []
        self._running = False

    async def _completion(self) -> dict:
        payload = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.board.definitions(),
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "provider": {"require_parameters": True},
        }
        # Only inference is retried here. Board writes have their own operation IDs.
        for attempt in range(3):
            self.usage.requests += 1
            self.usage.in_flight += 1
            self.usage.peak_in_flight = max(self.usage.peak_in_flight, self.usage.in_flight)
            try:
                response = await self.provider.post("/chat/completions", json=payload)
            except httpx.TransportError:
                if attempt == 2:
                    raise AgentRunError(
                        "Model provider connection failed after three attempts."
                    ) from None
                response = None
            finally:
                self.usage.in_flight -= 1
            if response is not None and response.status_code not in (429, 500, 502, 503, 504):
                if response.status_code != 200:
                    raise AgentRunError(f"Model provider returned HTTP {response.status_code}.")
                try:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError
                    message = data["choices"][0]["message"]
                    if not isinstance(message, dict) or message.get("role") != "assistant":
                        raise ValueError
                    if message.get("content") is not None and not isinstance(
                        message["content"], str
                    ):
                        raise ValueError
                    usage = data.get("usage") or {}
                    if not isinstance(usage, dict):
                        raise ValueError
                    self.usage.input_tokens += int(usage.get("prompt_tokens") or 0)
                    self.usage.output_tokens += int(usage.get("completion_tokens") or 0)
                    if usage.get("cost") is not None:
                        self.usage.reported_cost_usd += float(usage["cost"])
                        self.usage.cost_reports += 1
                    self.usage.models.add(str(data.get("model", self.model)))
                    self.usage.completed += 1
                    if data["choices"][0].get("finish_reason") == "length":
                        raise AgentRunError("Model output reached the token limit.")
                    return message
                except (ValueError, TypeError, KeyError, IndexError):
                    raise AgentRunError("Model provider returned an invalid completion.") from None
            if attempt < 2:
                await asyncio.sleep(0.5 * (2**attempt))
        raise AgentRunError("Model provider remained unavailable after three attempts.")

    async def run(self, task: str) -> str:
        if self._running:
            raise AgentRunError("This Agent is already processing a task.")
        self._running = True
        self.messages.append({"role": "user", "content": task})
        try:
            for _ in range(self.max_steps):
                message = await self._completion()
                calls = message.get("tool_calls") or []
                if not isinstance(calls, list) or len(calls) > 8:
                    raise AgentRunError("Model returned an invalid tool-call batch.")
                prepared = []
                canonical_calls = []
                seen_ids = set()
                for call in calls:
                    try:
                        call_id = call["id"]
                        function = call["function"]
                        name = function["name"]
                        if (
                            call.get("type") != "function"
                            or not isinstance(call_id, str)
                            or not call_id
                            or call_id in seen_ids
                            or not isinstance(name, str)
                            or not name
                        ):
                            raise ValueError
                        seen_ids.add(call_id)
                        raw_arguments = function.get("arguments")
                        prepared.append((call_id, name, raw_arguments))
                        canonical_calls.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": raw_arguments
                                    if isinstance(raw_arguments, str)
                                    else json.dumps(raw_arguments),
                                },
                            }
                        )
                    except (KeyError, TypeError, ValueError, AttributeError):
                        raise AgentRunError("Model returned an invalid tool call.") from None
                assistant = {"role": "assistant", "content": message.get("content")}
                if calls:
                    assistant["tool_calls"] = canonical_calls
                if message.get("reasoning_details"):
                    assistant["reasoning_details"] = message["reasoning_details"]
                self.messages.append(assistant)
                if not calls:
                    return message.get("content") or ""
                for call_id, name, raw_arguments in prepared:
                    try:
                        arguments = json.loads(raw_arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError
                    except (KeyError, TypeError, ValueError):
                        arguments = None
                        result = {
                            "ok": False,
                            "error": {"status": 422, "detail": "Arguments must be a JSON object."},
                        }
                    else:
                        result = await self.board.execute(name, arguments, call_id)
                    self.events.append(
                        {"tool": name, "call_id": call_id, "arguments": arguments, "result": result}
                    )
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)}
                    )
            raise AgentRunError("Agent reached its model step limit before completion.")
        finally:
            self._running = False
