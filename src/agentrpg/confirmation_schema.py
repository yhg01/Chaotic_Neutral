"""Explicit, opt-in owner confirmation contracts; no inferred peer consent."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from agentrpg.scenario_schema import Contract, Identifier

CONFIRMATION_PROTOCOL = "owner-confirmation-receipt-v1"


class ConfirmationSpec(Contract):
    task_key: Identifier
    owner_id: Identifier
    peer_owner_id: Identifier
    terms: str = Field(min_length=1, max_length=2000)
    condition: str = Field(min_length=1, max_length=2000)
    activation_event_key: Identifier | None = None

    @model_validator(mode="after")
    def different_people(self):
        if self.owner_id == self.peer_owner_id:
            raise ValueError("A confirmation requires two different owners.")
        return self


class ConfirmationAction(Contract):
    task_id: UUID
    kind: Literal["request", "confirm", "decline"]


class ConfirmationTool(Contract):
    task_id: UUID


class ConfirmationToolBody(ConfirmationTool):
    request_id: UUID


CONFIRMATION_TOOLS = {
    "list_confirmations": (
        Contract,
        "Read your scoped confirmation tasks and actual evidence IDs.",
    ),
    "request_confirmation": (
        ConfirmationTool,
        "Send the owner's complete terms and condition once. Pending is not agreement.",
    ),
    "ask_confirmation_owner": (
        ConfirmationTool,
        "Ask your owner for a decision on a received request once. Wait for owner_message; "
        "never invent their approval.",
    ),
    "reply_confirmation": (
        ConfirmationTool,
        "Send your owner's recorded confirm/decline decision as a real reply to this exact "
        "request. Owner approval is required.",
    ),
    "report_confirmation": (
        ConfirmationTool,
        "Deliver a canonical owner report. The requester can report completion only after "
        "reading and acknowledging the actual peer reply. Retries reuse the report.",
    ),
}
