"""Typed internal mail. These records never cause external email delivery."""

import re
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints

from agentrpg.schemas import Payload

MailOwner = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")]
# On a clocked run an assistant can write to anyone: an owner_id, a name, or an address.
MailAddress = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]
MailSubject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
MailBody = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]


_FROM_LINE = re.compile(r"\AFrom: *([^\n]{1,160})\n")


def mail_sender(body: str) -> str | None:
    """The sender named by the From line of mail from outside the run, if it has one."""
    match = _FROM_LINE.match(body or "")
    return match[1].strip() if match else None


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


class AddressedMailRequest(MailContract):
    """The same request with a free address. The service accepts it only on a clocked run."""

    recipient_owner_id: MailAddress
    subject: MailSubject
    body: MailBody
    in_reply_to: UUID | None = None


class MailSendBody(AddressedMailRequest):
    request_id: UUID


class MailReceiptBody(MailContract):
    message_ids: list[UUID] = Field(min_length=1, max_length=100)
