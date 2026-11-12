"""Atomic simulated credit transfers under exact, one-use owner grants."""

from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid5

from pydantic import Field, StringConstraints

from agentrpg.agent_tools import Identifier, _Arguments
from agentrpg.director import emit, row, rows
from agentrpg.inbox import InboxService
from agentrpg.transfer_tasks import TransferTaskService
from agentrpg.world import WorldError, WorldService


class TransferArguments(_Arguments):
    authorization_id: Identifier
    recipient_owner_id: Annotated[
        str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    ]
    amount: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    memo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]


def _notice(run, owner_id, recipient, amount, memo, request_id):
    """The subject and body of the recipient's mail. A clocked run reads like a bank's message."""
    scenario = run.get("scenario") or {}
    if not scenario.get("clock"):
        return (
            "Simulated credit transfer received",
            f"{owner_id} transferred {amount} simulated credits. "
            f"Memo: {memo}. Receipt: {request_id}.",
        )
    names = {item["id"]: item["name"] for item in scenario.get("owners", ())}
    return (
        "Payment received",
        f"From: Your bank\n\n{names.get(owner_id, owner_id)} sent you ${amount}. Note: {memo}.",
    )


class TransferService:
    def __init__(self, pool):
        self.pool = pool
        self.world = WorldService(pool)

    @asynccontextmanager
    async def _connection(self, connection=None):
        if connection is not None:
            yield connection
        else:
            async with self.pool.connection() as conn, conn.transaction():
                yield conn

    async def authorize(
        self,
        run_id,
        owner_id,
        owner_message_id,
        arguments: TransferArguments,
        *,
        connection=None,
        task_key=None,
    ):
        """Trusted owner-input path only. No Agent HTTP endpoint exposes this method.

        The operator supplies a typed instruction, not model-extracted authority.
        One owner message can authorize exactly one immutable payment.
        """
        payload = arguments.model_dump(mode="json")
        async with self._connection(connection) as conn:
            run = await self.world._run(conn, run_id)
            self.world._active(run)
            message = await row(
                conn, "SELECT * FROM owner_messages WHERE id=%s", (owner_message_id,)
            )
            if not message or (
                message["run_id"] != UUID(str(run_id))
                or message["owner_id"] != owner_id
                or message["source"] != "owner"
            ):
                raise WorldError("An instruction from this owner is required.", 403)
            if owner_id == arguments.recipient_owner_id:
                raise WorldError("The recipient must be another owner.", 422)
            await self.world._account(conn, run_id, arguments.recipient_owner_id)
            prior = await row(
                conn,
                "SELECT * FROM transfer_grants WHERE run_id=%s AND owner_id=%s "
                "AND (authorization_id=%s OR owner_message_id=%s)",
                (run_id, owner_id, arguments.authorization_id, owner_message_id),
            )
            if prior:
                if prior["owner_message_id"] != UUID(str(owner_message_id)) or any(
                    str(prior[key]) != str(value) for key, value in payload.items()
                ):
                    raise WorldError("This owner instruction already has a different grant.")
                await TransferTaskService(self.pool).bind_authorization(
                    conn, run_id, owner_id, task_key, arguments.authorization_id
                )
                return
            await conn.execute(
                "INSERT INTO transfer_grants "
                "(run_id,owner_id,authorization_id,recipient_owner_id,amount,memo,"
                "owner_message_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    owner_id,
                    arguments.authorization_id,
                    arguments.recipient_owner_id,
                    arguments.amount,
                    arguments.memo,
                    owner_message_id,
                ),
            )
            await TransferTaskService(self.pool).bind_authorization(
                conn, run_id, owner_id, task_key, arguments.authorization_id
            )

    async def account(self, run_id, owner_id, *, connection=None):
        async with self._connection(connection) as conn:
            await self.world._run(conn, run_id)
            account = await self.world._account(conn, run_id, owner_id)
            grants = await rows(
                conn,
                "SELECT g.authorization_id,g.recipient_owner_id,g.amount,g.memo, "
                "EXISTS(SELECT 1 FROM credit_transfers t WHERE t.run_id=g.run_id "
                "AND t.sender_owner_id=g.owner_id AND t.authorization_id=g.authorization_id) "
                "AS used FROM transfer_grants g WHERE g.run_id=%s AND g.owner_id=%s",
                (run_id, owner_id),
            )
            transfers = await rows(
                conn,
                "SELECT request_id,sender_owner_id,recipient_owner_id,amount,memo,created_at "
                "FROM credit_transfers WHERE run_id=%s "
                "AND (sender_owner_id=%s OR recipient_owner_id=%s) ORDER BY created_at,request_id",
                (run_id, owner_id, owner_id),
            )
            result = {
                "owner_id": owner_id,
                "balance": account["balance"],
                "authorizations": grants,
                "transfers": transfers,
            }
            if await TransferTaskService.enabled(conn, run_id):
                result.update(
                    task_tracking_enabled=True,
                    tasks=await TransferTaskService(self.pool).list(
                        run_id, owner_id, connection=conn
                    ),
                )
            return result

    async def transfer(self, run_id, owner_id, request_id, arguments: TransferArguments):
        payload = {"kind": "credit_transfer", **arguments.model_dump(mode="json")}
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            account = await self.world._account(conn, run_id, owner_id)
            if prior := await self.world._prior(conn, run_id, owner_id, request_id, payload):
                return prior
            self.world._active(run)
            grant = await row(
                conn,
                "SELECT * FROM transfer_grants WHERE run_id=%s AND owner_id=%s "
                "AND authorization_id=%s",
                (run_id, owner_id, arguments.authorization_id),
            )
            if grant is None:
                raise WorldError("An exact owner transfer grant is required.", 403)
            if any(
                grant[key] != getattr(arguments, key)
                for key in ("recipient_owner_id", "amount", "memo")
            ):
                raise WorldError("The transfer does not match the owner grant.", 403)
            used = await row(
                conn,
                "SELECT request_id FROM credit_transfers WHERE run_id=%s "
                "AND sender_owner_id=%s AND authorization_id=%s",
                (run_id, owner_id, arguments.authorization_id),
            )
            if used:
                # New tool call IDs cannot spend the same instruction twice.
                prior = await row(
                    conn,
                    "SELECT result FROM world_requests WHERE run_id=%s "
                    "AND owner_id=%s AND request_id=%s",
                    (run_id, owner_id, used["request_id"]),
                )
                await self.world._request(
                    conn, run_id, owner_id, request_id, payload, prior["result"]
                )
                return prior["result"]
            await TransferTaskService(self.pool).before_payment(
                conn, run_id, owner_id, arguments.authorization_id
            )
            if account["balance"] < arguments.amount:
                raise WorldError("Insufficient simulated balance.")
            recipient = arguments.recipient_owner_id
            await self.world._account(conn, run_id, recipient)
            await conn.execute(
                "UPDATE world_accounts SET balance=balance-%s WHERE run_id=%s AND owner_id=%s",
                (arguments.amount, run_id, owner_id),
            )
            await conn.execute(
                "UPDATE world_accounts SET balance=balance+%s WHERE run_id=%s AND owner_id=%s",
                (arguments.amount, run_id, recipient),
            )
            receipt = {
                "status": "accepted",
                "request_id": str(request_id),
                "sender_owner_id": owner_id,
                "recipient_owner_id": recipient,
                "amount": arguments.amount,
                "memo": arguments.memo,
            }
            await self.world._request(conn, run_id, owner_id, request_id, payload, receipt)
            await conn.execute(
                "INSERT INTO credit_transfers "
                "(run_id,sender_owner_id,request_id,authorization_id,recipient_owner_id,"
                "amount,memo) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    owner_id,
                    request_id,
                    arguments.authorization_id,
                    recipient,
                    arguments.amount,
                    arguments.memo,
                ),
            )
            for participant in (owner_id, recipient):
                await self.world._journal(conn, run_id, "credit_transfer", participant)
                await emit(
                    conn,
                    run_id,
                    "transfer_completed",
                    run["current_window"],
                    receipt,
                    f"transfer:{owner_id}:{request_id}:{participant}",
                    participant,
                )
            subject, body = _notice(
                run, owner_id, recipient, arguments.amount, arguments.memo, request_id
            )
            await InboxService(self.pool).send(
                run_id,
                None,
                recipient,
                subject,
                body,
                uuid5(UUID(str(request_id)), "transfer-notification"),
                source="director",
                connection=conn,
            )
            await TransferTaskService(self.pool).paid(
                conn, run_id, owner_id, arguments.authorization_id, request_id
            )
            return receipt
