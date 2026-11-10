"""Persistent simulated transfer work, independent of activation completion.

Only the trusted owner/Actor path can open or cancel a task. Assistant updates
can defer work or reference committed evidence; they cannot grant payment rights.
"""

import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from agentrpg.director import DirectorError, emit, fingerprint, row, rows
from agentrpg.transfer_task_schema import TransferTaskRequest
from agentrpg.world import WorldError, WorldService


class TransferTaskError(WorldError):
    def __init__(self, message, code):
        super().__init__(message, 403)
        self.code = code


class TransferTaskService:
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

    async def enable(self, run_id, *, connection=None, clock_runtime=False):
        """Operator setup before execution. Existing runs cannot change profile."""
        async with self._connection(connection) as conn:
            run = await self.world._run(conn, run_id)
            if await self.enabled(conn, run_id):
                return
            if clock_runtime:
                if connection is None or run["status"] != "paused":
                    raise WorldError("Elapsed task setup requires its Director transaction.", 403)
                clock = await row(
                    conn,
                    "SELECT id FROM simulation_clocks WHERE manager_run_id=%s AND "
                    "now_seconds=0 AND status='active'",
                    (run_id,),
                )
                if not clock or await row(
                    conn, "SELECT id FROM director_clock_jobs WHERE run_id=%s LIMIT 1", (run_id,)
                ):
                    raise WorldError("Enable elapsed task tracking before the first job.")
            elif run["status"] != "pending":
                raise WorldError("Enable task tracking before starting a new run.")
            await conn.execute("INSERT INTO transfer_task_tracking VALUES (%s)", (run_id,))

    @staticmethod
    async def enabled(conn, run_id):
        return bool(
            await row(conn, "SELECT 1 FROM transfer_task_tracking WHERE run_id=%s", (run_id,))
        )

    async def _task(self, conn, run_id, owner_id, task_id):
        task = await row(
            conn,
            "SELECT * FROM transfer_tasks WHERE run_id=%s AND owner_id=%s AND id=%s",
            (run_id, owner_id, task_id),
        )
        if not task:
            raise WorldError("Transfer task not found.", 404)
        return task

    async def list(self, run_id, owner_id, *, connection=None):
        async with self._connection(connection) as conn:
            await self.world._account(conn, run_id, owner_id)
            return await rows(
                conn,
                "SELECT * FROM transfer_tasks WHERE run_id=%s AND owner_id=%s "
                "ORDER BY (status IN ('active','blocked')) DESC,created_at DESC,id LIMIT 25",
                (run_id, owner_id),
            )

    async def _prior(self, conn, run_id, owner_id, request_id, payload):
        prior = await row(
            conn,
            "SELECT * FROM transfer_task_operations WHERE run_id=%s AND owner_id=%s "
            "AND request_id=%s",
            (run_id, owner_id, request_id),
        )
        if prior and prior["payload_hash"] != fingerprint(payload):
            raise WorldError("This task operation ID has different arguments.")
        return prior["result"] if prior else None

    async def _save(self, conn, run, task_id, owner_id, request_id, payload):
        task = await self._task(conn, run["id"], owner_id, task_id)
        public = json.loads(json.dumps(task, default=str))
        await conn.execute(
            "INSERT INTO transfer_task_operations VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                run["id"],
                owner_id,
                request_id,
                task_id,
                Jsonb(payload),
                fingerprint(payload),
                Jsonb(public),
            ),
        )
        await emit(
            conn,
            run["id"],
            "transfer_task_updated",
            run["current_window"],
            {"task": public, "operation": payload["kind"]},
            f"transfer-task:{owner_id}:{request_id}",
            owner_id,
        )
        return public

    async def owner_request(
        self,
        run_id,
        owner_id,
        owner_message_id,
        request_id,
        request: TransferTaskRequest,
        *,
        connection=None,
        clock_job_id=None,
    ):
        """Trusted typed owner instruction; never an assistant HTTP operation."""
        payload = {"owner_message_id": str(owner_message_id), **request.model_dump(mode="json")}
        async with self._connection(connection) as conn:
            run = await self.world._run(conn, run_id)
            if prior := await self._prior(conn, run_id, owner_id, request_id, payload):
                return prior
            if clock_job_id is not None:
                if connection is None or run["status"] != "paused":
                    raise WorldError("Elapsed owner tasks require their Director transaction.", 403)
                from agentrpg.clock_runtime import actor_task_context

                await actor_task_context(
                    conn, run_id, owner_id, owner_message_id, request_id, request, clock_job_id
                )
            else:
                self.world._active(run)
            if not await self.enabled(conn, run_id):
                raise WorldError("Transfer task tracking is not enabled.")
            message = await row(
                conn,
                "SELECT id FROM owner_messages WHERE run_id=%s AND owner_id=%s "
                "AND id=%s AND source='owner'",
                (run_id, owner_id, owner_message_id),
            )
            if not message:
                raise WorldError("A typed instruction from this owner is required.", 403)
            task = await row(
                conn,
                "SELECT * FROM transfer_tasks WHERE run_id=%s AND owner_id=%s AND task_key=%s",
                (run_id, owner_id, request.task_key),
            )
            if request.kind == "open":
                if task:
                    raise WorldError("This owner task already exists; do not restart it.")
                await self.world._account(conn, run_id, request.recipient_owner_id)
                if owner_id == request.recipient_owner_id:
                    raise WorldError("The recipient must be another owner.", 422)
                count = await row(
                    conn,
                    "SELECT count(*) AS n FROM transfer_tasks WHERE run_id=%s "
                    "AND owner_id=%s AND status IN ('active','blocked')",
                    (run_id, owner_id),
                )
                if count["n"] >= 20:
                    raise WorldError("This owner already has twenty open transfer tasks.")
                task_id = uuid5(UUID(str(run_id)), f"transfer-task:{owner_id}:{request.task_key}")
                await conn.execute(
                    "INSERT INTO transfer_tasks (id,run_id,owner_id,task_key,recipient_owner_id,"
                    "description,owner_message_id,status,blocked_on) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'blocked',%s)",
                    (
                        task_id,
                        run_id,
                        owner_id,
                        request.task_key,
                        request.recipient_owner_id,
                        request.description,
                        owner_message_id,
                        [request.blocked_on],
                    ),
                )
            else:
                if not task:
                    raise WorldError("Transfer task not found.", 404)
                if task["payment_request_id"] or task["status"] in {"completed", "cancelled"}:
                    raise WorldError("A terminal or already-paid task cannot be cancelled.")
                task_id = task["id"]
                await conn.execute(
                    "UPDATE transfer_tasks SET status='cancelled',blocked_on='{}',"
                    "completion_evidence=%s "
                    "WHERE id=%s",
                    (Jsonb({"owner_cancellation_message_id": str(owner_message_id)}), task_id),
                )
            return await self._save(conn, run, task_id, owner_id, request_id, payload)

    async def bind_authorization(self, conn, run_id, owner_id, task_key, authorization_id):
        if not await self.enabled(conn, run_id):
            if task_key is not None:
                raise WorldError("Task tracking is not enabled.")
            return
        task = await row(
            conn,
            "SELECT * FROM transfer_tasks WHERE run_id=%s AND owner_id=%s AND task_key=%s",
            (run_id, owner_id, task_key),
        )
        grant = await row(
            conn,
            "SELECT * FROM transfer_grants WHERE run_id=%s AND owner_id=%s AND authorization_id=%s",
            (run_id, owner_id, authorization_id),
        )
        if not task:
            raise WorldError("A live owner task key is required for this authorization.")
        if task["recipient_owner_id"] != grant["recipient_owner_id"] or (
            task["authorization_id"] and task["authorization_id"] != authorization_id
        ):
            raise WorldError("The authorization does not match this task.", 403)
        if task["authorization_id"] == authorization_id:
            return
        if task["status"] in {"completed", "cancelled"}:
            raise WorldError("A live owner task key is required for this authorization.")
        await conn.execute(
            "UPDATE transfer_tasks SET authorization_id=%s,status='active',blocked_on='{}' "
            "WHERE id=%s",
            (authorization_id, task["id"]),
        )

    @staticmethod
    async def received_mail(conn, message):
        """New peer mail is a reason to review; its text grants no payment rights."""
        await conn.execute(
            "UPDATE transfer_tasks SET status='active',blocked_on='{}',review_mail_id=%s "
            "WHERE run_id=%s AND owner_id=%s AND recipient_owner_id=%s AND status='blocked' "
            "AND 'inbox_received'=ANY(blocked_on) AND payment_request_id IS NULL",
            (
                message["id"],
                message["run_id"],
                message["recipient_owner_id"],
                message["sender_owner_id"],
            ),
        )

    async def before_payment(self, conn, run_id, owner_id, authorization_id):
        if not await self.enabled(conn, run_id):
            return
        task = await row(
            conn,
            "SELECT * FROM transfer_tasks WHERE run_id=%s AND owner_id=%s AND authorization_id=%s",
            (run_id, owner_id, authorization_id),
        )
        if not task or task["status"] in {"completed", "cancelled"}:
            raise WorldError("The payment has no live authorized owner task.", 403)

    async def paid(self, conn, run_id, owner_id, authorization_id, receipt_id):
        await conn.execute(
            "UPDATE transfer_tasks SET payment_request_id=%s,status='active',blocked_on='{}' "
            "WHERE run_id=%s AND owner_id=%s AND authorization_id=%s",
            (receipt_id, run_id, owner_id, authorization_id),
        )

    async def update(self, run_id, owner_id, request_id, kind, arguments):
        payload = {"kind": kind, **arguments.model_dump(mode="json")}
        async with self._connection() as conn:
            from agentrpg.clock_runtime import lock_profile, tool_context

            elapsed = await lock_profile(conn, run_id)
            run = await self.world._run(conn, run_id)
            if prior := await self._prior(conn, run_id, owner_id, request_id, payload):
                return prior
            if elapsed:
                try:
                    await tool_context(
                        conn,
                        run_id,
                        owner_id,
                        "defer_transfer_task" if kind == "defer" else "complete_transfer_task",
                        arguments.model_dump(mode="json"),
                        request_id,
                    )
                except DirectorError as error:
                    raise WorldError(str(error), 403) from None
            else:
                self.world._active(run)
            task = await self._task(conn, run_id, owner_id, arguments.task_id)
            if task["status"] in {"completed", "cancelled"}:
                raise WorldError("This task is already terminal.")
            if kind == "defer":
                await conn.execute(
                    "UPDATE transfer_tasks SET status='blocked',blocked_on=%s WHERE id=%s",
                    ([arguments.wait_for], task["id"]),
                )
            elif kind == "complete":
                receipt = await row(
                    conn,
                    "SELECT * FROM credit_transfers WHERE run_id=%s AND sender_owner_id=%s "
                    "AND request_id=%s AND authorization_id=%s",
                    (run_id, owner_id, arguments.receipt_id, task["authorization_id"]),
                )
                if not receipt or task["payment_request_id"] != arguments.receipt_id:
                    raise TransferTaskError(
                        "Completion requires this task's committed payment receipt.",
                        "invalid_task_receipt",
                    )
                notice = await row(
                    conn,
                    "SELECT id FROM inbox_messages WHERE run_id=%s AND id=%s "
                    "AND sender_owner_id=%s AND sender_source='agent' AND recipient_owner_id=%s "
                    "AND created_at>=%s",
                    (
                        run_id,
                        arguments.recipient_notice_id,
                        owner_id,
                        task["recipient_owner_id"],
                        receipt["created_at"],
                    ),
                )
                report = await row(
                    conn,
                    "SELECT id FROM owner_messages WHERE run_id=%s AND id=%s "
                    "AND owner_id=%s AND source='agent' AND created_at>=%s",
                    (run_id, arguments.owner_report_id, owner_id, receipt["created_at"]),
                )
                if not notice or not report:
                    raise TransferTaskError(
                        "Completion requires committed recipient notice and owner report.",
                        "missing_task_notice_or_report",
                    )
                evidence = {
                    key: str(getattr(arguments, key))
                    for key in ("receipt_id", "recipient_notice_id", "owner_report_id")
                }
                await conn.execute(
                    "UPDATE transfer_tasks SET status='completed',blocked_on='{}',"
                    "completion_evidence=%s WHERE id=%s",
                    (Jsonb(evidence), task["id"]),
                )
            else:
                raise WorldError("Unknown task update.", 422)
            return await self._save(conn, run, task["id"], owner_id, request_id, payload)


async def effective_transfer_task_states(conn, run_id, states):
    """Scheduler state only. Keep raw model results and completion receipts intact."""
    tasks = await rows(
        conn,
        "SELECT owner_id,status,blocked_on FROM transfer_tasks WHERE run_id=%s "
        "AND status IN ('active','blocked')",
        (run_id,),
    )
    for owner in {task["owner_id"] for task in tasks}:
        own = [task for task in tasks if task["owner_id"] == owner]
        previous = states.get(owner, {})
        waits = sorted(
            set(previous.get("wait_for", []))
            | {event for task in own for event in task["blocked_on"]}
        )
        active = any(task["status"] == "active" for task in own)
        states[owner] = {
            **previous,
            "status": "responded" if active else "waiting",
            "wait_for": [] if active else waits,
        }
    return states
