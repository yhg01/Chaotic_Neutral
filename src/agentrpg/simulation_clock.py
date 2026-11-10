"""Versioned elapsed-time Clock component, separate from legacy decision windows.

This trusted Director-side service has no model, tool-effect or public HTTP API.
Transactions only save clock decisions. Model execution belongs in a future adapter.
"""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

MAX_SECONDS = 31_536_000
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")]
Seconds = Annotated[int, Field(strict=True, ge=0, le=MAX_SECONDS)]
PositiveSeconds = Annotated[int, Field(strict=True, ge=1, le=MAX_SECONDS)]


class ClockError(ValueError):
    """A clock decision violates the frozen time contract."""


class ClockConflict(ClockError):
    """A stable identity was reused with changed input."""


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcessSpec(Frozen):
    id: Identifier
    owner_id: Identifier
    role: Literal["actor", "agent"]


class ClockContract(Frozen):
    version: Literal["elapsed-seconds-v1"] = "elapsed-seconds-v1"
    epoch: datetime
    horizon_seconds: PositiveSeconds
    processes: tuple[ProcessSpec, ...] = Field(min_length=2, max_length=30)
    tool_seconds: dict[Identifier, PositiveSeconds] = Field(default_factory=dict, max_length=50)
    output_tokens_per_minute: int = Field(strict=True, ge=1, le=1_000_000)
    max_events: int = Field(default=10_000, strict=True, ge=1, le=100_000)
    generation_time_policy: Literal["ceil_output_tokens_at_frozen_rate_minimum_one_second"] = (
        "ceil_output_tokens_at_frozen_rate_minimum_one_second"
    )
    horizon_policy: Literal["exclusive_end_retain_pending"] = "exclusive_end_retain_pending"
    same_time_order: Literal["seconds_priority_ascii_key_no_retroactive_insert"] = (
        "seconds_priority_ascii_key_no_retroactive_insert"
    )

    @field_validator("epoch", mode="before")
    @classmethod
    def explicit_epoch(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("Use an ISO timestamp or datetime, not a numeric epoch.")
        return value

    @field_validator("epoch")
    @classmethod
    def utc_epoch(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("A timezone-aware simulation epoch is required.")
        if value.microsecond:
            raise ValueError("The epoch must use whole seconds.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def pairs(self) -> Self:
        ids = [p.id for p in self.processes]
        identities = [(p.owner_id, p.role) for p in self.processes]
        owners = {p.owner_id for p in self.processes}
        if len(ids) != len(set(ids)) or len(identities) != len(set(identities)):
            raise ValueError("Process and owner-role identities must be unique.")
        if len(owners) > 15 or any(
            {p.role for p in self.processes if p.owner_id == o} != {"actor", "agent"}
            for o in owners
        ):
            raise ValueError("Each of at most fifteen owners needs one Actor and one Agent.")
        try:
            self.epoch + timedelta(seconds=self.horizon_seconds)
        except OverflowError as exc:
            raise ValueError("The epoch and horizon exceed the datetime range.") from exc
        return self


class Schedule(Frozen):
    operation: Literal["schedule"] = "schedule"
    event_key: Identifier
    at_seconds: Seconds
    priority: int = Field(default=0, strict=True, ge=-100, le=100)
    kind: Identifier
    recipients: tuple[Identifier, ...] = Field(min_length=1, max_length=30)
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def external(self) -> Self:
        if self.kind in {"timer", "tool_complete", "generation_complete"}:
            raise ValueError("Internal completion kinds cannot be scheduled as external events.")
        if len(set(self.recipients)) != len(self.recipients):
            raise ValueError("Recipients must be unique.")
        if len(json.dumps(self.payload, allow_nan=False).encode()) > 8192:
            raise ValueError("Event payload exceeds 8192 bytes.")
        return self


class Sleep(Frozen):
    operation: Literal["sleep"] = "sleep"
    process_id: Identifier
    until_seconds: Seconds
    interrupt_on: tuple[Identifier, ...] = Field(default=(), max_length=20)


class Work(Frozen):
    operation: Literal["work"] = "work"
    process_id: Identifier
    kind: Literal["tool", "generation"]
    tool: Identifier | None = None
    output_tokens: int | None = Field(default=None, strict=True, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def usage(self) -> Self:
        if self.kind == "tool" and (self.tool is None or self.output_tokens is not None):
            raise ValueError("Tool work requires only a declared tool name.")
        if self.kind == "generation" and (self.tool is not None or self.output_tokens is None):
            raise ValueError("Generation requires measured output tokens and no tool name.")
        return self


class Control(Frozen):
    operation: Literal["step", "pause", "resume"]


COMMAND = TypeAdapter(
    Annotated[Schedule | Sleep | Work | Control, Field(discriminator="operation")]
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def generation_seconds(tokens, rate):
    return max(1, (60 * tokens + rate - 1) // rate)


async def one(conn, query, params=()):
    return await (await conn.execute(query, params)).fetchone()


class SimulationClock:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def _connection(self, connection=None):
        if connection is not None:
            yield connection
        else:
            async with self.pool.connection() as conn:
                yield conn

    async def create(self, contract: ClockContract, *, clock_id=None, connection=None):
        # Revalidate copies: Pydantic's shallow frozen setting is not a deep freeze.
        contract = ClockContract.model_validate(contract.model_dump(mode="json"))
        clock_id = UUID(str(clock_id)) if clock_id else uuid4()
        data = contract.model_dump(mode="json")
        identity = digest(data)
        async with self._connection(connection) as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO simulation_clocks(id,contract,contract_hash) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (clock_id, Jsonb(data), identity),
            )
            saved = await one(
                conn, "SELECT * FROM simulation_clocks WHERE id=%s FOR UPDATE", (clock_id,)
            )
            if saved["contract_hash"] != identity:
                raise ClockConflict("Clock identity already has a different frozen contract.")
            for p in contract.processes:
                await conn.execute(
                    "INSERT INTO simulation_clock_processes(clock_id,process_id,owner_id,role) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (clock_id, p.id, p.owner_id, p.role),
                )
        return clock_id

    async def execute(self, clock_id, request_id, command, *, connection=None, manager_run_id=None):
        clock_id, request_id = UUID(str(clock_id)), UUID(str(request_id))
        data = command.model_dump(mode="json") if isinstance(command, Frozen) else command
        command = COMMAND.validate_python(data)
        identity = digest(command.model_dump(mode="json"))
        async with self._connection(connection) as conn, conn.transaction():
            clock = await one(
                conn, "SELECT * FROM simulation_clocks WHERE id=%s FOR UPDATE", (clock_id,)
            )
            if clock is None:
                raise ClockError("Clock not found.")
            if clock.get("manager_run_id") is not None and str(manager_run_id) != str(
                clock["manager_run_id"]
            ):
                raise ClockError("Use the bound Director Clock adapter for this clock.")
            prior = await one(
                conn,
                "SELECT * FROM simulation_clock_operations WHERE clock_id=%s AND request_id=%s",
                (clock_id, request_id),
            )
            if prior:
                if prior["input_hash"] != identity:
                    raise ClockConflict("Request identity already has a different command.")
                return prior["response"]
            contract = ClockContract.model_validate(clock["contract"])
            clock["revision"] += 1
            result = await self._apply(conn, clock, contract, request_id, command)
            await conn.execute(
                "UPDATE simulation_clocks SET now_seconds=%s,status=%s,revision=%s, "
                "last_event_second=%s,last_event_priority=%s,last_event_key=%s WHERE id=%s",
                tuple(
                    clock[k]
                    for k in (
                        "now_seconds",
                        "status",
                        "revision",
                        "last_event_second",
                        "last_event_priority",
                        "last_event_key",
                        "id",
                    )
                ),
            )
            response = {
                "now_seconds": clock["now_seconds"],
                "status": clock["status"],
                "revision": clock["revision"],
                **result,
            }
            await conn.execute(
                "INSERT INTO simulation_clock_operations VALUES (%s,%s,%s,%s)",
                (clock_id, request_id, identity, Jsonb(response)),
            )
        return response

    async def _event(self, conn, clock, contract, data):
        identity = digest(data)
        prior = await one(
            conn,
            "SELECT * FROM simulation_clock_events WHERE clock_id=%s AND event_key=%s",
            (clock["id"], data["event_key"]),
        )
        if prior:
            if prior["input_hash"] != identity:
                raise ClockConflict("Event identity already has different content.")
            return
        if data["at_seconds"] < clock["now_seconds"] or (
            data["at_seconds"] == clock["last_event_second"]
            and (data["priority"], data["event_key"])
            <= (clock["last_event_priority"], clock["last_event_key"])
        ):
            raise ClockError("An event cannot precede the committed event cursor.")
        if data["at_seconds"] > MAX_SECONDS:
            raise ClockError("Event time exceeds the versioned capacity.")
        count = await one(
            conn,
            "SELECT count(*) AS n FROM simulation_clock_events WHERE clock_id=%s",
            (clock["id"],),
        )
        if count["n"] >= contract.max_events:
            raise ClockError("The frozen event capacity is exhausted.")
        known = {p.id for p in contract.processes}
        if not set(data["recipients"]) <= known:
            raise ClockError("Event recipients must belong to this clock.")
        await conn.execute(
            "INSERT INTO simulation_clock_events "
            "(clock_id,event_key,at_seconds,priority,kind,recipients,payload,input_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                clock["id"],
                data["event_key"],
                data["at_seconds"],
                data["priority"],
                data["kind"],
                Jsonb(data["recipients"]),
                Jsonb(data["payload"]),
                identity,
            ),
        )

    async def _apply(self, conn, clock, contract, request_id, command):
        op = command.operation
        if op in {"pause", "resume"}:
            if clock["status"] == "horizon":
                raise ClockError("The horizon is terminal.")
            clock["status"] = "paused" if op == "pause" else "active"
            return {"reason": op}
        if op == "step":
            if clock["status"] != "active":
                return {"reason": clock["status"], "event": None}
            return await self._step(conn, clock, contract)
        if clock["status"] != "active":
            raise ClockError("Resume an active clock before changing its queue or processes.")
        if op == "schedule":
            await self._event(conn, clock, contract, command.model_dump(exclude={"operation"}))
            return {"event_key": command.event_key}
        process = await one(
            conn,
            "SELECT * FROM simulation_clock_processes WHERE clock_id=%s AND process_id=%s",
            (clock["id"], command.process_id),
        )
        if process is None:
            raise ClockError("Process not found in this clock.")
        if process["state"] != "ready":
            raise ClockError("Only a ready process may choose its next wait or work.")
        event_key = f"{op}-{request_id.hex}"
        if op == "sleep":
            until = command.until_seconds
            if until <= clock["now_seconds"]:
                raise ClockError("A timed wait must end after the current simulated second.")
            kind, state, payload = "timer", "sleeping", {}
            interrupt_on = list(command.interrupt_on)
        else:
            if command.kind == "tool":
                if command.tool not in contract.tool_seconds:
                    raise ClockError("No duration is frozen for this tool.")
                duration = contract.tool_seconds[command.tool]
            else:
                duration = generation_seconds(
                    command.output_tokens, contract.output_tokens_per_minute
                )
            until = clock["now_seconds"] + duration
            kind, state = command.kind + "_complete", "busy"
            payload = {
                "duration_seconds": duration,
                "tool": command.tool,
                "output_tokens": command.output_tokens,
            }
            interrupt_on = []
        await self._event(
            conn,
            clock,
            contract,
            {
                "event_key": event_key,
                "at_seconds": until,
                "priority": 0,
                "kind": kind,
                "recipients": [command.process_id],
                "payload": payload,
            },
        )
        await conn.execute(
            "UPDATE simulation_clock_processes SET state=%s,ready_at=%s,event_key=%s, "
            "interrupt_on=%s WHERE clock_id=%s AND process_id=%s",
            (state, until, event_key, Jsonb(interrupt_on), clock["id"], command.process_id),
        )
        return {"event_key": event_key, "ready_at": until}

    async def _step(self, conn, clock, contract):
        event = await one(
            conn,
            "SELECT * FROM simulation_clock_events WHERE clock_id=%s AND status='pending' "
            "ORDER BY at_seconds,priority,event_key LIMIT 1",
            (clock["id"],),
        )
        target = (
            min(event["at_seconds"], contract.horizon_seconds)
            if event
            else contract.horizon_seconds
        )
        ready = await one(
            conn,
            "SELECT count(*) AS n FROM simulation_clock_processes "
            "WHERE clock_id=%s AND state='ready'",
            (clock["id"],),
        )
        if target > clock["now_seconds"] and ready["n"]:
            return {"reason": "ready_process_requires_decision", "event": None}
        clock["now_seconds"] = target
        if target == contract.horizon_seconds:
            clock["status"] = "horizon"
            return {"reason": "horizon_cutoff", "event": None}
        processes = await (
            await conn.execute(
                "SELECT * FROM simulation_clock_processes WHERE clock_id=%s "
                "AND process_id=ANY(%s) ORDER BY process_id",
                (clock["id"], event["recipients"]),
            )
        ).fetchall()
        woken = []
        for process in processes:
            own_completion = process["event_key"] == event["event_key"]
            interrupted = (
                process["state"] == "sleeping" and event["kind"] in process["interrupt_on"]
            )
            if own_completion or interrupted:
                if interrupted and process["event_key"] != event["event_key"]:
                    await conn.execute(
                        "UPDATE simulation_clock_events SET status='cancelled' "
                        "WHERE clock_id=%s AND event_key=%s AND status='pending'",
                        (clock["id"], process["event_key"]),
                    )
                await conn.execute(
                    "UPDATE simulation_clock_processes SET state='ready',ready_at=%s, "
                    "event_key=NULL,interrupt_on='[]' WHERE clock_id=%s AND process_id=%s",
                    (target, clock["id"], process["process_id"]),
                )
                woken.append(process["process_id"])
        await conn.execute(
            "UPDATE simulation_clock_events SET status='delivered',delivered_revision=%s "
            "WHERE clock_id=%s AND event_key=%s",
            (clock["revision"], clock["id"], event["event_key"]),
        )
        clock.update(
            last_event_second=target,
            last_event_priority=event["priority"],
            last_event_key=event["event_key"],
        )
        return {
            "reason": "event_delivered",
            "event": {
                k: event[k]
                for k in ("event_key", "at_seconds", "priority", "kind", "recipients", "payload")
            },
            "woken_process_ids": woken,
        }

    async def snapshot(self, clock_id):
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            clock = await one(conn, "SELECT * FROM simulation_clocks WHERE id=%s", (clock_id,))
            if clock is None:
                raise ClockError("Clock not found.")
            processes = await (
                await conn.execute(
                    "SELECT process_id,owner_id,role,state,ready_at,event_key,interrupt_on "
                    "FROM simulation_clock_processes WHERE clock_id=%s ORDER BY process_id",
                    (clock_id,),
                )
            ).fetchall()
            counts = await (
                await conn.execute(
                    "SELECT status,count(*) AS n FROM simulation_clock_events "
                    "WHERE clock_id=%s GROUP BY status",
                    (clock_id,),
                )
            ).fetchall()
            adapter = await one(
                conn,
                "SELECT EXISTS(SELECT 1 FROM clock_runtime_profiles WHERE run_id=%s) AS installed",
                (clock.get("manager_run_id"),),
            )
            pending = await (
                await conn.execute(
                    "SELECT event_key,at_seconds,priority,kind,recipients,payload "
                    "FROM simulation_clock_events WHERE clock_id=%s AND status='pending' "
                    "ORDER BY at_seconds,priority,event_key LIMIT 100",
                    (clock_id,),
                )
            ).fetchall()
        counts = {x["status"]: x["n"] for x in counts}
        contract = ClockContract.model_validate(clock["contract"])
        return {
            "clock_id": str(clock_id),
            "contract": clock["contract"],
            "contract_hash": clock["contract_hash"],
            "now_seconds": clock["now_seconds"],
            "simulated_at": (contract.epoch + timedelta(seconds=clock["now_seconds"])).isoformat(),
            "status": clock["status"],
            "revision": clock["revision"],
            "processes": processes,
            "event_counts": counts,
            "pending_events": pending,
            "pending_events_truncated": counts.get("pending", 0) > len(pending),
            "queue_drained": counts.get("pending", 0) == 0,
            "task_success": None,
            "world_run_id": str(clock["manager_run_id"]) if clock.get("manager_run_id") else None,
            "model_adapter_integrated": adapter["installed"],
        }
