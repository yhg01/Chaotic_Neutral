"""The in-process gate that keeps world tool calls in story order.

Model calls read only private history, so they overlap. A world tool call parks here
until the conductor releases it as the lowest event in simulated time. This state lives
in memory. After a crash it is rebuilt from saved messages, because the stamps of a call
come only from those messages (R6). The gate holds no database connection, no credential,
and no prompt text.

A registered call that never arrives clears in one of four ways. The next model call
drops it, because a model call means that its batch has joined. The end of the activation
drops it. A failed attempt keeps it, so that the retry takes the same slot in story time.
The conductor drops it after ARRIVAL_TIMEOUT_S of real time if nothing else did.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field

from agentrpg.clock import (
    AGENT_TOOL_CLASS,
    ClockConfig,
    StampedCall,
    actor_bound,
    fold_stamps,
    is_gated,
    order_key,
    think_bound,
    tie,
)

THINKING = "thinking"
ACTING = "acting"
SETTLING = "settling"

# Longer than the longest retry wait, so a call waiting out an outage keeps its slot.
ARRIVAL_TIMEOUT_S = 600.0


@dataclass(eq=False)
class ParkedCall:
    """One gated tool call, registered in story order and maybe not yet arrived."""

    process: str
    call_id: str
    name: str
    stamp_s: int
    duration_s: int
    key: str
    tie: bytes
    arrived: bool = False
    running: bool = False
    completed: bool = False
    registered_at: float = field(default_factory=time.monotonic)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def order(self) -> tuple:
        return order_key(self.stamp_s, AGENT_TOOL_CLASS, self.tie, self.key)


@dataclass(eq=False)
class _Process:
    name: str
    kind: str
    phase: str
    bar_s: int
    confirmed_bar_s: int
    start_s: int
    activation_id: object = None
    seq: int = 0
    entries: dict[str, ParkedCall] = field(default_factory=dict)
    replays: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ClockGate:
    """Thinker bounds and parked calls for one run. One conductor and one event loop use it."""

    def __init__(
        self,
        config: ClockConfig,
        *,
        saved_calls: Callable[[str, object], Awaitable[Collection[str]]] | None = None,
    ) -> None:
        self.config = config
        self.saved_calls = saved_calls
        self.generation = 0
        self._changed = asyncio.Event()
        self._gated: dict[str, frozenset[str]] = {}
        self._processes: dict[str, _Process] = {}
        self._slots = asyncio.Semaphore(config.max_parallel_thinkers)
        self.calls_in_flight = 0
        self.peak_calls_in_flight = 0
        # Real work that came back. The conductor's circuit breaker reads this.
        self.successes = 0

    # Who is doing what -------------------------------------------------------------------

    def register_tools(self, process: str, names: Collection[str]) -> None:
        """Name the world tools of one Agent. Everything else that it calls is free."""
        self._gated[process] = frozenset(names)

    def gated_names(self, process: str) -> frozenset[str] | None:
        return self._gated.get(process)

    def begin_activation(
        self, process: str, activation_id, seq: int, start_s: int, *, resume_bar: int | None = None
    ) -> None:
        """Start or resume one activation.

        A resumed activation can have a saved call stamped exactly at its saved bar. It
        blocks from that bar, not from bar plus think time, until it reports. That is
        the recovery barrier, and it needs no saved gate state.
        """
        resumed = resume_bar is not None
        bar = resume_bar if resumed else start_s
        self._processes[process] = _Process(
            name=process,
            kind="agent",
            phase=SETTLING if resumed else THINKING,
            bar_s=bar,
            confirmed_bar_s=bar,
            start_s=start_s,
            activation_id=activation_id,
            seq=seq,
        )
        self._bump()

    def begin_actor(self, process: str, start_s: int) -> None:
        """An owner forms a reply from a view frozen at start_s."""
        self._processes[process] = _Process(
            name=process,
            kind="actor",
            phase=THINKING,
            bar_s=start_s,
            confirmed_bar_s=start_s,
            start_s=start_s,
        )
        self._bump()

    def end(self, process: str) -> None:
        """The process reported, or stopped. It no longer blocks anything."""
        state = self._processes.pop(process, None)
        if state is None:
            return
        for entry in state.entries.values():
            entry.finished.set()
        self._bump()

    # Hooks of the graph ------------------------------------------------------------------

    def _fold(self, state: _Process, messages: Sequence):
        return fold_stamps(
            state.start_s,
            messages,
            state.activation_id,
            self.config,
            gated=self._gated.get(state.name),
        )

    def before_model(self, process: str, messages: Sequence) -> None:
        """A model call follows. Any call that has not arrived can no longer arrive."""
        state = self._processes.get(process)
        if state is None:
            return
        for entry in state.entries.values():
            entry.finished.set()
        state.entries.clear()
        state.bar_s = state.confirmed_bar_s = self._fold(state, messages).end_s
        state.phase = THINKING
        self._bump()

    def after_model(self, process: str, messages: Sequence, ai_message) -> None:
        """Move from thinking to acting and register every gated call with its stamp."""
        state = self._processes.get(process)
        if state is None:
            return
        self.successes += 1
        fold = self._fold(state, [*messages, ai_message])
        state.bar_s = fold.end_s
        for call in fold.last_message_calls:
            if call.gated:
                self._register(state, call)
        state.phase = ACTING if state.entries else SETTLING
        self._bump()

    def _register(self, state: _Process, call: StampedCall) -> None:
        key = f"tool:{state.name}:{state.seq}:{call.message_index}:{call.call_index}"
        state.entries[call.call_id] = ParkedCall(
            process=state.name,
            call_id=call.call_id,
            name=call.name,
            stamp_s=call.stamp_s,
            duration_s=call.duration_s,
            key=key,
            tie=tie(self.config.seed, key),
        )

    async def _register_arrivals(self, state: _Process, messages: Sequence) -> None:
        """Registration at first arrival. After a crash no after_model has run."""
        async with state.lock:
            fold = self._fold(state, messages)
            saved = (
                await self.saved_calls(state.name, state.activation_id)
                if self.saved_calls is not None
                else ()
            )
            answered = {
                getattr(message, "tool_call_id", None)
                for message in messages
                if getattr(message, "type", None) == "tool"
            }
            state.replays.update(call_id for call_id in saved if call_id not in answered)
            # Only a result after the last model message answers its calls. A provider can
            # reuse a call ID, and an older result must not let this call skip its turn.
            current = set()
            for message in reversed(messages):
                if getattr(message, "type", None) == "ai":
                    break
                if getattr(message, "type", None) == "tool":
                    current.add(getattr(message, "tool_call_id", None))
            for call in fold.last_message_calls:
                if (
                    call.gated
                    and call.call_id not in saved
                    and call.call_id not in current
                    and call.call_id not in state.entries
                ):
                    self._register(state, call)
            state.bar_s = state.confirmed_bar_s = fold.end_s
            if state.entries:
                state.phase = ACTING
            self._bump()

    async def before_tool(self, process: str, tool_call: dict, messages: Sequence) -> None:
        """Park until released. A free tool or a call with a saved result returns at once."""
        state = self._processes.get(process)
        if state is None or not is_gated(tool_call.get("name") or "", self._gated.get(process)):
            return
        call_id = str(tool_call.get("id"))
        entry = state.entries.get(call_id)
        if entry is None:
            await self._register_arrivals(state, messages)
            entry = state.entries.get(call_id)
            if entry is None:
                return
        entry.arrived = True
        self._bump()
        try:
            await entry.released.wait()
        except asyncio.CancelledError:
            self._reset(entry)
            self._bump()
            raise
        entry.running = True

    def is_replay(self, process: str, call_id: str) -> bool:
        """This call committed before a crash. Its saved result stands; it does not run again."""
        state = self._processes.get(process)
        return state is not None and call_id in state.replays

    def after_tool(self, process: str, tool_call: dict) -> None:
        """The released call has reported. Its effect is committed."""
        state = self._processes.get(process)
        if state is None:
            return
        entry = state.entries.pop(str(tool_call.get("id")), None)
        if entry is None:
            return
        self.successes += 1
        entry.completed = True
        entry.running = False
        entry.finished.set()
        if not state.entries and state.phase == ACTING:
            state.phase = SETTLING
        self._bump()

    def tool_failed(self, process: str, tool_call: dict) -> None:
        """The call failed or was cancelled. It keeps its slot for the retry."""
        state = self._processes.get(process)
        if state is None:
            return
        entry = state.entries.get(str(tool_call.get("id")))
        if entry is None:
            return
        self._reset(entry)
        self._bump()

    @staticmethod
    def _reset(entry: ParkedCall) -> None:
        finished = entry.finished
        entry.arrived = False
        entry.running = False
        entry.released = asyncio.Event()
        entry.finished = asyncio.Event()
        entry.registered_at = time.monotonic()
        finished.set()

    def attempt_failed(self, process: str) -> None:
        """One attempt ended with an error. Retries keep the same IDs and the same stamps.

        Registered calls keep their slots. Without them the process blocks from its last
        saved bar, because a message that was not saved may have raised the current bar.
        """
        state = self._processes.get(process)
        if state is None:
            return
        for entry in state.entries.values():
            self._reset(entry)
        state.bar_s = state.confirmed_bar_s
        state.phase = ACTING if state.entries else SETTLING
        self._bump()

    def think_slot(self) -> _ThinkSlot:
        """At most max_parallel_thinkers model calls run at one time."""
        return _ThinkSlot(self)

    # The conductor's view ----------------------------------------------------------------

    def head(self) -> ParkedCall | None:
        """The lowest registered call, arrived or not."""
        entries = [e for state in self._processes.values() for e in state.entries.values()]
        return min(entries, key=lambda entry: entry.order) if entries else None

    def bound(self, process: str) -> int | None:
        """R5: the earliest stamp that this process could still produce, if it blocks."""
        state = self._processes.get(process)
        if state is None or state.phase == ACTING:
            return None
        if state.phase == SETTLING:
            return state.bar_s
        if state.kind == "actor":
            return actor_bound(state.start_s, self.config)
        return think_bound(state.bar_s, self.config)

    def allows(self, stamp_s: int) -> bool:
        """R5: an item at this stamp may go only below the bound of every blocking process."""
        for name in self._processes:
            bound = self.bound(name)
            if bound is not None and stamp_s >= bound:
                return False
        return True

    def blocking(self) -> int:
        """How many processes can still produce an earlier event."""
        return sum(1 for name in self._processes if self.bound(name) is not None)

    def active(self) -> bool:
        return bool(self._processes)

    def is_active(self, process: str) -> bool:
        return process in self._processes

    def release(self, entry: ParkedCall) -> None:
        """The conductor has committed story time. Wake the parked coroutine."""
        entry.released.set()

    def drop(self, entry: ParkedCall) -> None:
        """Forget a registered call that never arrived."""
        state = self._processes.get(entry.process)
        if state is not None:
            state.entries.pop(entry.call_id, None)
            if not state.entries and state.phase == ACTING:
                state.phase = SETTLING
        entry.finished.set()
        self._bump()

    def waited(self, entry: ParkedCall) -> float:
        return time.monotonic() - entry.registered_at

    def clear_changed(self) -> None:
        self._changed.clear()

    def notify(self) -> None:
        self._bump()

    async def wait_changed(self, seconds: float = 1.0) -> None:
        """Wait for any state change. The limit only rechecks; it decides nothing."""
        try:
            async with asyncio.timeout(seconds):
                await self._changed.wait()
        except TimeoutError:
            return

    def _bump(self) -> None:
        self.generation += 1
        self._changed.set()


class _ThinkSlot:
    def __init__(self, gate: ClockGate) -> None:
        self.gate = gate

    async def __aenter__(self) -> None:
        await self.gate._slots.acquire()
        self.gate.calls_in_flight += 1
        self.gate.peak_calls_in_flight = max(
            self.gate.peak_calls_in_flight, self.gate.calls_in_flight
        )

    async def __aexit__(self, *args) -> None:
        self.gate.calls_in_flight -= 1
        self.gate._slots.release()
