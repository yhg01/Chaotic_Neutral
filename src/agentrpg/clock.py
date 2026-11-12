"""Event clock arithmetic: prices, stamps, pop order, wake rules, and the durable queue.

Everything above the async helpers is a pure function of saved inputs, so a rerun
computes the same story time. Wall-clock time, provider latency, and retries never
enter these functions. docs/EVENT_CLOCK.md holds the rules that are cited here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from agentrpg.agent_surface import internal_tool_name

# Event classes at equal time (docs/EVENT_CLOCK.md). Class 2 lives in memory, in the gate.
EVENT_CLASSES = {
    "director_plan": 0,
    "plan": 0,
    "cue": 0,
    "beat": 0,
    "perturbation": 0,
    "actor_effect": 1,
    "agent_tool": 2,
    "agent_finish": 3,
    "boundary": 4,
    "lifecycle_check": 5,
    "action_end": 6,
    "actor_wake": 7,
    "agent_wake": 8,
    "director_review": 9,
    "run_end": 10,
}
AGENT_TOOL_CLASS = EVENT_CLASSES["agent_tool"]

# These read no world state. Their time is the thought that asked for them, so they cost
# nothing and never park at the gate.
FREE_TOOLS = frozenset(
    {
        "end_turn",
        "finish_activation",
        "wait_for_mail",
        "wait_for_owner",
        "write_todos",
        "ls",
        "glob",
        "grep",
        "read_file",
        "write_file",
        "edit_file",
    }
)

# Seconds before a person notices a message while they are reachable or free (R9).
NOTICE_MIN_S = 240
NOTICE_MAX_S = 2400
# Chat-only exchanges allowed before an owner stops being woken by further messages.
CHAT_STREAK_LIMIT = 2
# Only these do something for the owner, so only these restart the count of chat-only messages.
EFFECTFUL_TOOLS = frozenset(
    {
        "send_mail",
        "submit_form",
        "write_resource",
        "create_resource",
        "submit_world_action",
        "submit_market_action",
        "transfer_credits",
        "post_thread",
        "reply",
        "direct_message",
    }
)
# These submit_form actions only read a page, so they do not count as doing something.
READ_ONLY_FORM_ACTIONS = frozenset(
    {"search", "view_cart", "order_status", "view_listings", "view_bills"}
)
# Activations caused only by other assistants before their messages stop waking this one at
# once. After that they wait, coalesced, as ambient news does (R8).
PEER_WAKE_LIMIT = 3
# The shortest wait an assistant can ask for, whatever the scenario's lower bound (R8).
MIN_WAKE_S = 20 * 60

# A drawn perturbation has fired when its event was popped: an owner who ends the action first
# stops it, however late it would have been. It has an effect when its saved plan changed
# something. Reports count drawn, fired and with-effect separately. Alias d, a, e, p.
FIRED_DRAWS = """FROM clock_draws d
    LEFT JOIN clock_actions a ON a.run_id=d.run_id AND a.id=d.action_id
    LEFT JOIN clock_events e ON e.run_id=d.run_id AND e.key='perturb:'||a.key
    LEFT JOIN run_events p
      ON p.run_id=d.run_id AND p.operation_key='director_plan:perturb:'||a.key"""
DRAW_HAS_EFFECT = """(p.payload#>>'{summary,replace_action}' IS NOT NULL OR EXISTS (
    SELECT 1 FROM unnest(ARRAY['cues','mail','records','record_updates','balance','sites',
        'site_updates','events','cancelled','retimed']) AS effect(name)
    WHERE jsonb_array_length(p.payload#>ARRAY['summary',effect.name]) > 0))"""

# Wake rules (R8). Direct events wake an Agent at once. Ambient events wake it after the
# ambient delay, coalesced by the single queued wake. Own effects never wake it.
AGENT_DIRECT_KINDS = frozenset(
    {
        "owner_message",
        "inbox_received",
        "reservation_settled",
        "market_settled",
        "consent_changed",
        "commerce_consent_changed",
    }
)
AGENT_AMBIENT_KINDS = frozenset(
    {"board_posted", "market_changed", "world_changed", "resource_updated"}
)
AGENT_KINDS = AGENT_DIRECT_KINDS | AGENT_AMBIENT_KINDS

# Cues arrive at once. Messages arrive after a notice delay. Only these kinds wake an owner.
# A record change or a board post never wakes a person. The board is never in their view (R13).
ACTOR_CUE_KINDS = frozenset({"actor_cue"})
ACTOR_MESSAGE_KINDS = frozenset(
    {
        "owner_message",
        "inbox_received",
        "actor_effect_rejected",
        "market_settled",
        "reservation_settled",
    }
)
ACTOR_OBSERVATION_KINDS = frozenset(
    {"reservation_settled", "world_changed", "market_settled", "resource_updated", "inbox_received"}
)
ACTOR_KINDS = ACTOR_CUE_KINDS | ACTOR_MESSAGE_KINDS | ACTOR_OBSERVATION_KINDS

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_AT_PATTERN = re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2}):(\d{2})$")


class ClockError(ValueError):
    """A clock input that cannot be used."""


def agent_process(owner_id: str) -> str:
    return f"agent:{owner_id}"


def actor_process(owner_id: str) -> str:
    return f"actor:{owner_id}"


def parse_start(value: str) -> datetime:
    """Parse a start time. It must name its UTC offset, so story time has one meaning."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ClockError("start_at must be an ISO 8601 time.") from None
    if parsed.utcoffset() is None:
        raise ClockError("start_at must name its UTC offset.")
    return parsed


def parse_daily(value: str) -> int:
    """Return minutes after midnight for an HH:MM value."""
    match = re.fullmatch(r"(\d{2}):(\d{2})", value) if isinstance(value, str) else None
    if match is None or int(match[1]) > 23 or int(match[2]) > 59:
        raise ClockError("daily_wake must be HH:MM.")
    return int(match[1]) * 60 + int(match[2])


def parse_at(text: str, start_at: datetime | str) -> int:
    """Return minutes from start_at for a weekday and time such as "Mon 06:30".

    Days count from the Monday of the week that holds start_at. A negative value means
    a time before the run. The schema validator refuses those.
    """
    start = parse_start(start_at) if isinstance(start_at, str) else start_at
    match = _AT_PATTERN.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None:
        raise ClockError(f"{text!r} is not a weekday and time such as 'Mon 06:30'.")
    day = match[1].lower()
    index = next(
        (i for i, name in enumerate(_DAY_NAMES) if name.lower() == day or _WEEKDAYS[i] == day),
        None,
    )
    hours, minutes = int(match[2]), int(match[3])
    if index is None or hours > 23 or minutes > 59:
        raise ClockError(f"{text!r} is not a weekday and time such as 'Mon 06:30'.")
    monday = start.date() - timedelta(days=start.weekday())
    moment = datetime.combine(
        monday + timedelta(days=index), time(hours, minutes), tzinfo=start.tzinfo
    )
    return math.floor((moment - start).total_seconds() / 60)


def format_time(value: datetime | str, tz: tzinfo | None = None) -> str:
    """Plain speech for a moment, such as "Tuesday 14:05"."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if tz is not None:
        moment = moment.astimezone(tz)
    return f"{_DAY_NAMES[moment.weekday()]} {moment:%H:%M}"


def format_date(value: datetime, tz: tzinfo | None = None) -> str:
    moment = value.astimezone(tz) if tz is not None else value
    return f"{_DAY_NAMES[moment.weekday()]} {moment.day} {moment:%B}"


def format_moment(value: datetime | str, tz: tzinfo | None = None) -> str:
    """Plain speech with the date, such as "Friday 30 October, 19:40"."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    moment = moment.astimezone(tz) if tz is not None else moment
    return f"{format_date(moment)}, {moment:%H:%M}"


@dataclass(frozen=True)
class ClockConfig:
    """The frozen clock section of one run, in seconds."""

    seed: int
    start_at: datetime
    duration_s: int
    epoch_s: int
    drama: str = "off"
    daily_wake_minutes: int = 480
    tokens_per_minute: int = 1500
    think_floor_s: int = 20
    think_cap_s: int = 180
    actor_read_s: int = 45
    actor_chars_per_minute: int = 300
    actor_floor_s: int = 30
    actor_cap_s: int = 900
    tool_seconds: Mapping[str, int] = field(default_factory=dict)
    default_tool_s: int = 30
    until_min_s: int = 300
    until_max_s: int = 172800
    ambient_delay_s: int = 2700
    max_agent_activations: int = 400
    max_actor_turns: int = 400
    max_model_calls: int = 12
    max_parallel_thinkers: int = 8
    director_enabled: bool = True
    review_every_s: int = 21600
    max_sites: int = 12

    @classmethod
    def from_section(cls, section: Mapping[str, Any], seed: int) -> ClockConfig:
        """Build from ScenarioSpec.clock as saved in the run's scenario JSON."""
        think = section.get("think", {})
        pace = section.get("actor_pace", {})
        until = section.get("until", {})
        limits = section.get("limits", {})
        director = section.get("director", {})
        return cls(
            seed=seed,
            start_at=parse_start(section["start_at"]),
            duration_s=section["duration_minutes"] * 60,
            epoch_s=section["epoch_minutes"] * 60,
            drama=section.get("drama", "off"),
            daily_wake_minutes=parse_daily(section.get("daily_wake", "08:00")),
            tokens_per_minute=think.get("tokens_per_minute", 1500),
            think_floor_s=think.get("floor_seconds", 20),
            think_cap_s=think.get("cap_seconds", 180),
            actor_read_s=pace.get("read_seconds", 45),
            actor_chars_per_minute=pace.get("chars_per_minute", 300),
            actor_floor_s=pace.get("floor_seconds", 30),
            actor_cap_s=pace.get("cap_seconds", 900),
            tool_seconds=dict(section.get("tool_seconds", {})),
            default_tool_s=section.get("default_tool_seconds", 30),
            until_min_s=until.get("min_minutes", 5) * 60,
            until_max_s=until.get("max_minutes", 2880) * 60,
            ambient_delay_s=until.get("ambient_delay_minutes", 45) * 60,
            max_agent_activations=limits.get("max_agent_activations_per_owner", 400),
            max_actor_turns=limits.get("max_actor_turns_per_owner", 400),
            max_model_calls=limits.get("max_model_calls_per_activation", 12),
            max_parallel_thinkers=limits.get("max_parallel_thinkers", 8),
            director_enabled=director.get("enabled", True),
            review_every_s=director.get("review_every_minutes", 360) * 60,
            max_sites=director.get("max_sites", 12),
        )

    @property
    def tz(self) -> tzinfo:
        return self.start_at.tzinfo

    def daily_quota(self, process_kind: str) -> int:
        """R14: the run's limit shared over its days, so one busy day cannot end a process."""
        total = self.max_agent_activations if process_kind == "agent" else self.max_actor_turns
        days = max(1, math.ceil(self.duration_s / 86400))
        return math.ceil(total / days)

    def day_start_s(self, now_s: int) -> int:
        """The daily wake that opened the current day. It can be before the run began."""
        return next_daily_wake(now_s, self) - 86400

    def moment(self, sim_s: int) -> datetime:
        return self.start_at + timedelta(seconds=sim_s)

    def now_text(self, sim_s: int) -> str:
        return format_moment(self.moment(sim_s))

    def time_text(self, value: datetime | str) -> str:
        """A saved row's time in plain speech. A row older than the start names its date."""
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        if moment < self.start_at:
            return format_moment(moment, self.tz)
        return format_time(moment, self.tz)

    def today_text(self, sim_s: int) -> str:
        return format_date(self.moment(sim_s))

    def window_at(self, sim_s: int) -> int:
        """The clearing epoch that holds a moment (R15)."""
        return sim_s // self.epoch_s


def tie(seed: int, key: str) -> bytes:
    """The R2 tie break. It depends on the seed and a stable key, never on arrival."""
    return hashlib.sha256(f"{seed}|{key}".encode()).digest()


def order_key(due_s: int, event_class: int, tie_value: bytes, key: str) -> tuple:
    """The total R2 pop order. Equal keys cannot occur, because event keys are unique."""
    return (due_s, event_class, bytes(tie_value), key)


def clamp(low: int, high: int, value: int) -> int:
    return max(low, min(high, value))


def _visible_chars(message) -> int:
    """Visible output length: text plus canonical tool calls. Provider usage is not used."""
    text = getattr(message, "text", "") or ""
    calls = [
        {"name": call.get("name"), "args": call.get("args")}
        for call in getattr(message, "tool_calls", None) or ()
    ]
    encoded = json.dumps(calls, sort_keys=True, separators=(",", ":"), default=str) if calls else ""
    return len(str(text)) + len(encoded)


def price_think(message, config: ClockConfig) -> int:
    """R7: thinking time from visible output, at the same rate for every model."""
    tokens = _visible_chars(message) / 4
    seconds = math.ceil(tokens * 60 / config.tokens_per_minute)
    return clamp(config.think_floor_s, config.think_cap_s, seconds)


def price_tool(name: str, config: ClockConfig) -> int:
    """R7: tool time comes from the frozen table, by visible or internal name."""
    table = config.tool_seconds
    if name in table:
        return table[name]
    internal = internal_tool_name(name)
    return table.get(internal, config.default_tool_s)


def price_actor(chars: int, config: ClockConfig) -> int:
    """Time for an owner to read and answer, from the length of what they wrote."""
    seconds = config.actor_read_s + math.ceil(chars * 60 / config.actor_chars_per_minute)
    return clamp(config.actor_floor_s, config.actor_cap_s, seconds)


@dataclass(frozen=True)
class StampedCall:
    call_id: str
    name: str
    stamp_s: int
    duration_s: int
    message_index: int
    call_index: int
    gated: bool


@dataclass(frozen=True)
class Fold:
    """Stamps of one activation, recomputed from saved messages (R6)."""

    calls: tuple[StampedCall, ...] = ()
    bars: tuple[int, ...] = ()
    end_s: int = 0
    last_message_calls: tuple[StampedCall, ...] = ()

    def by_call_id(self) -> dict[str, StampedCall]:
        return {call.call_id: call for call in self.calls}


def is_gated(name: str, gated: Collection[str] | None) -> bool:
    if gated is not None:
        return name in gated and name not in FREE_TOOLS
    return name not in FREE_TOOLS


def _activation_messages(messages: Sequence, activation_id) -> Sequence | None:
    target = str(activation_id)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if getattr(message, "type", None) == "human" and str(getattr(message, "id", "")) == target:
            return messages[index + 1 :]
    return None


def fold_stamps(
    start_s: int,
    messages: Sequence,
    activation_id,
    config: ClockConfig,
    *,
    gated: Collection[str] | None = None,
) -> Fold:
    """Give the stamp of each tool call and the bar after each model message.

    Only messages after HumanMessage(id=activation_id) count. Thinking happens first.
    Then each call lands at the start of its duration, in listed order, and the bar moves
    past it. A free or unknown tool costs nothing. An activation that has not started
    has no messages and stays at its start.
    """
    own = _activation_messages(messages, activation_id)
    if own is None:
        return Fold(end_s=start_s)
    now = start_s
    calls: list[StampedCall] = []
    bars: list[int] = []
    last: tuple[StampedCall, ...] = ()
    message_index = 0
    for message in own:
        if getattr(message, "type", None) != "ai":
            continue
        now += price_think(message, config)
        batch = []
        for call_index, call in enumerate(getattr(message, "tool_calls", None) or ()):
            name = call.get("name") or ""
            controlled = is_gated(name, gated)
            duration = price_tool(name, config) if controlled else 0
            stamped = StampedCall(
                call_id=str(call.get("id")),
                name=name,
                stamp_s=now,
                duration_s=duration,
                message_index=message_index,
                call_index=call_index,
                gated=controlled,
            )
            batch.append(stamped)
            now += duration
        calls.extend(batch)
        last = tuple(batch)
        bars.append(now)
        message_index += 1
    return Fold(calls=tuple(calls), bars=tuple(bars), end_s=now, last_message_calls=last)


def think_bound(bar_s: int, config: ClockConfig) -> int:
    """R5: nothing this Agent has not produced yet can land before this."""
    return bar_s + config.think_floor_s


def actor_bound(start_s: int, config: ClockConfig) -> int:
    """R5: an owner's words land no earlier than the floor of their pace."""
    return start_s + config.actor_floor_s


def notice_delay_s(seed: int, key: str, low: int = NOTICE_MIN_S, high: int = NOTICE_MAX_S) -> int:
    """R9: a seeded delay before someone notices a message."""
    if high <= low:
        return low
    digest = hashlib.sha256(f"{seed}|{key}|notice".encode()).digest()
    return low + int.from_bytes(digest[:8], "big") % (high - low + 1)


def next_daily_wake(now_s: int, config: ClockConfig) -> int:
    """The first daily wake strictly after now, in seconds from the start."""
    now = config.moment(now_s)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = midnight + timedelta(minutes=config.daily_wake_minutes)
    if candidate <= now:
        candidate += timedelta(days=1)
    return int((candidate - config.start_at).total_seconds())


def until_after_yield(now_s: int, wake_in_minutes: int | None, config: ClockConfig) -> int:
    """R8: a yield always has an until. With no request, it is the next daily wake."""
    if wake_in_minutes is None:
        return next_daily_wake(now_s, config)
    shortest = max(config.until_min_s, MIN_WAKE_S)
    return now_s + clamp(shortest, config.until_max_s, wake_in_minutes * 60)


def event_time_s(created_at: datetime, config: ClockConfig) -> int:
    """Story seconds of a stamped row. The microsecond offset belongs to the same second."""
    return max(0, math.floor((created_at - config.start_at).total_seconds()))


def classify_agent_event(event: Mapping, owner_id: str, agent_id: str | None = None) -> str | None:
    """Return direct, ambient, or None when this event cannot wake this Agent (R8)."""
    if event.get("cause_process") == agent_process(owner_id):
        return None
    if event.get("owner_id") not in {None, owner_id}:
        return None
    kind, payload = event["kind"], event.get("payload") or {}
    if kind == "owner_message":
        return "direct" if payload.get("source") == "owner" else None
    if kind == "board_posted":
        if agent_id is not None and str(payload.get("author_id")) == str(agent_id):
            return None
        audience = payload.get("audience")
        if audience:
            return "direct" if agent_id is None or str(agent_id) in map(str, audience) else None
        return "ambient"
    if kind in AGENT_DIRECT_KINDS:
        return "direct"
    if kind in AGENT_AMBIENT_KINDS:
        return "ambient"
    return None


def is_peer_event(event: Mapping, owner_id: str) -> bool:
    """Did another owner or their assistant cause this message to this assistant?"""
    kind, payload = event["kind"], event.get("payload") or {}
    if kind == "board_posted":
        return bool(payload.get("audience"))
    if kind == "inbox_received":
        sender = payload.get("sender_owner_id")
        return sender is not None and sender != owner_id
    return False


def classify_actor_event(event: Mapping, owner_id: str) -> str | None:
    """Return cue, message, or None when this event cannot wake this owner (R8, R9, R13)."""
    if event.get("cause_process") == actor_process(owner_id):
        return None
    if event.get("owner_id") not in {None, owner_id}:
        return None
    kind, payload = event["kind"], event.get("payload") or {}
    if kind in ACTOR_CUE_KINDS:
        # A quiet cue is read at the owner's next moment. It does not make that moment.
        return None if payload.get("quiet") else "cue"
    if kind == "owner_message":
        return "message" if payload.get("source") == "agent" else None
    if kind in {"market_settled", "reservation_settled"} and event.get("owner_id") != owner_id:
        return None
    if kind in ACTOR_MESSAGE_KINDS:
        return "message"
    return None


def strip_nul(value: Any) -> Any:
    """Remove NUL from model text. PostgreSQL text and JSON cannot hold it."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, Mapping):
        return {strip_nul(key): strip_nul(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_nul(item) for item in value]
    return value


def ordered(events: Iterable[Mapping]) -> list[Mapping]:
    """Sort durable event rows by the R2 pop order. Tests use this as the reference order."""
    return sorted(events, key=lambda e: order_key(e["due_s"], e["class"], e["tie"], e["key"]))


async def _state(conn, run_id) -> dict:
    cursor = await conn.execute(
        """SELECT s.sim_s, r.seed FROM clock_state s JOIN environment_runs r ON r.id=s.run_id
           WHERE s.run_id=%s""",
        (run_id,),
    )
    state = await cursor.fetchone()
    if state is None:
        raise ClockError("This run has no event clock.")
    return state


async def schedule(
    conn,
    run_id,
    due_s: int,
    kind: str,
    *positional,
    payload: Mapping | None = None,
    key: str | None = None,
    owner_id: str | None = None,
    source: str = "engine",
    event_class: int | None = None,
) -> UUID:
    """Queue one durable event once. A repeated key keeps the first row.

    Positional forms: (payload, key, owner_id, source, event_class), or (key) with the
    rest as keywords. A due time before the current moment moves up to it, because story
    time never goes back. A key must not contain a random ID, because the R2 tie comes
    from it.
    """
    if positional and isinstance(positional[0], str):
        key = positional[0]
    elif positional:
        names = ("payload", "key", "owner_id", "source", "event_class")
        values = dict(zip(names, positional, strict=False))
        payload = values.get("payload", payload)
        key = values.get("key", key)
        owner_id = values.get("owner_id", owner_id)
        source = values.get("source", source)
        event_class = values.get("event_class", event_class)
    if not isinstance(key, str) or not 1 <= len(key) <= 500:
        raise ClockError("A clock event needs a stable key of 1 to 500 characters.")
    run_id = UUID(str(run_id))
    if event_class is None:
        if kind not in EVENT_CLASSES:
            raise ClockError(f"Unknown clock event kind {kind!r}.")
        event_class = EVENT_CLASSES[kind]
    state = await _state(conn, run_id)
    event_id = uuid5(run_id, f"clock_event:{key}")
    await conn.execute(
        """INSERT INTO clock_events
           (id,run_id,due_s,class,tie,key,kind,owner_id,payload,source,created_sim_s)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id,key) DO NOTHING""",
        (
            event_id,
            run_id,
            max(int(due_s), state["sim_s"]),
            event_class,
            tie(state["seed"], key),
            key,
            kind,
            owner_id,
            Jsonb(dict(payload or {})),
            source,
            state["sim_s"],
        ),
    )
    return event_id


async def cancel(conn, run_id, key: str) -> bool:
    """Cancel a queued event. A popped or missing event stays as it is."""
    cursor = await conn.execute(
        "UPDATE clock_events SET status='cancelled' WHERE run_id=%s AND key=%s "
        "AND status='queued' RETURNING id",
        (run_id, key),
    )
    return await cursor.fetchone() is not None


async def retime(conn, run_id, key: str, due_s: int) -> bool:
    """Move a queued event. It cannot move behind the current moment."""
    state = await _state(conn, UUID(str(run_id)))
    cursor = await conn.execute(
        "UPDATE clock_events SET due_s=%s WHERE run_id=%s AND key=%s "
        "AND status='queued' RETURNING id",
        (max(int(due_s), state["sim_s"]), run_id, key),
    )
    return await cursor.fetchone() is not None


async def _owned_event(conn, run_id, key: str, only_source: str | None) -> None:
    cursor = await conn.execute(
        "SELECT source,status FROM clock_events WHERE run_id=%s AND key=%s", (run_id, key)
    )
    event = await cursor.fetchone()
    if event is None or event["status"] != "queued":
        raise ClockError("That is not a queued event.")
    if only_source is not None and event["source"] != only_source:
        raise ClockError("That event belongs to another source.")


async def cancel_event(conn, run_id, key: str, *, only_source: str | None = None) -> None:
    """Cancel a queued event for a caller that must learn when it cannot."""
    await _owned_event(conn, run_id, key, only_source)
    await cancel(conn, run_id, key)


async def retime_event(
    conn, run_id, key: str, due_s: int, *, only_source: str | None = None
) -> None:
    """Move a queued event for a caller that must learn when it cannot."""
    await _owned_event(conn, run_id, key, only_source)
    await retime(conn, run_id, key, due_s)


def _seed_post_time(start: datetime, post) -> datetime:
    """A daytime moment for an old post, set by its ID, always early enough for its replies."""
    digest = hashlib.sha256(f"seed-board:{post.id}".encode()).digest()
    latest = start - timedelta(days=post.days_ago)
    moment = latest.replace(hour=8 + digest[0] % 14, minute=digest[1] % 60, second=0, microsecond=0)
    if moment >= latest:
        moment -= timedelta(days=1)
    return min(moment, start - timedelta(hours=7))


async def seed_board(conn, run_id, cluster_id, clock, participants) -> None:
    """Save old board posts with their past times, as already read by every Agent who sees them.

    Call this before the clock_state row exists, so the stamp trigger leaves created_at alone.
    Rows go in by time, so the board's sequence order agrees with story order.
    """
    agents = {item["owner_id"]: UUID(str(item["agent_id"])) for item in participants}
    start = parse_start(clock.start_at)
    rows = []
    for post in clock.seed_board:
        root_at = _seed_post_time(start, post)
        root_id = uuid5(UUID(str(run_id)), f"seed-board:{post.id}")
        audience = (
            None
            if post.audience is None
            else sorted({agents[owner] for owner in post.audience} - {agents[post.author]})
        )
        rows.append((root_at, root_id, agents[post.author], None, post.title, post.body, audience))
        spacing = timedelta(hours=6) / (len(post.replies) + 1)
        for index, reply in enumerate(post.replies):
            rows.append(
                (
                    root_at + spacing * (index + 1),
                    uuid5(UUID(str(run_id)), f"seed-board:{post.id}:{index}"),
                    agents[reply.author],
                    root_id,
                    None,
                    reply.body,
                    None,
                )
            )
    rows.sort(key=lambda item: (item[0], str(item[1])))
    visibility = {row[1]: row[6] for row in rows if row[3] is None}
    for created_at, message_id, author, parent, title, body, audience in rows:
        payload_hash = hashlib.sha256(
            json.dumps([str(parent), title, body], sort_keys=True).encode()
        ).hexdigest()
        await conn.execute(
            """INSERT INTO messages
               (id,cluster_id,author_id,parent_id,title,body,request_id,payload_hash,created_at,
                audience)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                message_id,
                cluster_id,
                author,
                parent,
                title,
                body,
                uuid5(message_id, "seed-request"),
                payload_hash,
                created_at,
                audience,
            ),
        )
        root = visibility[parent or message_id]
        root_author = next(row[2] for row in rows if row[1] == (parent or message_id))
        readers = agents.values() if root is None else {root_author, *root}
        for reader in sorted(readers, key=str):
            await conn.execute(
                """INSERT INTO message_receipts(cluster_id,agent_id,message_id,acknowledged_at)
                   VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (cluster_id, reader, message_id, created_at),
            )


async def seed_clock(conn, run_id, clock, seed, participants) -> None:
    """Create the clock of a new run in the caller's transaction, with its known future.

    Keys hold no random ID, so the same scenario and seed give the same pop order.
    """
    section = clock.model_dump(mode="json")
    start = parse_start(clock.start_at)
    await conn.execute(
        "INSERT INTO clock_state(run_id,epoch,config) VALUES (%s,%s,%s)",
        (run_id, start, Jsonb(section)),
    )
    # R15: a clocked run has no Actor and Agent phases. Readers of this column stay valid.
    await conn.execute("UPDATE environment_runs SET phase='agents' WHERE id=%s", (run_id,))
    for participant in participants:
        for kind in ("actor", "agent"):
            await conn.execute(
                "INSERT INTO clock_processes(run_id,owner_id,kind) VALUES (%s,%s,%s)",
                (run_id, participant["owner_id"], kind),
            )
    duration_s = clock.duration_minutes * 60
    epoch_s = clock.epoch_minutes * 60
    await schedule(
        conn,
        run_id,
        min(epoch_s, duration_s),
        "boundary",
        {"window": 0},
        "boundary:0",
        None,
        "plot",
    )
    for item in clock.timeline:
        await schedule(
            conn,
            run_id,
            parse_at(item.at, start) * 60,
            "beat",
            {"id": item.id},
            f"beat:{item.id}",
            None,
            "plot",
        )
    if clock.director.enabled:
        every = clock.director.review_every_minutes * 60
        for index, due_s in enumerate(range(every, duration_s, every)):
            await schedule(
                conn,
                run_id,
                due_s,
                "director_review",
                {"reason": "scheduled"},
                f"review:{index}",
                None,
                "plot",
            )
    for item in clock.lifecycle:
        await schedule(
            conn,
            run_id,
            parse_at(item.due_at, start) * 60,
            "lifecycle_check",
            item.model_dump(mode="json"),
            f"lifecycle:{item.owner_id}:{item.site_id}:{item.bill_id}",
            item.owner_id,
            "plot",
        )
    await schedule(conn, run_id, duration_s, "run_end", {}, "run_end", None, "plot")


async def next_event(conn, run_id) -> dict | None:
    """The lowest queued durable event by the R2 order, or None."""
    cursor = await conn.execute(
        """SELECT * FROM clock_events WHERE run_id=%s AND status='queued'
           ORDER BY due_s, class, tie, key COLLATE "C" LIMIT 1""",
        (run_id,),
    )
    return await cursor.fetchone()
