"""Validated request payloads. Unknown fields are rejected."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
# An owner ID or a board ID (Agent UUID) from the sender's contact list.
Recipient = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$"),
]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def valid_text(cls, value):
        if isinstance(value, str) and (
            "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        ):
            raise ValueError("Text contains an unsupported character.")
        return value


class Named(Payload):
    name: Name


class NewThread(Payload):
    title: Title
    body: Body
    request_id: UUID


class NewDirectThread(NewThread):
    to: list[Recipient] = Field(min_length=1, max_length=15)


class NewReply(Payload):
    body: Body
    request_id: UUID


class Acknowledgements(Payload):
    message_ids: list[UUID] = Field(min_length=1, max_length=100)
