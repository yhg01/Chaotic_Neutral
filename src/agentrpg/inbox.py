"""Private internal mail with atomic notifications and explicit read receipts."""

import base64
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid5

from pydantic import ValidationError

from agentrpg.director import emit, fingerprint, row, rows
from agentrpg.mail_schema import InitialMailSpec, MailRequest


class InboxError(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def _public(message):
    return {
        key: value
        for key, value in message.items()
        if key not in {"payload_hash", "sender_key", "sequence", "request_id"}
    }


class InboxService:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def _connection(self, connection=None):
        if connection is not None:
            yield connection
        else:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    yield conn

    async def _owner(self, conn, run_id, owner_id):
        if not await row(
            conn,
            "SELECT 1 FROM run_participants WHERE run_id=%s AND owner_id=%s",
            (run_id, owner_id),
        ):
            raise InboxError("Mailbox not found.", 404)

    async def _missing_reference(self, conn, run_id, owner_id, ids):
        # Inspect only this caller's own chat. A foreign private ID stays a 404.
        if await row(
            conn,
            "SELECT 1 FROM owner_messages WHERE run_id=%s AND owner_id=%s AND id=ANY(%s)",
            (run_id, owner_id, ids),
        ):
            raise InboxError(
                "Owner-chat IDs are not Inbox email IDs. Owner chat needs no mail receipt. "
                "Use received email IDs from list_mail/read_mail.",
                422,
            )
        raise InboxError("One or more Inbox email references were not found.", 404)

    async def initialize(self, run_id, initial_mail, *, connection):
        for value in initial_mail:
            item = (
                value
                if isinstance(value, InitialMailSpec)
                else InitialMailSpec.model_validate(value)
            )
            await self.send(
                run_id,
                item.sender_owner_id,
                item.recipient_owner_id,
                item.subject,
                item.body,
                uuid5(UUID(str(run_id)), f"initial-mail:{item.id}"),
                source="actor" if item.sender_owner_id else "director",
                connection=connection,
                allow_pending=True,
            )

    async def send(
        self,
        run_id,
        sender_owner_id,
        recipient_owner_id,
        subject,
        body,
        request_id,
        *,
        source="agent",
        in_reply_to=None,
        connection=None,
        allow_pending=False,
    ):
        try:
            payload = MailRequest(
                recipient_owner_id=recipient_owner_id,
                subject=subject,
                body=body,
                in_reply_to=in_reply_to,
            )
            request_id = UUID(str(request_id))
            run_id = UUID(str(run_id))
        except (ValidationError, ValueError, TypeError):
            raise InboxError("Invalid mail arguments.", 422) from None
        if source not in {"agent", "actor", "director"} or (
            (source == "director") != (sender_owner_id is None)
        ):
            raise InboxError("Invalid sender attribution.", 422)
        if allow_pending and (connection is None or source == "agent"):
            raise InboxError("Initialization requires a Director transaction.", 422)
        sender_key = f"{source}:{sender_owner_id or ''}"
        message_id = uuid5(run_id, f"mail:{sender_key}:{request_id}")
        payload_hash = fingerprint(payload.model_dump(mode="json"))
        async with self._connection(connection) as conn:
            run = await row(
                conn,
                "SELECT status,current_window FROM environment_runs WHERE id=%s FOR SHARE",
                (run_id,),
            )
            if run is None:
                raise InboxError("Run not found.", 404)
            if sender_owner_id is not None:
                await self._owner(conn, run_id, sender_owner_id)
            existing = await row(conn, "SELECT * FROM inbox_messages WHERE id=%s", (message_id,))
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise InboxError("The mail request ID has different content.")
                return _public(existing)
            if run["status"] != "running" and not (allow_pending and run["status"] == "pending"):
                raise InboxError("The run is not running.")
            await self._owner(conn, run_id, payload.recipient_owner_id)
            if payload.in_reply_to is not None:
                original = await row(
                    conn,
                    "SELECT * FROM inbox_messages WHERE run_id=%s AND id=%s "
                    "AND (sender_owner_id=%s OR recipient_owner_id=%s)",
                    (run_id, payload.in_reply_to, sender_owner_id, sender_owner_id),
                )
                if original is None:
                    raise InboxError("The reply message was not found in this mailbox.", 404)
                # A reply stays between the participants of the original message.
                counterpart = (
                    original["recipient_owner_id"]
                    if original["sender_owner_id"] == sender_owner_id
                    else original["sender_owner_id"]
                )
                if payload.recipient_owner_id != counterpart:
                    raise InboxError("A reply must address the other mail participant.", 422)
            await conn.execute(
                """INSERT INTO inbox_messages
                (id,run_id,sender_owner_id,sender_source,sender_key,recipient_owner_id,
                 subject,body,in_reply_to,request_id,payload_hash,"window")
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                (
                    message_id,
                    run_id,
                    sender_owner_id,
                    source,
                    sender_key,
                    payload.recipient_owner_id,
                    payload.subject,
                    payload.body,
                    payload.in_reply_to,
                    request_id,
                    payload_hash,
                    run["current_window"],
                ),
            )
            existing = await row(conn, "SELECT * FROM inbox_messages WHERE id=%s", (message_id,))
            if existing["payload_hash"] != payload_hash:
                raise InboxError("The mail request ID has different content.")
            await emit(
                conn,
                run_id,
                "inbox_received",
                existing["window"],
                {
                    "message_id": str(message_id),
                    "sender_owner_id": sender_owner_id,
                    "recipient_owner_id": payload.recipient_owner_id,
                    "source": {"agent": "assistant"}.get(source, source),
                    "subject": payload.subject,
                },
                f"mail:{message_id}",
                payload.recipient_owner_id,
            )
            from agentrpg.transfer_tasks import TransferTaskService

            await TransferTaskService.received_mail(conn, existing)
            return _public(existing)

    async def list_messages(
        self,
        run_id,
        owner_id,
        *,
        mailbox="inbox",
        unread_only=False,
        query=None,
        limit=20,
        cursor=None,
        connection=None,
    ):
        if (
            mailbox not in {"inbox", "sent", "all"}
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise InboxError("Invalid mailbox query.", 422)
        if type(unread_only) is not bool or (unread_only and mailbox != "inbox"):
            raise InboxError("Unread queries require the received inbox.", 422)
        if query is not None and (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 200
            or "\x00" in query
            or any(0xD800 <= ord(char) <= 0xDFFF for char in query)
        ):
            raise InboxError("Search text must contain 1 to 200 characters.", 422)
        query = query.strip() if query is not None else None
        scope = fingerprint([str(run_id), owner_id, mailbox, unread_only, query])
        before = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 512:
                    raise ValueError
                decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if (
                    decoded["scope"] != scope
                    or type(decoded["before"]) is not int
                    or not 1 <= decoded["before"] <= 2**63 - 1
                ):
                    raise ValueError
                before = decoded["before"]
            except (ValueError, TypeError, KeyError, UnicodeDecodeError):
                raise InboxError("Invalid mail history cursor.", 422) from None
        condition = {
            "inbox": "m.recipient_owner_id=%s",
            "sent": "m.sender_owner_id=%s",
            "all": "(m.recipient_owner_id=%s OR m.sender_owner_id=%s)",
        }[mailbox]
        args = [run_id, owner_id]
        if mailbox == "all":
            args.append(owner_id)
        if unread_only:
            condition += " AND r.message_id IS NULL"
        if before is not None:
            condition += " AND m.sequence < %s"
            args.append(before)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            condition += " AND (m.subject ILIKE %s OR m.body ILIKE %s)"
            args.extend([f"%{escaped}%", f"%{escaped}%"])
        args.append(limit + 1)
        async with self._connection(connection) as conn:
            await self._owner(conn, run_id, owner_id)
            items = await rows(
                conn,
                "SELECT m.*,r.read_at FROM inbox_messages m LEFT JOIN inbox_receipts r "
                "ON r.run_id=m.run_id AND r.message_id=m.id AND r.owner_id=m.recipient_owner_id "
                f"WHERE m.run_id=%s AND {condition} ORDER BY m.sequence DESC LIMIT %s",
                tuple(args),
            )
        next_cursor = None
        if len(items) > limit:
            value = json.dumps({"scope": scope, "before": items[limit - 1]["sequence"]}).encode()
            next_cursor = base64.urlsafe_b64encode(value).decode().rstrip("=")
        return {"items": [_public(item) for item in items[:limit]], "next_cursor": next_cursor}

    async def read(self, run_id, owner_id, message_id):
        async with self._connection() as conn:
            await self._owner(conn, run_id, owner_id)
            message = await row(
                conn,
                "SELECT m.*,r.read_at FROM inbox_messages m LEFT JOIN inbox_receipts r "
                "ON r.run_id=m.run_id AND r.message_id=m.id AND r.owner_id=m.recipient_owner_id "
                "WHERE m.run_id=%s AND m.id=%s "
                "AND (m.recipient_owner_id=%s OR m.sender_owner_id=%s)",
                (run_id, message_id, owner_id, owner_id),
            )
            if message is None:
                await self._missing_reference(conn, run_id, owner_id, [UUID(str(message_id))])
            return _public(message)

    async def acknowledge(self, run_id, owner_id, message_ids):
        if not isinstance(message_ids, (list, tuple)) or not 1 <= len(message_ids) <= 100:
            raise InboxError("Supply 1 to 100 mail IDs.", 422)
        try:
            ids = list(dict.fromkeys(UUID(str(value)) for value in message_ids))
        except (TypeError, ValueError):
            raise InboxError("Invalid mail ID.", 422) from None
        async with self._connection() as conn:
            await self._owner(conn, run_id, owner_id)
            found = await rows(
                conn,
                "SELECT id FROM inbox_messages WHERE run_id=%s "
                "AND recipient_owner_id=%s AND id=ANY(%s)",
                (run_id, owner_id, ids),
            )
            if len(found) != len(ids):
                await self._missing_reference(conn, run_id, owner_id, ids)
            for message_id in ids:
                await conn.execute(
                    "INSERT INTO inbox_receipts(run_id,message_id,owner_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (run_id, message_id, owner_id),
                )
        return {"acknowledged_ids": [str(value) for value in ids]}

    async def actor_view(self, run_id, owner_id, limit=20, *, connection=None):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise InboxError("Actor mail limit must be between 1 and 20.", 422)
        result = await self.list_messages(
            run_id,
            owner_id,
            limit=limit,
            connection=connection,
        )
        # The Actor receives observations, not an unbounded email archive.
        # Keep the original history cursor and every returned message identity.
        # Only this private observation copy has shortened body text.
        bodies = [item["body"] for item in result["items"]]
        for item in result["items"]:
            item.update(body="", body_truncated=True)
        remaining = 24000 - len(json.dumps(result, default=str)) - len(bodies)
        for index, (item, body) in enumerate(zip(result["items"], bodies, strict=True)):
            allowance = max(0, remaining // (len(bodies) - index))
            low, high = 0, min(4000, len(body))
            while low < high:
                midpoint = (low + high + 1) // 2
                if len(json.dumps(body[:midpoint])) - 2 <= allowance:
                    low = midpoint
                else:
                    high = midpoint - 1
            excerpt = body[:low]
            item.update(body=excerpt, body_truncated=low < len(body))
            remaining -= len(json.dumps(excerpt)) - 2
        return result
