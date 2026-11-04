"""Local model configuration and usage accounting for personal Agents."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import httpx
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_openrouter import ChatOpenRouter
from langsmith import tracing_context
from openrouter import OpenRouter
from openrouter.errors.openroutererror import OpenRouterError
from openrouter.utils.logger import NoOpLogger
from pydantic import PrivateAttr

from agentrpg.budget import BudgetBinding, BudgetError, BudgetTransport, NoSyncBudgetTransport


class ProviderCallError(RuntimeError):
    """A provider failure with no response body or credential in its message."""


@contextmanager
def local_model_calls() -> Iterator[None]:
    """Disable automatic LangSmith tracing in this task and its child tasks."""
    with tracing_context(enabled=False, parent=False):
        yield


class LocalChatOpenRouter(ChatOpenRouter):
    """Use explicit retry counts and safe errors with the official integration.

    The upstream adapter treats max_retries as a time budget. Its timeout is in
    milliseconds. The factory supplies an SDK client with retries disabled, so
    this class controls the count. Graph execution uses the async interface.
    """

    _budget_clients: list[Any] = PrivateAttr(default_factory=list)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        for attempt in range(self.max_retries + 1):
            try:
                return await super()._agenerate(messages, stop, run_manager, **kwargs)
            except BudgetError:
                raise
            except OpenRouterError as error:
                status = error.status_code
                retryable = status in {408, 429, 500, 502, 503, 504, 529}
                if not retryable or attempt == self.max_retries:
                    raise ProviderCallError(f"Model provider returned HTTP {status}.") from None
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise ProviderCallError("Model provider connection failed.") from None
            except Exception:
                raise ProviderCallError("Model provider returned an invalid response.") from None
            await asyncio.sleep(0.5 * 2**attempt)
        raise ProviderCallError("Model provider is unavailable.")

    async def aclose(self) -> None:
        """Close both HTTP clients created by the provider SDK."""
        await self.client.__aexit__(None, None, None)
        self.client.__exit__(None, None, None)
        for client in self._budget_clients:
            if isinstance(client, httpx.AsyncClient):
                await client.aclose()
            else:
                client.close()


def create_model(
    api_key: str,
    model: str = "openai/gpt-4.1-mini",
    *,
    max_tokens: int = 1000,
    max_output_limit: int = 4096,
    timeout_seconds: int = 45,
    max_retries: int = 2,
    budget: BudgetBinding | None = None,
    budget_role: str = "model",
) -> LocalChatOpenRouter:
    """Create one bounded model client. The caller must call ``aclose``."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("Set OPENROUTER_API_KEY in the local configuration.")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("Set a valid model name.")
    if (
        not isinstance(max_output_limit, int)
        or isinstance(max_output_limit, bool)
        or not 4096 <= max_output_limit <= 16384
    ):
        raise ValueError("Explicit output limit must be from 4096 through 16384 tokens.")
    if not 1 <= max_tokens <= max_output_limit or not 1 <= timeout_seconds <= 120:
        raise ValueError("Model token or timeout limit is outside the allowed range.")
    if not 0 <= max_retries <= 2:
        raise ValueError("Model retries must be between zero and two.")
    http_clients = {}
    if budget is not None:
        if model.strip() not in budget.prices:
            raise BudgetError("The model has no verified campaign price.")
        http_clients = {
            "client": httpx.Client(transport=NoSyncBudgetTransport(), follow_redirects=False),
            "async_client": httpx.AsyncClient(
                transport=BudgetTransport(budget, budget_role),
                follow_redirects=False,
                timeout=timeout_seconds,
            ),
        }
    client = OpenRouter(
        api_key=api_key,
        server_url="https://openrouter.ai/api/v1",
        timeout_ms=timeout_seconds * 1000,
        retry_config=None,
        debug_logger=NoOpLogger(),
        **http_clients,
    )
    result = LocalChatOpenRouter(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        client=client,
        model=model.strip(),
        max_tokens=max_tokens,
        timeout=timeout_seconds * 1000,
        max_retries=max_retries,
        temperature=0,
        streaming=False,
        disable_streaming=True,
        verbose=False,
        callbacks=[],
        session_id=None,
        trace=None,
        openrouter_provider={"require_parameters": True},
    )
    result._budget_clients = list(http_clients.values())
    return result


class UsageTracker(AsyncCallbackHandler):
    """Count concurrent model invocations without retaining prompts or errors.

    Each invocation can include bounded transport retries. Token and cost counts
    include only successful responses reported by the provider. Methods contain
    no awaits, so one shared event loop updates the counters without a mutex.
    """

    run_inline = True

    def __init__(self) -> None:
        self.requests = 0
        self.completed = 0
        self.failed = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.reported_cost_usd = 0.0
        self.cost_reports = 0
        self.peak_in_flight = 0
        self.models: set[str] = set()
        self._active: set[UUID] = set()

    async def on_chat_model_start(
        self, serialized, messages, *, run_id: UUID, **kwargs: Any
    ) -> None:
        if run_id in self._active:
            return
        self._active.add(run_id)
        self.requests += 1
        self.peak_in_flight = max(self.peak_in_flight, len(self._active))

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        if run_id not in self._active:
            return
        self._active.remove(run_id)
        self.completed += 1
        model = (response.llm_output or {}).get("model_name")
        if isinstance(model, str):
            self.models.add(model)
        for generations in response.generations:
            # n=1 in the model factory. Token usage covers the complete response.
            if not generations:
                continue
            message = getattr(generations[0], "message", None)
            if message is None:
                continue
            usage = message.usage_metadata or {}
            self.input_tokens += int(usage.get("input_tokens") or 0)
            self.output_tokens += int(usage.get("output_tokens") or 0)
            self.total_tokens += int(usage.get("total_tokens") or 0)
            metadata = message.response_metadata
            model = metadata.get("model_name")
            if isinstance(model, str):
                self.models.add(model)
            cost = metadata.get("cost")
            if isinstance(cost, (int, float)):
                self.reported_cost_usd += cost
                self.cost_reports += 1

    async def on_llm_error(self, error, *, run_id: UUID, **kwargs: Any) -> None:
        if run_id in self._active:
            self._active.remove(run_id)
            self.failed += 1

    def summary(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "completed_requests": self.completed,
            "failed_requests": self.failed,
            "peak_concurrent_requests": self.peak_in_flight,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "reported_cost_usd": round(self.reported_cost_usd, 8),
            "cost_reports": self.cost_reports,
            "models": sorted(self.models),
            "request_unit": "logical model invocation; excludes internal HTTP retries",
        }
