"""A chat model that is called at its own provider, with that provider's key.

The request has the chat-completions form. A model name is "provider:model", for example
"deepseek:deepseek-flash". `budget.DIRECT_PROVIDERS` lists the providers, their models and
their prices. With a budget, every request goes through the campaign ledger.
"""

import json
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, PrivateAttr, SecretStr

from agentrpg.agent_model import ProviderCallError
from agentrpg.budget import BudgetBinding, BudgetError, BudgetTransport, direct_provider

RETRYABLE = frozenset({408, 429, 500, 502, 503, 504, 529})


def _wire(messages) -> list[dict]:
    out = []
    for message in messages:
        text = message.content if isinstance(message.content, str) else json.dumps(message.content)
        if message.type == "system":
            out.append({"role": "system", "content": text})
        elif message.type == "human":
            out.append({"role": "user", "content": text})
        elif message.type == "tool":
            out.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": text})
        else:
            item = {"role": "assistant", "content": text}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                    }
                    for call in message.tool_calls
                ]
            out.append(item)
    return out


class DirectChatModel(BaseChatModel):
    """One model at its own provider. Graph execution uses the asynchronous interface."""

    endpoint: str
    provider_model: str
    key: SecretStr
    role: str = "model"
    # An optional counter with before(role), after(role, usage) and a failed mapping.
    meter: Any = None
    max_tokens: int = 4000
    temperature: float | None = None
    extra: dict = Field(default_factory=dict)
    tools: list = Field(default_factory=list)
    timeout: float = 120
    max_retries: int = 0
    json_format: bool = True
    _client: httpx.AsyncClient | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "direct-chat"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": [convert_to_openai_tool(tool) for tool in tools]})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("Direct models require the asynchronous interface.")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _failed(self) -> None:
        if self.meter is not None:
            self.meter.failed[self.role] = self.meter.failed.get(self.role, 0) + 1

    async def _post(self, body: dict) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.key.get_secret_value()}"}
        if self._client is not None:
            return await self._client.post(self.endpoint, json=body, headers=headers)
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            return await client.post(self.endpoint, json=body, headers=headers)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        body = {
            "model": self.provider_model,
            "messages": _wire(messages),
            "max_tokens": self.max_tokens,
            "stream": False,
            **self.extra,
        }
        if self.json_format and kwargs.get("response_format"):
            body["response_format"] = kwargs["response_format"]
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.tools:
            body["tools"] = self.tools
        for attempt in range(self.max_retries + 1):
            if self.meter is not None:
                self.meter.before(self.role)
                self.meter.first_request.setdefault(
                    self.role,
                    {
                        "messages": body["messages"],
                        "tools": [tool["function"]["name"] for tool in self.tools],
                    },
                )
            last = attempt == self.max_retries
            try:
                response = await self._post(body)
            except BudgetError:
                raise
            except httpx.HTTPError:
                self._failed()
                if last:
                    raise ProviderCallError(
                        "Model provider connection failed.", retryable=True
                    ) from None
                continue
            if response.status_code == 200:
                break
            self._failed()
            retryable = response.status_code in RETRYABLE
            if last or not retryable:
                raise ProviderCallError(
                    f"Model provider returned HTTP {response.status_code}.",
                    retryable=retryable,
                    status=response.status_code,
                )
        try:
            data = response.json()
            choice = data["choices"][0]
            reply = choice["message"]
            calls, invalid = [], []
            for call in reply.get("tool_calls") or []:
                name, raw = call["function"]["name"], call["function"].get("arguments") or "{}"
                try:
                    arguments = json.loads(raw)
                    if not isinstance(arguments, dict):
                        raise ValueError
                    calls.append(
                        {"name": name, "args": arguments, "id": call["id"], "type": "tool_call"}
                    )
                except ValueError:
                    invalid.append(
                        {
                            "name": name,
                            "args": raw,
                            "id": call.get("id"),
                            "error": "Arguments are not a JSON object.",
                            "type": "invalid_tool_call",
                        }
                    )
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            self._failed()
            raise ProviderCallError(
                "Model provider returned an invalid response.", status=0
            ) from None
        if self.meter is not None:
            self.meter.after(self.role, usage)
        message = AIMessage(
            content=reply.get("content") or "",
            tool_calls=calls,
            invalid_tool_calls=invalid,
            id=data.get("id"),
            response_metadata={
                "finish_reason": choice.get("finish_reason"),
                "model_name": self.provider_model,
            },
            usage_metadata={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def create_direct_model(
    api_key: str,
    model: str,
    *,
    max_tokens: int = 1000,
    timeout_seconds: float = 45,
    max_retries: int = 0,
    budget: BudgetBinding | None = None,
    budget_role: str = "model",
    temperature: float | None = 0,
    meter: Any = None,
) -> DirectChatModel:
    """Create one model for a "provider:model" name. The caller must call ``aclose``."""
    found = direct_provider(model)
    if found is None:
        raise ValueError("A direct model is written as provider:model, from the price table.")
    _, provider, own = found
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(f"Set {provider.key_name} in the local configuration.")
    if budget is not None and model not in budget.prices:
        raise BudgetError("The model has no verified campaign price.")
    result = DirectChatModel(
        endpoint=provider.url,
        provider_model=own,
        key=SecretStr(api_key),
        role=budget_role,
        meter=meter,
        max_tokens=max_tokens,
        temperature=temperature if provider.temperature else None,
        extra=dict(provider.fixed),
        timeout=timeout_seconds,
        max_retries=max_retries,
        json_format=provider.json_reply,
    )
    if budget is not None:
        result._client = httpx.AsyncClient(
            transport=BudgetTransport(budget, budget_role),
            follow_redirects=False,
            timeout=timeout_seconds,
            trust_env=False,
        )
    return result
