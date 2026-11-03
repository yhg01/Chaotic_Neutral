"""Agent-scoped storefront routes; local observers receive a read-only view."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from agentrpg.director import Director, DirectorError
from agentrpg.environment_api import identity
from agentrpg.main import AUTH, OPERATOR_AUTH, STATIC, require_admin
from agentrpg.resource_api import safe
from agentrpg.shop import ShopService

router = APIRouter(tags=["browser shop"])


class CartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["select_task", "set_quantity"]
    task_id: UUID | None = None
    listing_id: UUID | None = None
    quantity: int = Field(default=0, strict=True, ge=0, le=100)


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cart_version: int = Field(strict=True, ge=0)


OperationID = Annotated[UUID, Header(alias="X-Shop-Operation-ID")]
ActionHash = Annotated[str | None, Header(alias="X-Shop-Action-Hash", pattern=r"^[0-9a-f]{64}$")]


@router.get("/shop", include_in_schema=False)
async def storefront():
    return FileResponse(
        STATIC / "shop.html",
        headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'",
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/v1/runs/{run_id}/shop/state")
async def shop_state(run_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await safe(ShopService(request.app.state.pool).state(run_id, member["owner_id"]))


@router.post("/api/v1/runs/{run_id}/shop/cart")
async def shop_cart(
    run_id: UUID,
    payload: CartBody,
    request: Request,
    principal: AUTH,
    operation_id: OperationID,
    action_hash: ActionHash = None,
):
    member = await identity(request, run_id, principal)
    return await safe(
        ShopService(request.app.state.pool).change_cart(
            run_id,
            member["owner_id"],
            operation_id,
            **payload.model_dump(),
            action_hash=action_hash,
        )
    )


@router.post("/api/v1/runs/{run_id}/shop/checkout")
async def shop_checkout(
    run_id: UUID,
    payload: CheckoutBody,
    request: Request,
    principal: AUTH,
    operation_id: OperationID,
    action_hash: ActionHash = None,
):
    member = await identity(request, run_id, principal)
    return await safe(
        ShopService(request.app.state.pool).checkout(
            run_id, member["owner_id"], operation_id, payload.cart_version, action_hash=action_hash
        )
    )


@router.get("/api/v1/runs/{run_id}/shop/operations/{operation_id}")
async def shop_operation(run_id: UUID, operation_id: UUID, request: Request, principal: AUTH):
    member = await identity(request, run_id, principal)
    return await safe(
        ShopService(request.app.state.pool).operation(run_id, member["owner_id"], operation_id)
    )


@router.get("/api/v1/observer/runs/{run_id}/shop")
async def observe_shop(
    run_id: UUID,
    request: Request,
    principal: OPERATOR_AUTH,
    owner: Annotated[str | None, Query(max_length=80)] = None,
):
    require_admin(principal)
    director = Director(request.app.state.pool)
    try:
        members = await director.participants(run_id)
    except DirectorError:
        from fastapi import HTTPException

        raise HTTPException(404, "Run not found.") from None
    selected = owner or (members[0]["owner_id"] if members else None)
    result = await safe(ShopService(request.app.state.pool).state(run_id, selected))
    return {**result, "read_only": True, "owners": [m["owner_id"] for m in members]}
