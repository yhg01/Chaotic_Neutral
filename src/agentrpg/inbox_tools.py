"""Internal email tools. Identity comes from the Agent's authenticated client."""

import asyncio
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import Field, ValidationError, model_validator

from agentrpg.agent_tools import Cursor, Identifier, Limit, _Arguments, _error
from agentrpg.mail_schema import MailAddress, MailBody, MailOwner, MailSubject


class _ListMail(_Arguments):
    mailbox: Literal["inbox", "sent", "all"] = "inbox"
    unread_only: bool = False
    query: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    limit: Limit = 20
    cursor: Cursor | None = None

    @model_validator(mode="after")
    def received_unread_only(self):
        if self.unread_only and self.mailbox != "inbox":
            raise ValueError("unread_only requires mailbox='inbox'.")
        return self


class _ReadMail(_Arguments):
    message_id: Identifier = Field(
        description="Inbox email id from list_mail or send_mail, "
        "not an owner-chat message_id, event_id, action receipt or board post ID."
    )


class _SendMail(_Arguments):
    recipient_owner_id: MailOwner
    subject: MailSubject
    body: MailBody
    in_reply_to: Identifier | None = None


class _ClockedSendMail(_SendMail):
    """A clocked assistant can write to anyone, and the world decides what comes back."""

    recipient_owner_id: MailAddress = Field(
        description="A contact's owner_id, or anyone else's name or email address."
    )


class _AcknowledgeMail(_Arguments):
    message_ids: list[Identifier] = Field(
        min_length=1,
        max_length=100,
        description="Only IDs of received Inbox emails from list_mail/read_mail. "
        "Never use owner-chat, event, sent-email or board IDs. Owner chat needs no mail receipt.",
    )


INBOX_TOOLS = {
    "list_mail": (
        _ListMail,
        "Read or search your private internal mailbox, newest first. mailbox selects inbox, sent, "
        "or all. unread_only requires inbox. Use next_cursor for history pages. "
        "Unread queries without a cursor find all unacknowledged mail, including late commits. "
        "Reading does not acknowledge mail. This is simulated internal email only.",
    ),
    "read_mail": (
        _ReadMail,
        "Read one received or sent internal email by its Inbox id. "
        "Owner-chat IDs are not email IDs.",
    ),
    "send_mail": (
        _SendMail,
        "Send private internal email to an owner_id from read_run_view.participants. "
        "Board author_id is an Agent UUID, not an owner_id. Read the directory first; "
        "do not guess. Your sender identity is fixed. "
        "Set in_reply_to to reply to mail in your mailbox, addressed to its other participant. "
        "Use plain text. A successful response confirms delivery to that private inbox.",
    ),
    "acknowledge_mail": (
        _AcknowledgeMail,
        "Explicitly mark received internal emails as read after processing them. "
        "Use only received email IDs from list_mail/read_mail. Owner-chat messages, events "
        "and your sent emails cannot be acknowledged here. The sender can see this receipt. "
        "Repeated acknowledgements are safe.",
    ),
}


class InboxTools:
    def __init__(
        self, client: httpx.AsyncClient, run_id, operation_namespace=None, *, clocked=False
    ):
        self.client = client
        self.clocked = clocked
        self.run_id = str(UUID(str(run_id)))
        if operation_namespace is not None and not isinstance(operation_namespace, UUID):
            raise ValueError("The operation namespace must be a UUID.")
        self.operation_namespace = operation_namespace or uuid4()

    def definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in INBOX_TOOLS.items()
        ]

    async def execute(self, name, arguments, call_id):
        if not isinstance(name, str) or name not in INBOX_TOOLS:
            return _error(422, "Unknown inbox tool.")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or len(call_id) > 512
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in call_id)
        ):
            return _error(422, "A valid tool call ID is required.")
        if not isinstance(arguments, dict):
            return _error(422, "Tool arguments must be an object.")
        model = _ClockedSendMail if self.clocked and name == "send_mail" else INBOX_TOOLS[name][0]
        try:
            payload = model.model_validate(arguments).model_dump(
                mode="json",
                exclude_none=True,
            )
        except ValidationError:
            if name == "list_mail" and arguments.get("unread_only") is True:
                return _error(
                    422,
                    "Invalid mailbox query. unread_only=true requires mailbox='inbox'. "
                    "For mailbox='sent' or 'all', use unread_only=false. "
                    "Check the remaining tool parameters against the schema.",
                )
            return _error(422, "Invalid tool arguments. Follow the tool's parameter schema.")
        method, path = "GET", f"runs/{self.run_id}/inbox"
        if name == "read_mail":
            path += "/" + payload.pop("message_id")
        elif name == "send_mail":
            method = "POST"
            payload["request_id"] = str(uuid5(self.operation_namespace, call_id))
        elif name == "acknowledge_mail":
            method, path = "POST", path + "/receipts"
        for attempt in range(3):
            try:
                response = await self.client.request(
                    method,
                    path,
                    **({"params": payload} if method == "GET" else {"json": payload}),
                    follow_redirects=False,
                )
            except httpx.RequestError:
                if attempt == 2:
                    return _error(503, "The inbox could not be reached. Retry later.")
            else:
                if response.status_code != 503 or attempt == 2:
                    if not response.is_success:
                        detail = {
                            401: "The Agent credential was rejected.",
                            403: "This inbox operation is not permitted.",
                            404: (
                                "The email you are replying to was not found in this mailbox."
                                if self.clocked and name == "send_mail"
                                else "Recipient not found. Use owner_id from "
                                "read_run_view.participants, "
                                "not a board Agent UUID."
                                if name == "send_mail"
                                else "Inbox email reference not found or not accessible. "
                                "Use received email id from list_mail/read_mail. Owner-chat IDs "
                                "and event IDs are not mail IDs; owner chat needs no mail receipt. "
                                "This is a reference error, not evidence of a service outage."
                            ),
                            409: "The operation conflicts with the saved mail or run state.",
                            422: (
                                "That email could not be sent. A reply goes to the other person "
                                "on the email it answers, and in_reply_to must be the ID of an "
                                "email in this mailbox."
                                if self.clocked and name == "send_mail"
                                else "The inbox rejected the query. unread_only requires mailbox="
                                "'inbox'; check mailbox, filter, limit and cursor arguments."
                                if name == "list_mail"
                                else "The inbox rejected the arguments. Mail IDs must refer "
                                "to Inbox emails, never owner-chat messages, events or board posts."
                            ),
                            503: "The inbox is unavailable. Retry later.",
                        }.get(response.status_code, "The inbox request failed.")
                        return _error(response.status_code, detail)
                    try:
                        data = response.json()
                    except (ValueError, UnicodeDecodeError):
                        return _error(502, "The inbox returned an invalid response.")
                    if not isinstance(data, dict):
                        return _error(502, "The inbox returned an invalid response.")
                    return {"ok": True, "data": data}
            await asyncio.sleep(0.1 * (attempt + 1))
        raise AssertionError("The bounded request loop must return a result.")
