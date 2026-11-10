"""Timed simulated credits: pending admission, atomic effect and real ledger receipt.

No provider, real bank or Agent authority-grant endpoint is installed here.
"""

from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import model_validator

from agentrpg.director import DirectorError, emit, row
from agentrpg.director_clock import DirectorClock, TaskOutput
from agentrpg.simulation_clock import ClockConflict, ClockContract, Schedule, Work, digest
from agentrpg.transfer_schema import TransferConsent
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.world import WorldError


class TimedActorOutput(TaskOutput):
    transfer_consent: TransferConsent | None = None

    @model_validator(mode="after")
    def owner_instruction(self):
        if self.transfer_consent is not None and not self.text.strip():
            raise ValueError("Exact owner consent requires an accompanying owner message.")
        return self


async def enabled(conn, run_id):
    return await row(conn, "SELECT * FROM clock_transfer_profiles WHERE run_id=%s", (run_id,))


async def decode_output(conn, run_id, job, output):
    data = output.model_dump(mode="json") if isinstance(output, TaskOutput) else output
    if await row(conn, "SELECT run_id FROM clock_runtime_profiles WHERE run_id=%s", (run_id,)):
        from agentrpg.clock_runtime import AgentPhaseOutput, RuntimeActorOutput

        return (AgentPhaseOutput if job["role"] == "agent" else RuntimeActorOutput).model_validate(
            data
        )
    schema = (
        TimedActorOutput if job["role"] == "actor" and await enabled(conn, run_id) else TaskOutput
    )
    return schema.model_validate(data)


async def grant_context(conn, run_id, owner_id, message_id, arguments, job_id):
    job = await row(
        conn,
        """SELECT j.*,c.now_seconds,c.status AS clock_status,
        e.status AS event_status,e.at_seconds FROM director_clock_jobs j
        JOIN simulation_clocks c ON c.id=j.clock_id AND c.manager_run_id=j.run_id
        JOIN simulation_clock_events e ON e.clock_id=j.clock_id AND e.event_key=j.event_key
        JOIN clock_transfer_profiles p ON p.run_id=j.run_id
        WHERE j.id=%s AND j.run_id=%s AND j.owner_id=%s""",
        (job_id, run_id, owner_id),
    )
    if (
        not job
        or job["role"] != "actor"
        or job["status"] != "timed"
        or job["clock_status"] != "active"
        or job["event_status"] != "delivered"
        or job["now_seconds"] != job["at_seconds"]
    ):
        raise WorldError("No current timed Actor-consent completion.", 403)
    consent = (job["output"] or {}).get("transfer_consent")
    if (
        not consent
        or str(arguments.authorization_id) != str(uuid5(job["id"], "clock-transfer-consent"))
        or any(
            consent[k] != getattr(arguments, k) for k in ["recipient_owner_id", "amount", "memo"]
        )
    ):
        raise WorldError("Consent does not match the saved Actor decision.", 403)
    message = await row(conn, "SELECT * FROM owner_messages WHERE id=%s", (message_id,))
    if (
        not message
        or message["run_id"] != UUID(str(run_id))
        or message["request_id"] != job["id"]
        or message["source"] != "owner"
        or message["owner_id"] != owner_id
        or message["content"] != job["output"]["text"].strip()
    ):
        raise WorldError("Timed consent requires this Actor completion message.", 403)


async def payment_context(conn, run_id, owner_id, request_id, arguments, operation_id):
    op = await row(
        conn,
        """SELECT o.*,c.now_seconds,c.status AS clock_status,e.status AS event_status
        FROM clock_transfer_operations o JOIN simulation_clocks c
          ON c.id=o.clock_id AND c.manager_run_id=o.run_id
        JOIN simulation_clock_events e ON e.clock_id=o.clock_id AND e.event_key=o.event_key
        JOIN clock_transfer_profiles p ON p.run_id=o.run_id
        WHERE o.id=%s AND o.run_id=%s AND o.owner_id=%s""",
        (operation_id, run_id, owner_id),
    )
    if (
        not op
        or op["id"] != UUID(str(request_id))
        or op["status"] != "pending"
        or op["clock_status"] != "active"
        or op["event_status"] != "delivered"
        or op["now_seconds"] != op["due_seconds"]
        or op["arguments"] != arguments.model_dump(mode="json")
    ):
        raise WorldError("No current timed transfer completion.", 403)
    return op


async def bank_context(conn, run_id, recipient, subject, body, request_id, operation_id):
    op = await row(
        conn,
        """SELECT o.*,c.now_seconds,c.status AS clock_status,e.status AS event_status,
        t.amount,t.memo,t.recipient_owner_id FROM clock_transfer_operations o
        JOIN simulation_clocks c ON c.id=o.clock_id AND c.manager_run_id=o.run_id
        JOIN simulation_clock_events e ON e.clock_id=o.clock_id AND e.event_key=o.event_key
        JOIN credit_transfers t ON t.run_id=o.run_id
          AND t.sender_owner_id=o.owner_id AND t.request_id=o.id
        WHERE o.id=%s AND o.run_id=%s""",
        (operation_id, run_id),
    )
    if (
        not op
        or op["status"] != "pending"
        or op["clock_status"] != "active"
        or op["event_status"] != "delivered"
        or op["now_seconds"] != op["due_seconds"]
    ):
        raise WorldError("Bank notice requires a committed-in-transaction timed payment.", 403)
    expected = (
        f"{op['owner_id']} transferred {op['amount']} simulated credits. "
        f"Memo: {op['memo']}. Receipt: {op['id']}."
    )
    if (
        recipient != op["recipient_owner_id"]
        or subject != "Simulated credit transfer received"
        or body != expected
        or UUID(str(request_id)) != uuid5(op["id"], "transfer-notification")
    ):
        raise WorldError("Bank notice must match the actual timed payment.", 403)


async def authorize_job(conn, pool, run, job, output, message):
    consent = getattr(output, "transfer_consent", None)
    if consent is None:
        return
    arguments = TransferArguments(
        authorization_id=uuid5(job["id"], "clock-transfer-consent"),
        recipient_owner_id=consent.recipient_owner_id,
        amount=consent.amount,
        memo=consent.memo,
    )
    await TransferService(pool).authorize(
        run["id"],
        job["owner_id"],
        message["id"],
        arguments,
        connection=conn,
        task_key=consent.task_key,
        clock_job_id=job["id"],
    )
    await emit(
        conn,
        run["id"],
        "clock_transfer_authorized",
        0,
        {"job_id": str(job["id"]), "authorization_id": str(arguments.authorization_id)},
        f"clock_transfer_grant:{job['id']}",
        job["owner_id"],
    )


class TimedTransfers:
    def __init__(self, pool):
        self.pool = pool
        self.director = DirectorClock(pool)

    async def enable(self, run_id, *, max_operations=1000, max_calls=10000):
        if any(type(v) is not int or not 1 <= v <= 10000 for v in [max_operations, max_calls]):
            raise DirectorError("Timed transfer limits must be integers from 1 to 10000.")
        async with self.director._locked(run_id) as (conn, binding, clock, run):
            prior = await enabled(conn, run_id)
            if prior:
                if (prior["max_operations"], prior["max_calls"]) != (max_operations, max_calls):
                    raise ClockConflict("Transfer profile is already frozen differently.")
                return
            jobs = await row(
                conn, "SELECT count(*) AS n FROM director_clock_jobs WHERE run_id=%s", (run_id,)
            )
            spec = ClockContract.model_validate(clock["contract"])
            if (
                clock["status"] != "active"
                or clock["now_seconds"] != 0
                or jobs["n"]
                or "transfer_credits" not in spec.tool_seconds
            ):
                raise DirectorError(
                    "Enable before the first job with a frozen transfer_credits duration."
                )
            await conn.execute(
                "INSERT INTO clock_transfer_profiles VALUES (%s,%s,%s,%s)",
                (run_id, "clock-transfer-v1", max_operations, max_calls),
            )
            await emit(
                conn,
                run_id,
                "clock_transfers_enabled",
                0,
                {"version": "clock-transfer-v1"},
                "clock_transfers_enabled",
            )

    async def _lane(self, conn, clock_id, process_id):
        lane = await row(
            conn,
            "SELECT * FROM simulation_clock_processes WHERE clock_id=%s AND process_id=%s",
            (clock_id, process_id),
        )
        if lane is None or lane["role"] != "agent":
            raise WorldError("Only an own Agent lane may call the transfer tool.", 403)
        return lane

    async def schedule(self, run_id, process_id, request_id, arguments):
        request_id = UUID(str(request_id))
        arguments = TransferArguments.model_validate(
            arguments.model_dump(mode="json")
            if isinstance(arguments, TransferArguments)
            else arguments
        )
        data = arguments.model_dump(mode="json")
        identity = digest({"process_id": process_id, "arguments": data})
        async with self.director._locked(run_id) as (conn, binding, clock, run):
            profile = await enabled(conn, run_id)
            if not profile:
                raise DirectorError("Timed transfers are not enabled for this run.")
            lane = await self._lane(conn, clock["id"], process_id)
            prior = await row(
                conn,
                """SELECT c.input_hash,o.admission FROM clock_transfer_calls c
                JOIN clock_transfer_operations o ON o.id=c.operation_id
                WHERE c.run_id=%s AND c.owner_id=%s AND c.request_id=%s""",
                (run_id, lane["owner_id"], request_id),
            )
            if prior:
                if prior["input_hash"] != identity:
                    raise ClockConflict("Transfer call ID already has a different payload.")
                return prior["admission"]
            calls = await row(
                conn, "SELECT count(*) AS n FROM clock_transfer_calls WHERE run_id=%s", (run_id,)
            )
            if calls["n"] >= profile["max_calls"]:
                raise DirectorError("The frozen transfer-call capacity is exhausted.")
            grant = await row(
                conn,
                "SELECT * FROM transfer_grants WHERE run_id=%s AND owner_id=%s AND "
                "authorization_id=%s",
                (run_id, lane["owner_id"], arguments.authorization_id),
            )
            if not grant or any(
                grant[k] != getattr(arguments, k) for k in ["recipient_owner_id", "amount", "memo"]
            ):
                raise WorldError("An exact own Actor transfer grant is required.", 403)
            old = await row(
                conn,
                "SELECT * FROM clock_transfer_operations WHERE run_id=%s AND owner_id=%s AND "
                "authorization_id=%s",
                (run_id, lane["owner_id"], arguments.authorization_id),
            )
            if old:
                op_id = old["id"]
                admission = old["admission"]
            else:
                if await row(
                    conn, "SELECT run_id FROM clock_runtime_profiles WHERE run_id=%s", (run_id,)
                ):
                    from agentrpg.clock_runtime import tool_context

                    await tool_context(
                        conn, run_id, lane["owner_id"], "transfer_credits", data, request_id
                    )
                active = await row(
                    conn,
                    "SELECT id FROM director_clock_jobs WHERE clock_id=%s AND process_id=%s "
                    "AND status<>'applied'",
                    (clock["id"], process_id),
                )
                count = await row(
                    conn,
                    "SELECT count(*) AS n FROM clock_transfer_operations WHERE run_id=%s",
                    (run_id,),
                )
                if active or lane["state"] != "ready" or clock["status"] != "active":
                    raise DirectorError("A ready Agent lane without an unresolved job is required.")
                if count["n"] >= profile["max_operations"]:
                    raise DirectorError("The frozen transfer-operation capacity is exhausted.")
                op_id = uuid5(run["id"], f"clock-transfer:{lane['owner_id']}:{request_id}")
                timed = await self.director.clock.execute(
                    clock["id"],
                    op_id,
                    Work(process_id=process_id, kind="tool", tool="transfer_credits"),
                    connection=conn,
                    manager_run_id=run["id"],
                )
                admission = {
                    "status": "pending",
                    "id": str(op_id),
                    "operation_id": str(op_id),
                    "due_seconds": timed["ready_at"],
                    "sender_owner_id": lane["owner_id"],
                    "recipient_owner_id": arguments.recipient_owner_id,
                    "amount": arguments.amount,
                }
                await conn.execute(
                    """INSERT INTO clock_transfer_operations
                    (id,run_id,clock_id,process_id,owner_id,authorization_id,arguments,event_key,due_seconds,admission)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        op_id,
                        run_id,
                        clock["id"],
                        process_id,
                        lane["owner_id"],
                        arguments.authorization_id,
                        Jsonb(data),
                        timed["event_key"],
                        timed["ready_at"],
                        Jsonb(admission),
                    ),
                )
                await emit(
                    conn,
                    run_id,
                    "clock_transfer_scheduled",
                    0,
                    admission,
                    f"clock_transfer_schedule:{op_id}",
                    lane["owner_id"],
                )
            await conn.execute(
                "INSERT INTO clock_transfer_calls VALUES (%s,%s,%s,%s,%s)",
                (run_id, lane["owner_id"], request_id, op_id, identity),
            )
            return admission

    async def operation(self, run_id, process_id, operation_id):
        async with self.director._locked(run_id) as (conn, binding, clock, run):
            lane = await self._lane(conn, clock["id"], process_id)
            op = await row(
                conn,
                "SELECT * FROM clock_transfer_operations WHERE id=%s AND run_id=%s AND owner_id=%s",
                (operation_id, run_id, lane["owner_id"]),
            )
            if not op:
                raise WorldError("Transfer operation not found in this account.", 404)
            return {
                "id": str(op["id"]),
                "operation_id": str(op["id"]),
                "status": op["status"],
                "due_seconds": op["due_seconds"],
                "applied_at_seconds": op["applied_at_seconds"],
                "result": op["result"],
            }


async def finish_transfer(conn, pool, director, run, clock, event, now):
    op = await row(
        conn,
        "SELECT * FROM clock_transfer_operations WHERE clock_id=%s AND event_key=%s",
        (clock["id"], event["event_key"]),
    )
    if not op:
        raise DirectorError("Tool completion has no durable timed transfer.")
    if op["status"] != "pending":
        return {"operation_id": str(op["id"]), "result": op["result"]}
    try:
        # A business rejection rolls back any nested business writes before saving its result.
        async with conn.transaction():
            receipt = await TransferService(pool).transfer(
                run["id"],
                op["owner_id"],
                op["id"],
                TransferArguments.model_validate(op["arguments"]),
                connection=conn,
                clock_operation_id=op["id"],
            )
        status = "applied"
    except WorldError as exc:
        receipt = {
            "status": "rejected",
            "operation_id": str(op["id"]),
            "reason": str(exc),
            "simulated_seconds": now,
        }
        status = "rejected"
    await conn.execute(
        "UPDATE clock_transfer_operations SET "
        "status=%s,result=%s,applied_at_seconds=%s,receipt_request_id=%s WHERE id=%s",
        (status, Jsonb(receipt), now, op["id"] if status == "applied" else None, op["id"]),
    )
    recipients = [op["owner_id"]]
    if status == "applied":
        recipients.append(op["arguments"]["recipient_owner_id"])
    for owner in recipients:
        lane = await row(
            conn,
            "SELECT process_id FROM simulation_clock_processes WHERE clock_id=%s AND "
            "owner_id=%s AND role='agent'",
            (clock["id"], owner),
        )
        await director.clock.execute(
            clock["id"],
            uuid5(op["id"], "receipt:" + owner),
            Schedule(
                event_key=f"receipt-{uuid5(op['id'], 'receipt:' + owner).hex}",
                at_seconds=now,
                priority=1,
                kind="transfer_completed" if status == "applied" else "transfer_rejected",
                recipients=(lane["process_id"],),
                payload=receipt,
            ),
            connection=conn,
            manager_run_id=run["id"],
        )
    if status == "applied":
        mail = await row(
            conn,
            "SELECT id FROM inbox_messages WHERE run_id=%s AND sender_key='director:' AND "
            "request_id=%s",
            (run["id"], uuid5(op["id"], "transfer-notification")),
        )
        lane = await row(
            conn,
            "SELECT process_id FROM simulation_clock_processes WHERE clock_id=%s AND "
            "owner_id=%s AND role='agent'",
            (clock["id"], op["arguments"]["recipient_owner_id"]),
        )
        await director.clock.execute(
            clock["id"],
            uuid5(op["id"], "bank-mail-clock-notice"),
            Schedule(
                event_key=f"bank-{op['id'].hex}",
                at_seconds=now,
                priority=1,
                kind="inbox_received",
                recipients=(lane["process_id"],),
                payload={"message_id": str(mail["id"])},
            ),
            connection=conn,
            manager_run_id=run["id"],
        )
    await emit(
        conn,
        run["id"],
        "clock_transfer_" + status,
        0,
        {"operation_id": str(op["id"]), "simulated_seconds": now, "result": receipt},
        f"clock_transfer_result:{op['id']}",
        op["owner_id"],
    )
    return {"operation_id": str(op["id"]), "result": receipt}
