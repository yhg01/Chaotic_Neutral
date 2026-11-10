"""Private Actor cognition; intentions and inferences never authorize world effects."""

from datetime import date
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from agentrpg.scenario_schema import Contract

MEMORY_PROTOCOL = "actor-memory-agenda-v1"


class MemoryProfile(Contract):
    protocol: Literal["actor-memory-agenda-v1"] = MEMORY_PROTOCOL
    selection_limit: int = Field(default=8, strict=True, ge=1, le=20)
    context_chars: int = Field(default=6000, strict=True, ge=500, le=12000)
    observation_ttl_seconds: int = Field(default=604800, strict=True, ge=1, le=604800)
    thought_ttl_limit_seconds: int = Field(default=86400, strict=True, ge=1, le=604800)
    recency_half_life_seconds: int = Field(default=86400, strict=True, ge=1, le=604800)
    max_memories_per_owner: int = Field(default=10000, strict=True, ge=10, le=100000)


class PrivateThought(Contract):
    text: str = Field(min_length=1, max_length=1000)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=10)
    importance: int = Field(default=5, strict=True, ge=1, le=10)
    valid_for_seconds: int = Field(default=86400, strict=True, ge=1, le=604800)
    supersedes: UUID | None = None


class AgendaSlot(Contract):
    start_minute: int = Field(strict=True, ge=0, le=1439)
    end_minute: int = Field(strict=True, ge=1, le=1440)
    activity: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def positive(self) -> Self:
        if self.end_minute <= self.start_minute:
            raise ValueError("An agenda slot must have a positive duration.")
        return self


class DailyAgenda(Contract):
    day: date
    slots: tuple[AgendaSlot, ...] = Field(max_length=24)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if any(
            a.end_minute > b.start_minute for a, b in zip(self.slots, self.slots[1:], strict=False)
        ):
            raise ValueError("Agenda slots must be ordered and cannot overlap.")
        return self


class CognitionUpdate(Contract):
    thoughts: tuple[PrivateThought, ...] = Field(default=(), max_length=5)
    agendas: tuple[DailyAgenda, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def unique_days(self) -> Self:
        if len({p.day for p in self.agendas}) != len(self.agendas):
            raise ValueError("Only one agenda revision per day is allowed in a response.")
        return self


def validate_cognition(context, update):
    if update is None:
        return
    if not context or context.get("protocol") != MEMORY_PROTOCOL:
        raise ValueError("Cognition updates require the explicitly enabled Actor profile.")
    allowed = set(context["allowed_evidence"])
    thoughts = {m["id"] for m in context["memories"] if m["kind"] == "thought"}
    for thought in update.thoughts:
        if not set(thought.evidence) <= allowed:
            raise ValueError("Thought evidence must come from this owner's frozen visible context.")
        if thought.valid_for_seconds > context["thought_ttl_limit_seconds"]:
            raise ValueError("Thought expiry exceeds the frozen profile limit.")
        if thought.supersedes and str(thought.supersedes) not in thoughts:
            raise ValueError("Only a selected own thought may be superseded.")
    for agenda in update.agendas:
        if agenda.day.isoformat() not in context["planning_days"]:
            raise ValueError("Agenda revisions are limited to the current or next UTC day.")
