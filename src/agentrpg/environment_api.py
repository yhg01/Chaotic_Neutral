"""Private participant routes and local administrator run controls."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentrpg.director import Director, DirectorError, row
from agentrpg.main import AUTH, require_admin, require_agent
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.world import WorldError, WorldService

router = APIRouter(prefix="/api/v1", tags=["environment"])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OwnerMessageBody(Body):
    request_id: UUID
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)
    ]


class ActionBody(Body):
    request_id: UUID
    kind: Literal["reserve", "cancel"]
    asset_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    expected_version: int | None = Field(default=None, strict=True, ge=0)


def director(request):
    return Director(request.app.state.pool)


def world(request):
    return WorldService(request.app.state.pool)


async def identity(request, run_id, principal):
    agent = require_agent(principal)
    try:
        return await director(request).participant(run_id, agent["id"])
    except DirectorError:
        raise HTTPException(404, "Run not found.") from None


@router.post("/scenarios/validate")
async def validate_scenario(payload: ScenarioSpec, principal: AUTH):
    require_admin(principal)
    return {
        "valid": True,
        "scenario_hash": payload.content_hash(),
        "participants": len(payload.owners),
    }


@router.post("/runs", status_code=201)
async def create_run(payload: ScenarioSpec, request: Request, principal: AUTH):
    require_admin(principal)
    return await director(request).create_run(payload)


@router.get("/runs/{run_id}")
async def get_run(run_id: UUID, request: Request, principal: AUTH):
    require_admin(principal)
    try:
        return await director(request).get_run(run_id)
    except DirectorError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/runs/{run_id}/control/{action}")
async def control_run(
    run_id: UUID, action: Literal["start", "pause", "resume"], request: Request, principal: AUTH
):
    require_admin(principal)
    try:
        return await director(request).control(run_id, action)
    except DirectorError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/runs/{run_id}/view")
async def run_view(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    run = await director(request).get_run(run_id)
    owner = next(o for o in run["scenario"]["owners"] if o["id"] == member["owner_id"])
    return {
        **member,
        "cluster_id": run["cluster_id"],
        "status": run["status"],
        "current_window": run["current_window"],
        "owner_name": owner["name"],
        "participants": [
            {"owner_id": item["id"], "name": item["name"]} for item in run["scenario"]["owners"]
        ],
    }


@router.get("/runs/{run_id}/owner/messages")
async def owner_messages(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return {"items": await director(request).messages(run_id, member["owner_id"])}


@router.post("/runs/{run_id}/owner/messages")
async def send_owner_message(
    run_id: UUID, payload: OwnerMessageBody, request: Request, principal: AUTH
):
    member = await identity(request, run_id, principal)
    try:
        return await director(request).send_message(
            run_id, member["owner_id"], payload.content, payload.request_id
        )
    except DirectorError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/runs/{run_id}/world")
async def inspect_world(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await world(request).view(run_id, member["owner_id"])


@router.post("/runs/{run_id}/actions")
async def submit_action(run_id: UUID, payload: ActionBody, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    try:
        return await world(request).submit_current(
            run_id,
            member["owner_id"],
            payload.request_id,
            payload.kind,
            payload.asset_id,
            payload.expected_version,
        )
    except WorldError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.get("/runs/{run_id}/actions/{action_id}")
async def read_action(run_id: UUID, action_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    try:
        return await world(request).receipt(run_id, member["owner_id"], action_id)
    except WorldError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.get("/runs/{run_id}/trace")
async def read_trace(run_id: UUID, request: Request, principal: AUTH):
    require_admin(principal)
    return {"items": await director(request).trace(run_id)}


@router.get("/runs/{run_id}/report")
async def read_report(run_id: UUID, request: Request, principal: AUTH):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        result = await row(conn, "SELECT report FROM run_reports WHERE run_id=%s", (run_id,))
    if result is None:
        raise HTTPException(404, "Run report not found. Complete evaluation with the run command.")
    return result["report"]
