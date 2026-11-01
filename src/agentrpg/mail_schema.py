"""Typed internal mail. These records never cause external email delivery."""

from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints

from agentrpg.schemas import Payload

MailOwner = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")]
MailSubject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
MailBody = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]


class MailContract(Payload):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InitialMailSpec(MailContract):
    id: MailOwner
    sender_owner_id: MailOwner | None = None
    recipient_owner_id: MailOwner
    subject: MailSubject
    body: MailBody


class MailRequest(MailContract):
    """An Actor may ask the Director to send mail under that Actor's identity."""

    recipient_owner_id: MailOwner
    subject: MailSubject
    body: MailBody
    in_reply_to: UUID | None = None


class MailSendBody(MailRequest):
    request_id: UUID


class MailReceiptBody(MailContract):
    message_ids: list[UUID] = Field(min_length=1, max_length=100)
