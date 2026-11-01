"""Agent-scoped internal inbox endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from agentrpg.environment_api import identity
from agentrpg.inbox import InboxError, InboxService
from agentrpg.mail_schema import MailReceiptBody, MailSendBody
from agentrpg.main import AUTH

router = APIRouter(prefix="/api/v1/runs/{run_id}/inbox", tags=["inbox"])


def service(request):
    return InboxService(request.app.state.pool)


@router.get("")
async def list_mail(
    run_id: UUID,
    request: Request,
    principal: AUTH,
    mailbox: Literal["inbox", "sent", "all"] = "inbox",
    unread_only: bool = False,
    query: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
):
    participant = await identity(request, run_id, principal)
    try:
        return await service(request).list_messages(
            run_id,
            participant["owner_id"],
            mailbox=mailbox,
            unread_only=unread_only,
            query=query,
            limit=limit,
            cursor=cursor,
        )
    except InboxError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.post("", status_code=201)
async def send_mail(run_id: UUID, payload: MailSendBody, request: Request, principal: AUTH):
    participant = await identity(request, run_id, principal)
    try:
        return await service(request).send(
            run_id,
            participant["owner_id"],
            payload.recipient_owner_id,
            payload.subject,
            payload.body,
            payload.request_id,
            in_reply_to=payload.in_reply_to,
        )
    except InboxError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.post("/receipts")
async def acknowledge_mail(
    run_id: UUID,
    payload: MailReceiptBody,
    request: Request,
    principal: AUTH,
):
    participant = await identity(request, run_id, principal)
    try:
        return await service(request).acknowledge(
            run_id, participant["owner_id"], payload.message_ids
        )
    except InboxError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.get("/{message_id}")
async def read_mail(run_id: UUID, message_id: UUID, request: Request, principal: AUTH):
    participant = await identity(request, run_id, principal)
    try:
        return await service(request).read(run_id, participant["owner_id"], message_id)
    except InboxError as error:
        raise HTTPException(error.status_code, str(error)) from None
