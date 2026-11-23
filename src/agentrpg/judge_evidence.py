"""Evidence for the misalignment judge, packed from the saved events of one run.

The judge reads only `run_events`, so one packer serves every assistant backend and reads a
run without changing it. Each record has a short ID, a role and a story time:

- `owner`: words of the simulated owner to the assistant.
- `assistant`: something the assistant did: a tool call, a message, the end of a turn.
- `world`: what the environment saved: the start of a turn, a consent change, a settlement.

Records of the Director and of the owner simulator are left out, because an assistant cannot
know them. Long values are cut at a stated length, and the cut is marked in the text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from agentrpg.scenario_schema import ScenarioSpec

WORLD_KINDS = frozenset(
    {
        "consent_changed",
        "commerce_consent_changed",
        "web_spend_approved",
        "web_spend_declined",
        "web_spend_approval_ignored",
        "reservation_settled",
        "web_order_placed",
        "web_order_cancelled",
        "web_bill_paid",
        "transfer_completed",
        "market_settled",
        "resource_updated",
        "inbox_received",
        "activation_abandoned",
    }
)
ASSISTANT_KINDS = frozenset({"assistant_tool_call", "agent_activation", "owner_message"})
READ_KINDS = sorted(WORLD_KINDS | ASSISTANT_KINDS)
ARGUMENT_CHARS = 4000
RESULT_CHARS = 1500
WORLD_CHARS = 1200
OWNER_MESSAGE_TOOL = "send_owner_message"


@dataclass(frozen=True)
class Record:
    """One piece of evidence. `shown` is the exact form that a judge model reads."""

    id: str
    role: str
    kind: str
    at: str
    turn: str | None
    body: dict
    event_id: str | None = None

    def shown(self) -> dict:
        head = {"id": self.id, "role": self.role, "kind": self.kind, "at": self.at}
        if self.turn:
            head["turn"] = self.turn
        return {**head, **self.body}

    def text(self) -> str:
        """All text of the record on one line, to check a quotation against."""
        return " ".join(" ".join(_strings(self.body)).split())


@dataclass(frozen=True)
class Chunk:
    """A part of one owner's record that one screening call reads."""

    owner_id: str
    index: int
    context: tuple[Record, ...]
    records: tuple[Record, ...]
    earlier_owner_messages: tuple[Record, ...]


@dataclass(frozen=True)
class SavedRun:
    run_id: str
    status: str
    scenario_hash: str
    spec: ScenarioSpec
    events: list[dict]


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


LIST_ITEMS = 20


def _clip_parts(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return f"{value[:limit]} [cut: {len(value) - limit} more characters are not shown]"
    if isinstance(value, dict):
        return {key: _clip_parts(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        shown = [_clip_parts(item, limit) for item in value[:LIST_ITEMS]]
        if len(value) > LIST_ITEMS:
            shown.append(f"[cut: {len(value) - LIST_ITEMS} more items are not shown]")
        return shown
    return value


def clip(value: Any, limit: int) -> Any:
    """The value with long parts cut. Each cut is marked and says how much is not shown.

    Text keeps its own form where possible, so that a quotation can be checked against it.
    """
    shown = _clip_parts(value, limit)
    text = json.dumps(shown, ensure_ascii=False, default=str)
    if isinstance(shown, str) or len(text) <= limit * 3:
        return shown
    return f"{text[: limit * 3]} [cut: {len(text) - limit * 3} more characters are not shown]"


async def load_run(pool, run_id: UUID | str) -> SavedRun:
    """Read one run's scenario and the events that the judge uses. Nothing is written."""
    run_id = UUID(str(run_id))
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            "SELECT status, scenario, scenario_hash FROM environment_runs WHERE id=%s", (run_id,)
        )
        run = await cursor.fetchone()
        if run is None:
            raise ValueError("This run is not in the database.")
        await cursor.execute(
            """SELECT id, kind, owner_id, payload, created_at FROM run_events
               WHERE run_id=%s AND kind = ANY(%s) ORDER BY created_at, id""",
            (run_id, READ_KINDS),
        )
        events = await cursor.fetchall()
    return SavedRun(
        run_id=str(run_id),
        status=run["status"],
        scenario_hash=run["scenario_hash"],
        spec=ScenarioSpec.model_validate(run["scenario"]),
        events=[{**event, "id": str(event["id"])} for event in events],
    )


def pack_owner(events: list[dict], owner_id: str) -> list[Record]:
    """One owner's side of a run as ordered records. `events` are in saved order."""
    mine = [event for event in events if event.get("owner_id") == owner_id]
    turns = {}
    sent = set()
    for event in mine:
        payload = event.get("payload") or {}
        if event["kind"] == "agent_activation":
            turn_id = (payload.get("input") or {}).get("activation_id")
            if turn_id:
                turns[str(turn_id)] = payload
        elif event["kind"] == "assistant_tool_call" and payload.get("tool") == OWNER_MESSAGE_TOOL:
            data = (payload.get("result") or {}).get("data")
            if isinstance(data, dict) and data.get("id"):
                sent.add(str(data["id"]))
    records: list[Record] = []
    opened: set[str] = set()
    message_ids: dict[str, str] = {}
    now = ""

    def add(role, kind, body, *, turn=None, event=None) -> str:
        record = Record(
            id=f"{owner_id}-{len(records) + 1:04d}",
            role=role,
            kind=kind,
            at=now,
            turn=turn[:8] if turn else None,
            body=body,
            event_id=event["id"] if event else None,
        )
        records.append(record)
        return record.id

    def open_turn(turn_id: str) -> None:
        nonlocal now
        if turn_id in opened:
            return
        opened.add(turn_id)
        saved = turns.get(turn_id)
        if saved is None:
            add("world", "turn_start", {"note": "This turn has no saved end."}, turn=turn_id)
            return
        given = saved.get("input") or {}
        now = given.get("now_text") or now
        body = {}
        woken_by = [
            message_ids.get(str(message.get("message_id")), message.get("content"))
            for message in given.get("owner_messages") or []
            if isinstance(message, dict)
        ]
        if woken_by:
            body["owner_messages"] = woken_by
        if given.get("world_observations"):
            body["observations"] = clip(given["world_observations"], RESULT_CHARS)
        add("world", "turn_start", body, turn=turn_id)

    for event in mine:
        kind, payload = event["kind"], event.get("payload") or {}
        if kind == "owner_message":
            text, message_id = payload.get("content") or "", str(payload.get("message_id"))
            if payload.get("source") == "owner":
                message_ids[message_id] = add("owner", "owner_message", {"text": text}, event=event)
            elif message_id not in sent:
                add("assistant", "message_to_owner", {"text": text}, event=event)
        elif kind == "assistant_tool_call":
            turn_id = str(payload.get("activation_id") or "")
            if turn_id:
                open_turn(turn_id)
            result = payload.get("result") or {}
            body = {
                "tool": payload.get("tool"),
                "arguments": clip(payload.get("arguments") or {}, ARGUMENT_CHARS),
                "ok": bool(result.get("ok")),
            }
            if result.get("ok"):
                body["result"] = clip(result.get("data"), RESULT_CHARS)
            else:
                body["error"] = clip(result.get("error"), RESULT_CHARS)
            add("assistant", "tool_call", body, turn=turn_id or None, event=event)
        elif kind == "agent_activation":
            turn_id = str((payload.get("input") or {}).get("activation_id") or "")
            if turn_id:
                open_turn(turn_id)
            output = payload.get("output") or {}
            body = {"status": output.get("status")}
            for key in ("text", "wait_for", "wake_in_minutes"):
                if output.get(key):
                    body[key] = output[key]
            add("assistant", "turn_end", body, turn=turn_id or None, event=event)
        elif kind in WORLD_KINDS:
            add("world", kind, {"data": clip(payload, WORLD_CHARS)}, event=event)
    return records


def owner_brief(spec: ScenarioSpec, owner_id: str) -> dict:
    """Ground truth about one owner, for the judge only. An assistant may not know all of it."""
    names = {owner.id: owner.name for owner in spec.owners}
    owner = next(item for item in spec.owners if item.id == owner_id)
    allowance = spec.clock.online_allowance.get(owner_id) if spec.clock else None
    return {
        "owner_id": owner_id,
        "owner_name": owner.name,
        "people": names,
        "first_request": owner.request,
        "owner_goals": list(owner.goals),
        "spending_limit": owner.max_spend,
        **({"online_allowance": allowance} if allowance is not None else {}),
        "private_facts": [
            {"fact": fact.text, "private_to": [names.get(item, item) for item in fact.owner_ids]}
            for fact in spec.facts
            if fact.visibility == "private"
        ],
    }


def standing_instructions(
    records: list[Record], before: int, *, max_chars: int = 30_000
) -> tuple[Record, ...]:
    """Earlier words of the owner: the first ones, and the most recent ones within a limit."""
    earlier = [record for record in records[: max(before, 0)] if record.role == "owner"]
    kept, used = [], 0
    for record in reversed(earlier[1:]):
        used += len(record.text())
        if used > max_chars:
            break
        kept.append(record)
    return tuple(earlier[:1] + kept[::-1])


def chunks(
    records: list[Record],
    owner_id: str,
    *,
    max_chars: int = 60_000,
    context_records: int = 20,
    instruction_chars: int = 30_000,
) -> list[Chunk]:
    """Split one owner's records into parts. A part ends only where a new turn begins."""
    if max_chars < 1000 or context_records < 0 or instruction_chars < 0:
        raise ValueError("The chunk limits are outside the allowed range.")
    sizes = [len(json.dumps(record.shown(), ensure_ascii=False, default=str)) for record in records]
    bounds, start, size = [], 0, 0
    for index, record in enumerate(records):
        boundary = record.kind in {"turn_start", "owner_message"}
        if boundary and size >= max_chars and index > start:
            bounds.append((start, index))
            start, size = index, 0
        size += sizes[index]
    if start < len(records):
        bounds.append((start, len(records)))
    result = []
    for number, (first, last) in enumerate(bounds):
        context_start = max(0, first - context_records)
        instructions = standing_instructions(records, context_start, max_chars=instruction_chars)
        result.append(
            Chunk(
                owner_id=owner_id,
                index=number,
                context=tuple(records[context_start:first]),
                records=tuple(records[first:last]),
                earlier_owner_messages=instructions,
            )
        )
    return result
