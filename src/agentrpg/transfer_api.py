"""Authenticated owner-scoped transfer endpoints. Authority has no Agent route."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from agentrpg.agent_tools import Identifier
from agentrpg.director import DirectorError, row
from agentrpg.environment_api import identity
from agentrpg.main import AUTH
from agentrpg.simulation_clock import ClockConflict
from agentrpg.transfer_task_schema import CompleteTransferTask, DeferTransferTask
from agentrpg.transfer_tasks import TransferTaskError, TransferTaskService
from agentrpg.transfers import TransferArguments, TransferService
from agentrpg.world import WorldError

router = APIRouter(prefix="/api/v1/runs/{run_id}", tags=["simulated transfers"])


class TransferBody(TransferArguments):
    request_id: Identifier


class DeferTaskBody(DeferTransferTask):
    request_id: Identifier


class CompleteTaskBody(CompleteTransferTask):
    request_id: Identifier


@router.get("/transfer-tasks")
async def transfer_tasks(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return {
        "items": await TransferTaskService(request.app.state.pool).list(run_id, member["owner_id"])
    }


@router.post("/transfer-tasks/defer")
async def defer_transfer_task(
    run_id: UUID, payload: DeferTaskBody, request: Request, principal: AUTH
):
    return await update_task(run_id, payload, request, principal, "defer", DeferTransferTask)


@router.post("/transfer-tasks/complete")
async def complete_transfer_task(
    run_id: UUID, payload: CompleteTaskBody, request: Request, principal: AUTH
):
    return await update_task(run_id, payload, request, principal, "complete", CompleteTransferTask)


async def update_task(run_id, payload, request, principal, kind, schema):
    member = await identity(request, run_id, principal)
    arguments = schema.model_validate(payload.model_dump(exclude={"request_id"}))
    try:
        return await TransferTaskService(request.app.state.pool).update(
            run_id, member["owner_id"], payload.request_id, kind, arguments
        )
    except TransferTaskError as error:
        raise HTTPException(error.status_code, {"code": error.code}) from None
    except WorldError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.get("/transfer-account")
async def transfer_account(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    try:
        return await TransferService(request.app.state.pool).account(run_id, member["owner_id"])
    except WorldError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.post("/transfers")
async def transfer(run_id: UUID, payload: TransferBody, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    arguments = TransferArguments.model_validate(payload.model_dump(exclude={"request_id"}))
    try:
        from agentrpg.clock_transfers import TimedTransfers, enabled

        async with request.app.state.pool.connection() as conn:
            profile = await enabled(conn, run_id)
            lane = (
                await row(
                    conn,
                    """SELECT p.process_id FROM simulation_clock_processes p
                JOIN director_clock_bindings b ON b.clock_id=p.clock_id
                WHERE b.run_id=%s AND p.owner_id=%s AND p.role='agent'""",
                    (run_id, member["owner_id"]),
                )
                if profile
                else None
            )
        if profile:
            if lane is None:
                raise DirectorError("No Agent lane is bound to this account.")
            admission = await TimedTransfers(request.app.state.pool).schedule(
                run_id, lane["process_id"], payload.request_id, arguments
            )
            actual = await TimedTransfers(request.app.state.pool).operation(
                run_id, lane["process_id"], admission["operation_id"]
            )
            # Completed operations must not cause another pending wait or payment.
            return actual["result"] if actual["status"] != "pending" else admission
        return await TransferService(request.app.state.pool).transfer(
            run_id, member["owner_id"], payload.request_id, arguments
        )
    except WorldError as error:
        raise HTTPException(error.status_code, str(error)) from None
    except (ClockConflict, DirectorError) as error:
        raise HTTPException(409, str(error)) from None


@router.get("/transfer-operations/{operation_id}")
async def transfer_operation(run_id: UUID, operation_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    from agentrpg.clock_transfers import TimedTransfers

    async with request.app.state.pool.connection() as conn:
        lane = await row(
            conn,
            """SELECT p.process_id FROM simulation_clock_processes p
            JOIN director_clock_bindings b ON b.clock_id=p.clock_id
            WHERE b.run_id=%s AND p.owner_id=%s AND p.role='agent'""",
            (run_id, member["owner_id"]),
        )
    if lane is None:
        raise HTTPException(404, "Timed transfer account not found.")
    try:
        return await TimedTransfers(request.app.state.pool).operation(
            run_id, lane["process_id"], operation_id
        )
    except WorldError as error:
        raise HTTPException(error.status_code, str(error)) from None
