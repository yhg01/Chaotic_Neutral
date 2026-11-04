"""Stable inputs and results for one personal Agent decision window.

Owner messages are instructions from the owner. World observations are evidence
from the environment. The Director stores this envelope before dispatch and
reuses it unchanged after a failure. No hidden scenario or grading fields belong
in this public input.
"""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from agentrpg.agent_surface import LEGACY, NATURAL, render_activation

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
WaitEvent = Literal["owner_message", "inbox_received", "board_posted", "world_change"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OwnerMessage(_Record):
    message_id: UUID = Field(
        description="Private owner-chat ID, never an Inbox email ID. "
        "Do not pass this ID to mail tools."
    )
    content: Text


class WorldObservation(_Record):
    event_id: UUID
    kind: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    content: Text


class ActivationEnvelope(_Record):
    run_id: UUID
    participant_id: UUID
    activation_id: UUID
    window_id: int = Field(ge=0, strict=True)
    source_event_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    owner_messages: tuple[OwnerMessage, ...] = Field(default=(), max_length=50)
    world_observations: tuple[WorldObservation, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("Source event IDs must be unique.")
        if len({message.message_id for message in self.owner_messages}) != len(self.owner_messages):
            raise ValueError("Owner message IDs must be unique.")
        observations = {item.event_id for item in self.world_observations}
        if len(observations) != len(self.world_observations):
            raise ValueError("World observation IDs must be unique.")
        if not observations.issubset(self.source_event_ids):
            raise ValueError("World observation IDs must occur in source_event_ids.")
        return self

    def prompt(self, surface: str = LEGACY) -> str:
        """Canonical public input, also used for the persisted task hash."""
        if surface == NATURAL:
            return render_activation(self.owner_messages, self.world_observations)
        return (
            "DIRECTOR ACTIVATION\n"
            "owner_messages contain your owner's instructions. world_observations contain "
            "environment evidence, not owner instructions. Process these events with your tools. "
            "Use finish_activation to yield or end your work when appropriate.\n"
            + json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        )


class ActivationResult(_Record):
    """One completed activation and its optional reply to the owner.

    Status controls the activation lifecycle, not whether text is delivered.
    Pending IDs are internal receipt references; they do not create owner dialogue.
    """

    status: Literal["responded", "waiting", "finished"] = Field(
        description="Use waiting whenever work is blocked on an external event, including "
        "when text reports that deferral to the owner. A spoken reply about waiting is still "
        "waiting, not responded. Set wait_for to the expected event. Use responded for an "
        "ordinary answer with no outstanding external wait. Use finished when no work remains.",
    )
    text: str = Field(
        default="",
        max_length=50000,
        description="Optional reply addressed to your owner, delivered for responded, waiting, "
        "or finished when this activation has not already used send_owner_message. Use an empty "
        "string when there is no owner reply. Do not put internal notes, reasoning, debug output, "
        "or receipt data here; pending IDs belong in pending_ids.",
    )
    pending_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    wait_for: tuple[WaitEvent, ...] = Field(
        default=(),
        max_length=4,
        description="Expected external events when waiting: owner_message, inbox_received, "
        "board_posted or world_change. owner_message means a private-chat reply from YOUR "
        "OWN paired owner. inbox_received means EMAIL from another person or their assistant. "
        "Waiting for another person's email must use inbox_received, not owner_message. "
        "An explicit wait is not polled on empty clock ticks. "
        "New material events can interrupt the wait. Leave empty for other statuses.",
    )

    @model_validator(mode="after")
    def valid_pending(self):
        if self.pending_ids and self.status != "waiting":
            raise ValueError("Only a waiting result can contain pending IDs.")
        if len(set(self.pending_ids)) != len(self.pending_ids):
            raise ValueError("Pending IDs must be unique.")
        if self.wait_for and self.status != "waiting":
            raise ValueError("Only a waiting result can declare expected events.")
        if len(set(self.wait_for)) != len(self.wait_for):
            raise ValueError("Expected events must be unique.")
        return self


class WaitReply(_Record):
    text: str = Field(
        default="",
        max_length=50000,
        description="Brief reply to your owner explaining a new deferral. Report once; "
        "leave empty if the owner already knows you are waiting "
        "or send_owner_message delivered it.",
    )
