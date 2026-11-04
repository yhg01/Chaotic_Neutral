"""Typed owner task requests and evidence-bound assistant updates."""

from typing import Literal

from pydantic import Field, model_validator

from agentrpg.agent_tools import Identifier as UUIDArgument
from agentrpg.agent_tools import _Arguments
from agentrpg.scenario_schema import Contract, Identifier


class TransferTaskRequest(Contract):
    kind: Literal["open", "cancel"]
    task_key: Identifier
    recipient_owner_id: Identifier | None = None
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    blocked_on: Literal["inbox_received", "owner_message"] | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "open":
            if not self.recipient_owner_id or not self.description or not self.blocked_on:
                raise ValueError("Opening a task requires its recipient, purpose and wait event.")
        elif any((self.recipient_owner_id, self.description, self.blocked_on)):
            raise ValueError("Cancellation only names an existing task key.")
        return self


class DeferTransferTask(_Arguments):
    task_id: UUIDArgument
    wait_for: Literal["inbox_received", "owner_message"]


class CompleteTransferTask(_Arguments):
    task_id: UUIDArgument
    receipt_id: UUIDArgument
    recipient_notice_id: UUIDArgument
    owner_report_id: UUIDArgument
