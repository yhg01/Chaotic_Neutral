"""Authenticated confirmation tools, scoped by native participant identity."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from agentrpg.confirmation_schema import ConfirmationToolBody
from agentrpg.confirmations import ConfirmationService
from agentrpg.director import DirectorError
from agentrpg.environment_api import identity
from agentrpg.main import AUTH

router = APIRouter(prefix="/api/v1/runs/{run_id}/confirmations", tags=["confirmations"])


@router.get("")
async def list_confirmations(run_id: UUID, request: Request, principal: AUTH):
    participant = await identity(request, run_id, principal)
    return await ConfirmationService(request.app.state.pool).execute(
        run_id, participant["owner_id"], "list_confirmations"
    )


@router.post("/{operation}")
async def confirmation_operation(
    run_id: UUID, operation: str, payload: ConfirmationToolBody, request: Request, principal: AUTH
):
    names = {
        "request": "request_confirmation",
        "ask-owner": "ask_confirmation_owner",
        "reply": "reply_confirmation",
        "report": "report_confirmation",
    }
    if operation not in names:
        raise HTTPException(404, "Confirmation operation not found.")
    participant = await identity(request, run_id, principal)
    try:
        return await ConfirmationService(request.app.state.pool).execute(
            run_id, participant["owner_id"], names[operation], payload.task_id, payload.request_id
        )
    except DirectorError as error:
        raise HTTPException(409, {"code": "confirmation_protocol", "detail": str(error)}) from None
