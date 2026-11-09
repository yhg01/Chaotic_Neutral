"""Durable cost reservations for every actual text-model HTTP attempt.

GPT-4.1 uses a byte-tokenizer content bound with explicit wrapper allowances.
Two pinned DeepSeek/Qwen text models reserve their full declared input context
instead of assuming that their tokenizers behave like GPT. The routed models use
the byte bound too: their contexts are so large that a full-context reservation
would refuse real work, and a cost above any reservation halts the campaign.
Unknown models, paid plugins, other reasoning options and media fail closed.
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
    "deepseek/deepseek-chat-v3-0324": "DeepSeek",
    "qwen/qwen3-30b-a3b-instruct-2507": "Qwen3",
}
# Model name, then its tokenizer and the multiple of the listed price that a provider may charge.
# The listed price is the cheapest route. Above it, another provider can answer when one is rate
# limited. Reservations use the same multiple, so a reported cost stays inside its reservation.
ROUTED_MODELS = {
    "deepseek/deepseek-v4.1-flash": ("DeepSeek", Decimal(3)),
    "qwen/qwen3.8-flash": ("Qwen", Decimal(1)),
    "anthropic/claude-opus-5.5": ("Claude", Decimal(1)),
}
# These think by default, and thinking counts toward max_tokens. A measured 800-token JSON call
# on a flash model returned no content at all, so campaign requests turn it off.
NO_REASONING_MODELS = frozenset({"deepseek/deepseek-v4.1-flash", "qwen/qwen3.8-flash"})
# No provider of this model accepts temperature, and require_parameters then refuses the call.
NO_TEMPERATURE_MODELS = frozenset({"anthropic/claude-opus-5.5"})
SUPPORTED_MODELS = frozenset(
    {
        "openai/gpt-4.1",
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
        *CONTEXT_BOUND_MODELS,
        *ROUTED_MODELS,
    }
)
_context: ContextVar[dict | None] = ContextVar("model_budget_context", default=None)
_QUANTUM = Decimal("0.000000000001")
# The provider said no before any model ran, so nothing was spent. A 5xx stays reserved: the
# provider may have started the work. Nothing here depends on the body of the response.
REFUSED_STATUSES = frozenset({400, 401, 402, 403, 404, 429})
# How long a request waits for outstanding attempts before a refusal stands. A row older than
# the age limit belongs to a stopped process, so it will not finish and is not waited for.
RESERVE_WAIT_SECONDS = 60.0
RESERVE_POLL_SECONDS = 1.0
OUTSTANDING_AGE_SECONDS = 300


class BudgetError(RuntimeError):
    """A safe error that contains no provider request or credential."""


class BudgetExceeded(BudgetError):
    """The campaign cannot reserve another provider attempt."""


class _CeilingReached(BudgetExceeded):
    """Committed money leaves no room. Attempts in flight can still give some back."""


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
        if self.model in ROUTED_MODELS:
            result["route_price_factor"] = str(ROUTED_MODELS[self.model][1])
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
            routed = ROUTED_MODELS.get(model["id"])
            expected_tokenizer = (
                routed[0] if routed else CONTEXT_BOUND_MODELS.get(model["id"], "GPT")
            )
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
            if routed and not {"tools", "tool_choice", "response_format"} <= set(
                model.get("supported_parameters", [])
            ):
                raise BudgetError("The routed model lacks the required tool contract.")
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
            factor = routed[1] if routed else Decimal(1)
            result[model["id"]] = ModelPrice(
                model["id"],
                prompt * factor,
                completion * factor,
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
        self, campaign_id, price, prompt_bound, output_bound, *, role="model", wait_seconds=0.0
    ):
        """Reserve one attempt. With wait_seconds, a refusal first waits for attempts in flight.

        A reservation is far above what a call costs, so finished calls usually free room.
        No connection is held during the wait. The ceiling is checked again each time.
        """
        deadline = None
        while True:
            try:
                return await self._reserve_once(
                    campaign_id, price, prompt_bound, output_bound, role
                )
            except _CeilingReached:
                now = asyncio.get_running_loop().time()
                deadline = now + wait_seconds if deadline is None else deadline
                if now >= deadline:
                    raise
                if not await self._outstanding(campaign_id):
                    # The last one can have finished since the check above. This try decides.
                    return await self._reserve_once(
                        campaign_id, price, prompt_bound, output_bound, role
                    )
            await asyncio.sleep(RESERVE_POLL_SECONDS)

    async def _outstanding(self, campaign_id) -> int:
        async with self.pool.connection() as conn:
            found = await (
                await conn.execute(
                    """SELECT count(*) AS n FROM model_spend_attempts
                       WHERE campaign_id=%s AND status='reserved'
                       AND created_at > clock_timestamp() - make_interval(secs => %s)""",
                    (campaign_id, OUTSTANDING_AGE_SECONDS),
                )
            ).fetchone()
        return found["n"]

    async def _reserve_once(self, campaign_id, price, prompt_bound, output_bound, role):
        cost = price.maximum(prompt_bound, output_bound)
        meta = _context.get() or {}
        attempt_id = uuid4()
        async with self.pool.connection() as conn, conn.transaction():
            campaign = await (
                await conn.execute(
                    "SELECT * FROM model_campaigns WHERE id=%s FOR UPDATE", (campaign_id,)
                )
            ).fetchone()
            if campaign is None or campaign["halted"]:
                raise BudgetExceeded("The model campaign is absent or halted.")
            committed = await (
                await conn.execute(
                    """SELECT COALESCE(sum(CASE WHEN status='reported' THEN reported_usd
                   ELSE GREATEST(reserved_usd, COALESCE(reported_usd,0)) END),0) AS total
                   FROM model_spend_attempts WHERE campaign_id=%s""",
                    (campaign_id,),
                )
            ).fetchone()
            if committed["total"] + cost > campaign["limit_usd"]:
                raise _CeilingReached("The campaign budget cannot cover this provider attempt.")
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
                    role if role != "model" else meta.get("role", role),
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
        return {
            **campaign,
            **totals,
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
        "reasoning",
        "stream",
        "provider",
        "usage",
        "frequency_penalty",
        "presence_penalty",
        "user",
    }
    if set(body) - allowed or body.get("stream") is True:
        raise BudgetError("The request contains an unsupported billing option.")
    if body.get("reasoning") not in (None, {"enabled": False}):
        raise BudgetError("The request contains an unsupported billing option.")
    body.pop("reasoning", None)
    if model in NO_REASONING_MODELS:
        body["reasoning"] = {"enabled": False}
    if model in NO_TEMPERATURE_MODELS:
        body.pop("temperature", None)
    output = body.get("max_tokens")
    if type(output) is not int or not 1 <= output <= 16384:
        raise BudgetError("The request has no supported output token limit.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BudgetError("The request has no text messages.")
    for message in messages:
        # Thinking from an earlier reply is not needed again. It would only add input cost.
        if model in ROUTED_MODELS:
            message.pop("reasoning", None)
            message.pop("reasoning_details", None)
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
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name", "refusal"}:
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
        if model not in ROUTED_MODELS:
            raise BudgetError(
                "The conservative request token bound exceeds the model context limit."
            )
        # The byte estimate adds 1,024 tokens for each message, so a long history of short tool
        # results passed the context while the real prompt was a tenth of it. A real prompt
        # cannot exceed the context, and a provider refuses one that does, at no cost.
        bound = price.context_length - output
    body["provider"] = {
        "require_parameters": True,
        "allow_fallbacks": model in ROUTED_MODELS,
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
            wait_seconds=RESERVE_WAIT_SECONDS,
        )
        try:
            response = await self.transport.handle_async_request(bounded)
            await response.aread()
            cost, generation = None, None
            reason = "Provider charge is not confirmed; maximum retained."
            if response.status_code in REFUSED_STATUSES:
                cost, reason = 0, "The provider refused the request, so no model ran."
            elif response.is_success:
                try:
                    result = json.loads(response.content, parse_float=Decimal)
                    cost = amount(result["usage"]["cost"])
                    generation = result.get("id")
                    reason = None
                except (KeyError, TypeError, ValueError, BudgetError):
                    pass
            await self.binding.ledger.finish(
                attempt, reported=cost, generation_id=generation, reason=reason
            )
            return response
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # No connection was made, so the request never left this machine.
            await self._close_attempt(
                attempt, reported=0, reason="The connection failed before the request was sent."
            )
            raise
        except BaseException:
            # A request may have reached the provider. Never release its reserve.
            await self._close_attempt(attempt, reason="Request did not complete; maximum retained.")
            raise

    async def _close_attempt(self, attempt, **fields):
        try:
            await asyncio.shield(self.binding.ledger.finish(attempt, **fields))
        except Exception:
            pass  # The committed reservation itself remains durable.

    async def aclose(self):
        await self.transport.aclose()


class NoSyncBudgetTransport(httpx.BaseTransport):
    def handle_request(self, request):
        raise BudgetError("Budgeted models require the asynchronous interface.")
