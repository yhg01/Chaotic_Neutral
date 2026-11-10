"""Durable cost reservations for every actual text-model HTTP attempt.

GPT-4.1 uses a byte-tokenizer content bound with explicit wrapper allowances.
Explicitly supported DeepSeek/Qwen text models reserve their full declared input context
instead of assuming that their tokenizers behave like GPT. Unknown models,
paid plugins, reasoning options and media fail closed.
OpenRouter enforces the stored provider price ceilings on each request.
"""

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb

CONTEXT_BOUND_MODELS = {
    "deepseek/deepseek-v3.2": "DeepSeek",
    "deepseek/deepseek-chat-v3-0324": "DeepSeek",
    "qwen/qwen3-30b-a3b-instruct-2507": "Qwen3",
}
SUPPORTED_MODELS = frozenset(
    {"openai/gpt-4.1", "openai/gpt-4.1-mini", "openai/gpt-4.1-nano", *CONTEXT_BOUND_MODELS}
)
_context: ContextVar[dict | None] = ContextVar("model_budget_context", default=None)
_QUANTUM = Decimal("0.000000000001")


class BudgetError(RuntimeError):
    """A safe error that contains no provider request or credential."""


class BudgetExceeded(BudgetError):
    """The campaign cannot reserve another provider attempt."""


class ProviderResponseError(RuntimeError):
    """A numeric provider error in an HTTP-success body, without private text."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Model provider reported error {status_code} inside HTTP 200.")


def amount(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BudgetError("Model price or cost is not a valid nonnegative decimal.")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise BudgetError("Model price or cost is not a valid nonnegative decimal.") from None
    if not result.is_finite() or result < 0 or result > Decimal("1000000"):
        raise BudgetError("Model price or cost is not a valid nonnegative decimal.")
    return result


@contextmanager
def budget_context(*, run_id=None, owner_id=None, role=None) -> Iterator[None]:
    values = dict(_context.get() or {})
    for key, value in (("run_id", run_id), ("owner_id", owner_id), ("role", role)):
        if value is not None:
            values[key] = str(value)
    token = _context.set(values)
    try:
        yield
    finally:
        _context.reset(token)


@dataclass(frozen=True)
class ModelPrice:
    model: str
    prompt: Decimal
    completion: Decimal
    request: Decimal
    context_length: int
    fetched_at: str

    def __post_init__(self):
        if self.model not in SUPPORTED_MODELS:
            raise BudgetError("This model does not have a supported text-token cost bound.")
        for key in ("prompt", "completion", "request"):
            object.__setattr__(self, key, amount(getattr(self, key)))
        if type(self.context_length) is not int or not 1 <= self.context_length <= 2_000_000:
            raise BudgetError("Model context limit is missing or unsupported.")

    def snapshot(self):
        result = {
            "model": self.model,
            "prompt_per_token_usd": str(self.prompt),
            "completion_per_token_usd": str(self.completion),
            "per_request_usd": str(self.request),
            "context_length": self.context_length,
            "fetched_at": self.fetched_at,
            "source": "https://openrouter.ai/api/v1/models",
        }
        if self.model in CONTEXT_BOUND_MODELS:
            result["input_bound_strategy"] = "full_declared_context"
        return result

    def maximum(self, prompt_tokens, output_tokens):
        return (
            self.prompt * prompt_tokens + self.completion * output_tokens + self.request
        ).quantize(_QUANTUM, rounding=ROUND_CEILING)

    def provider_limit(self):
        return {
            "prompt": float(self.prompt * 1_000_000),
            "completion": float(self.completion * 1_000_000),
            "request": float(self.request),
            "image": 0,
        }


async def fetch_model_prices(models, *, client: httpx.AsyncClient | None = None):
    """Read current public prices. This endpoint does not make a model call."""
    wanted = set(models)
    if not wanted or not wanted <= SUPPORTED_MODELS:
        raise BudgetError("The campaign contains an unsupported model.")
    owned = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
    try:
        response = await client.get("https://openrouter.ai/api/v1/models")
        response.raise_for_status()
        data = response.json()["data"]
        result = {}
        for model in data:
            if model.get("id") not in wanted:
                continue
            architecture = model.get("architecture", {})
            expected_tokenizer = CONTEXT_BOUND_MODELS.get(model["id"], "GPT")
            if architecture.get("tokenizer") != expected_tokenizer or architecture.get(
                "output_modalities"
            ) != ["text"]:
                raise BudgetError("The model does not have the expected text-token contract.")
            if "max_tokens" not in model.get("supported_parameters", []):
                raise BudgetError("The model does not support a fixed output limit.")
            if model["id"] in CONTEXT_BOUND_MODELS and (
                architecture.get("input_modalities") != ["text"]
                or not {"tools", "tool_choice", "response_format"}
                <= set(model.get("supported_parameters", []))
            ):
                raise BudgetError("The comparison model lacks the required text/tool contract.")
            pricing = model["pricing"]
            levels = [pricing, *pricing.get("overrides", [])]
            prompt = max(amount(level.get("prompt", pricing["prompt"])) for level in levels)
            completion = max(
                amount(level.get("completion", pricing["completion"])) for level in levels
            )
            # Cache prices can differ from ordinary input. Reserve the maximum.
            prompt = max(
                prompt,
                *(
                    amount(level.get(key, 0))
                    for level in levels
                    for key in ("input_cache_write", "input_cache_read")
                ),
            )
            result[model["id"]] = ModelPrice(
                model["id"],
                prompt,
                completion,
                max(amount(level.get("request", pricing.get("request", 0))) for level in levels),
                model["context_length"],
                datetime.now(UTC).isoformat(),
            )
        if set(result) != wanted:
            raise BudgetError("Current prices are unavailable for a campaign model.")
        return result
    except BudgetError:
        raise
    except Exception:
        raise BudgetError("Current model prices could not be verified.") from None
    finally:
        if owned:
            await client.aclose()


class CampaignLedger:
    def __init__(self, pool):
        self.pool = pool

    async def ensure_campaign(self, campaign_id, limit_usd, *, name="Environment test"):
        campaign_id = UUID(str(campaign_id))
        limit = amount(limit_usd)
        if limit <= 0 or not isinstance(name, str) or not 1 <= len(name) <= 160:
            raise BudgetError("Campaign name or budget is invalid.")
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO model_campaigns(id,name,limit_usd) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (campaign_id, name, limit),
            )
            row = await (
                await conn.execute(
                    "SELECT limit_usd FROM model_campaigns WHERE id=%s", (campaign_id,)
                )
            ).fetchone()
            if row["limit_usd"] != limit:
                raise BudgetError("The saved campaign budget differs from the requested budget.")
        return await self.summary(campaign_id)

    async def reserve(
        self,
        campaign_id,
        price,
        prompt_bound,
        output_bound,
        *,
        role="model",
        max_role_attempts=None,
    ):
        if max_role_attempts is not None and (
            type(max_role_attempts) is not int or not 1 <= max_role_attempts <= 100
        ):
            raise BudgetError("The role attempt limit must be from one through 100.")
        cost = price.maximum(prompt_bound, output_bound)
        meta = _context.get() or {}
        role = role if role != "model" else meta.get("role", role)
        attempt_id = uuid4()
        async with self.pool.connection() as conn, conn.transaction():
            campaign = await (
                await conn.execute(
                    "SELECT * FROM model_campaigns WHERE id=%s FOR UPDATE", (campaign_id,)
                )
            ).fetchone()
            if campaign is None or campaign["halted"]:
                raise BudgetExceeded("The model campaign is absent or halted.")
            if max_role_attempts is not None:
                used = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM model_spend_attempts "
                        "WHERE campaign_id=%s AND role=%s",
                        (campaign_id, role),
                    )
                ).fetchone()
                if used["n"] >= max_role_attempts:
                    raise BudgetExceeded("The saved role attempt limit is exhausted.")
            committed = await (
                await conn.execute(
                    """SELECT COALESCE(sum(CASE WHEN status='reported' THEN reported_usd
                   ELSE GREATEST(reserved_usd, COALESCE(reported_usd,0)) END),0) AS total
                   FROM model_spend_attempts WHERE campaign_id=%s""",
                    (campaign_id,),
                )
            ).fetchone()
            opening = await (
                await conn.execute(
                    "SELECT committed_usd FROM model_campaign_opening_balances "
                    "WHERE campaign_id=%s",
                    (campaign_id,),
                )
            ).fetchone()
            historical = opening["committed_usd"] if opening else Decimal(0)
            if historical + committed["total"] + cost > campaign["limit_usd"]:
                raise BudgetExceeded("The campaign budget cannot cover this provider attempt.")
            await conn.execute(
                """INSERT INTO model_spend_attempts
                (id,campaign_id,run_id,owner_id,role,model,reserved_usd,status,
                 prompt_token_bound,output_token_bound,price_snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s,%s)""",
                (
                    attempt_id,
                    campaign_id,
                    meta.get("run_id"),
                    meta.get("owner_id"),
                    role,
                    price.model,
                    cost,
                    prompt_bound,
                    output_bound,
                    Jsonb(price.snapshot()),
                ),
            )
        return attempt_id

    async def finish(self, attempt_id, *, reported=None, generation_id=None, reason=None):
        cost = None if reported is None else amount(reported)
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT * FROM model_spend_attempts WHERE id=%s FOR UPDATE", (attempt_id,)
                )
            ).fetchone()
            if row is None:
                raise BudgetError("Model spend reservation is absent.")
            if row["status"] not in {"reserved", "uncertain"}:
                return
            status = "uncertain" if cost is None else "reported"
            if cost is not None and cost > row["reserved_usd"]:
                status, reason = "breach", "Provider cost exceeded the reserved maximum."
                await conn.execute(
                    "UPDATE model_campaigns SET halted=true WHERE id=%s", (row["campaign_id"],)
                )
            await conn.execute(
                """UPDATE model_spend_attempts SET status=%s,reported_usd=%s,generation_id=%s,
                    reason=%s,finished_at=clock_timestamp() WHERE id=%s""",
                (
                    status,
                    cost,
                    str(generation_id)[:200] if generation_id else None,
                    reason,
                    attempt_id,
                ),
            )
        if status == "breach":
            raise BudgetError("Provider cost exceeded its maximum. The campaign is halted.")

    async def summary(self, campaign_id, *, recent_limit=50):
        async with self.pool.connection() as conn:
            campaign = await (
                await conn.execute("SELECT * FROM model_campaigns WHERE id=%s", (campaign_id,))
            ).fetchone()
            if campaign is None:
                raise BudgetError("Campaign not found.")
            totals = await (
                await conn.execute(
                    """SELECT count(*) AS attempts,
                COALESCE(sum(reported_usd),0) AS reported_usd,
                COALESCE(sum(reserved_usd) FILTER (WHERE status='reserved'),0) AS reserved_usd,
                COALESCE(sum(GREATEST(reserved_usd,COALESCE(reported_usd,0)))
                    FILTER (WHERE status IN ('uncertain','breach')),0) AS uncertain_usd,
                COALESCE(sum(CASE WHEN status='reported' THEN reported_usd
                    ELSE GREATEST(reserved_usd,COALESCE(reported_usd,0)) END),0) AS committed_usd
                FROM model_spend_attempts WHERE campaign_id=%s""",
                    (campaign_id,),
                )
            ).fetchone()
            recent = await (
                await conn.execute(
                    """SELECT id,run_id,owner_id,role,model,status,reserved_usd,reported_usd,
                generation_id,reason,created_at,finished_at FROM model_spend_attempts
                WHERE campaign_id=%s ORDER BY created_at DESC,id DESC LIMIT %s""",
                    (campaign_id, min(max(recent_limit, 0), 100)),
                )
            ).fetchall()
            opening = await (
                await conn.execute(
                    "SELECT * FROM model_campaign_opening_balances WHERE campaign_id=%s",
                    (campaign_id,),
                )
            ).fetchone()
        carried = {}
        if opening:
            carried = {"opening_balance": opening, "local_attempts": totals["attempts"]}
            for key in (
                "attempts",
                "reported_usd",
                "reserved_usd",
                "uncertain_usd",
                "committed_usd",
            ):
                totals[key] += opening[key]
        return {
            **campaign,
            **totals,
            **carried,
            "remaining_usd": max(Decimal(0), campaign["limit_usd"] - totals["committed_usd"]),
            "recent_attempts": recent,
            "accounting": (
                "Reported cost plus full reservations for outstanding or uncertain attempts."
            ),
        }


@dataclass(frozen=True)
class BudgetBinding:
    ledger: CampaignLedger
    campaign_id: UUID
    prices: Mapping[str, ModelPrice]
    max_role_attempts: int | None = None


def prepare_request(body: dict, prices):
    model = body.get("model")
    if model not in prices or model not in SUPPORTED_MODELS:
        raise BudgetError("The request model has no verified campaign price.")
    price = prices[model]
    allowed = {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
        "seed",
        "response_format",
        "stream",
        "provider",
        "usage",
        "frequency_penalty",
        "presence_penalty",
        "user",
    }
    if model == "deepseek/deepseek-v3.2" and "reasoning" in body:
        # Only an explicit opt-out is supported. No paid thinking configuration.
        if body["reasoning"] != {"effort": "none"}:
            raise BudgetError("The request contains an unsupported billing option.")
        allowed.add("reasoning")
    if set(body) - allowed or body.get("stream") is True:
        raise BudgetError("The request contains an unsupported billing option.")
    output = body.get("max_tokens")
    if type(output) is not int or not 1 <= output <= 16384:
        raise BudgetError("The request has no supported output token limit.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BudgetError("The request has no text messages.")
    for message in messages:
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            if not isinstance(content, list) or any(
                not isinstance(part, dict)
                or set(part) - {"type", "text"}
                or part.get("type") != "text"
                or not isinstance(part.get("text"), str)
                for part in content
            ):
                raise BudgetError("Campaign requests support text only.")
        message_fields = {"role", "content", "tool_calls", "tool_call_id", "name", "refusal"}
        if model == "deepseek/deepseek-v3.2" and message.get("role") == "assistant":
            # Some providers return these even after an explicit reasoning opt-out.
            # They are saved text input, covered by the full-context reservation;
            # they do not activate an additional reasoning or billing option.
            message_fields.update({"reasoning", "reasoning_details"})
            if "reasoning" in message and not isinstance(message["reasoning"], str):
                raise BudgetError("Historical reasoning must contain text only.")
            if "reasoning_details" in message:
                details = message["reasoning_details"]
                if not isinstance(details, list):
                    raise BudgetError("Historical reasoning must contain text only.")
                for item in details:
                    if (
                        not isinstance(item, dict)
                        or set(item) - {"type", "text", "summary", "format", "index", "id"}
                        or item.get("type") not in {"reasoning.text", "reasoning.summary"}
                        or any(not isinstance(v, str) for k, v in item.items() if k != "index")
                        or (
                            "index" in item
                            and (type(item["index"]) is not int or item["index"] < 0)
                        )
                    ):
                        raise BudgetError("Historical reasoning must contain text only.")
        if set(message) - message_fields:
            raise BudgetError("The message contains an unsupported billing option.")
    tools = body.get("tools", [])
    if not isinstance(tools, list) or any(tool.get("type") != "function" for tool in tools):
        raise BudgetError("Campaign requests support local function tools only.")
    serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # At most one byte-level token per content byte, doubled for provider tool
    # formatting. Add explicit room for each message/tool and fixed wrappers.
    bound = len(serialized) * 2 + 8192 + 1024 * (len(messages) + len(tools))
    if model in CONTEXT_BOUND_MODELS:
        # A successful request's prompt cannot exceed the declared model context.
        # Reserve the whole input context plus the bounded output independently.
        # This deliberately over-reserves and does not estimate a foreign tokenizer.
        bound = price.context_length
        if output > price.context_length:
            raise BudgetError("The output limit exceeds the model context limit.")
    elif bound + output > price.context_length:
        raise BudgetError("The conservative request token bound exceeds the model context limit.")
    body["provider"] = {
        "require_parameters": True,
        "allow_fallbacks": False,
        "max_price": price.provider_limit(),
    }
    body["usage"] = {"include": True}
    return price, bound, output


class BudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, binding, role, *, transport=None):
        self.binding, self.role = binding, role
        self.transport = transport or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if (
            request.method != "POST"
            or str(request.url) != "https://openrouter.ai/api/v1/chat/completions"
        ):
            raise BudgetError("Campaign provider request target is unsupported.")
        try:
            body = json.loads(await request.aread())
            price, prompt_bound, output_bound = prepare_request(body, self.binding.prices)
        except BudgetError:
            raise
        except Exception:
            raise BudgetError("Campaign provider request could not be validated.") from None
        data = json.dumps(body, separators=(",", ":")).encode()
        headers = dict(request.headers)
        headers["content-length"] = str(len(data))
        bounded = httpx.Request(
            request.method,
            request.url,
            headers=headers,
            content=data,
            extensions=request.extensions,
        )
        attempt = await self.binding.ledger.reserve(
            self.binding.campaign_id,
            price,
            prompt_bound,
            output_bound,
            role=self.role,
            **(
                {"max_role_attempts": self.binding.max_role_attempts}
                if self.binding.max_role_attempts is not None
                else {}
            ),
        )
        try:
            response = await self.transport.handle_async_request(bounded)
            await response.aread()
            cost, generation, provider_error = None, None, None
            if response.is_success:
                try:
                    result = json.loads(response.content, parse_float=Decimal)
                    generation = result.get("id")
                    error = result.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    if type(code) is int and 400 <= code <= 599:
                        provider_error = code
                    cost = amount(result["usage"]["cost"])
                except (AttributeError, KeyError, TypeError, ValueError, BudgetError):
                    pass
            await self.binding.ledger.finish(
                attempt,
                reported=cost,
                generation_id=generation,
                reason=None
                if cost is not None
                else (
                    f"Provider charge is not confirmed (HTTP {response.status_code}"
                    + (f", provider error {provider_error}" if provider_error else "")
                    + "); "
                    "maximum retained."
                ),
            )
        except BaseException as error:
            # A request may have reached the provider. Never release its reserve.
            try:
                await asyncio.shield(
                    self.binding.ledger.finish(
                        attempt,
                        reason=(
                            f"Request did not complete ({type(error).__name__}); maximum retained."
                        ),
                    )
                )
            except Exception:
                pass  # The committed reservation itself remains durable.
            raise
        if provider_error is not None:
            raise ProviderResponseError(provider_error)
        return response

    async def aclose(self):
        await self.transport.aclose()


class NoSyncBudgetTransport(httpx.BaseTransport):
    def handle_request(self, request):
        raise BudgetError("Budgeted models require the asynchronous interface.")
