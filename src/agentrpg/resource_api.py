"""Agent-scoped workspace and simulated commerce HTTP routes."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentrpg.environment_api import identity
from agentrpg.main import AUTH
from agentrpg.market import MarketService
from agentrpg.resource_schema import TRADE_VERSION_DESCRIPTION, Money, Quantity, ResourceID
from agentrpg.resources import ResourceService
from agentrpg.world import WorldError

router = APIRouter(prefix="/api/v1", tags=["resources and market"])


class ResourceWriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    content: str = Field(max_length=12000)
    expected_version: int = Field(strict=True, ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class ResourceCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=12000)


class MarketActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    kind: Literal["sell", "buy"]
    resource_id: ResourceID | None = None
    listing_id: UUID | None = None
    quantity: Quantity = 1
    unit_price: Money | None = None
    expected_version: int | None = Field(
        default=None, strict=True, ge=0, description=TRADE_VERSION_DESCRIPTION
    )

    @model_validator(mode="after")
    def check_action(self):
        if self.kind == "sell" and (
            self.resource_id is None or self.unit_price is None or self.listing_id is not None
        ):
            raise ValueError("Sell requires a resource ID and unit price.")
        if self.kind == "buy" and (
            self.listing_id is None or self.resource_id is not None or self.unit_price is not None
        ):
            raise ValueError("Buy requires a listing ID and its published price.")
        return self


async def safe(awaitable):
    try:
        return await awaitable
    except WorldError as error:
        raise HTTPException(error.status_code, str(error)) from None


@router.get("/runs/{run_id}/resources")
async def resource_view(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await safe(ResourceService(request.app.state.pool).view(run_id, member["owner_id"]))


@router.get("/runs/{run_id}/search")
async def search_environment(
    run_id: UUID,
    request: Request,
    principal: AUTH,
    query: Annotated[str, Query(max_length=200)] = "",
):
    member = await identity(request, run_id, principal)
    return await safe(
        ResourceService(request.app.state.pool).search(run_id, member["owner_id"], query)
    )


@router.get("/runs/{run_id}/resources/{resource_id}")
async def fetch_resource(run_id: UUID, resource_id: str, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await safe(
        ResourceService(request.app.state.pool).fetch(run_id, member["owner_id"], resource_id)
    )


@router.put("/runs/{run_id}/resources/{resource_id}")
async def write_resource(
    run_id: UUID, resource_id: str, payload: ResourceWriteBody, request: Request, principal: AUTH
):
    member = await identity(request, run_id, principal)
    return await safe(
        ResourceService(request.app.state.pool).update(
            run_id, member["owner_id"], resource_id, **payload.model_dump()
        )
    )


@router.get("/runs/{run_id}/market")
async def market_view(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await safe(MarketService(request.app.state.pool).view(run_id, member["owner_id"]))


@router.post("/runs/{run_id}/market/actions")
async def market_action(run_id: UUID, payload: MarketActionBody, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await safe(
        MarketService(request.app.state.pool).submit_current(
            run_id, member["owner_id"], **payload.model_dump()
        )
    )


@router.get("/runs/{run_id}/market/actions/{request_id}")
async def market_receipt(run_id: UUID, request_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await safe(
        MarketService(request.app.state.pool).receipt(run_id, member["owner_id"], request_id)
    )


@router.post("/runs/{run_id}/resources", status_code=201)
async def create_resource(
    run_id: UUID, payload: ResourceCreateBody, request: Request, principal: AUTH
):
    member = await identity(request, run_id, principal)
    return await safe(
        ResourceService(request.app.state.pool).create_document(
            run_id, member["owner_id"], **payload.model_dump()
        )
    )
