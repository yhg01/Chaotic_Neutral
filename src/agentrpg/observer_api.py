"""Read-only administrator views of persisted runs and campaign costs."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg import sql

from agentrpg.budget import BudgetError, CampaignLedger
from agentrpg.director import row, rows
from agentrpg.english_display import english_view
from agentrpg.main import OPERATOR_AUTH, require_admin

router = APIRouter(prefix="/api/v1/observer", tags=["run observer"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=1000000)]
Owner = Annotated[str | None, Query(max_length=80)]


async def get_run(conn, run_id):
    run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s", (run_id,))
    if run is None:
        raise HTTPException(404, "Run not found.")
    return run


def page(records, limit, offset):
    return {
        "items": records[:limit],
        "offset": offset,
        "next_offset": offset + limit if len(records) > limit else None,
    }


@router.get("/runs")
async def list_runs(
    request: Request, principal: OPERATOR_AUTH, limit: Limit = 50, offset: Offset = 0
):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        records = await rows(
            conn,
            """SELECT r.id,r.status,r.phase,r.current_window,r.max_windows,
            r.created_at,r.scenario->>'title' AS title,r.scenario->>'name' AS name,
            (SELECT count(*) FROM run_participants p WHERE p.run_id=r.id) AS participants
            FROM environment_runs r ORDER BY r.created_at DESC,r.id DESC LIMIT %s OFFSET %s""",
            (limit + 1, offset),
        )
    return await english_view(request, page(records, limit, offset))


@router.get("/runs/{run_id}")
async def overview(run_id: UUID, request: Request, principal: OPERATOR_AUTH, compact: bool = False):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        run = await get_run(conn, run_id)
        participants = await rows(
            conn,
            """SELECT p.owner_id,p.agent_id,a.name AS agent_name
            FROM run_participants p JOIN agents a ON a.id=p.agent_id
            WHERE p.run_id=%s ORDER BY p.owner_id""",
            (run_id,),
        )
        jobs = await rows(
            conn,
            """SELECT owner_id,kind,status,attempts,error,"window",lease_until
            FROM run_jobs WHERE run_id=%s AND "window"=%s ORDER BY owner_id,kind""",
            (run_id, run["current_window"]),
        )
        counts = await row(
            conn,
            """SELECT
            (SELECT count(*) FROM run_events WHERE run_id=%s) AS events,
            (SELECT count(*) FROM owner_messages WHERE run_id=%s) AS owner_messages,
            (SELECT count(*) FROM messages WHERE cluster_id=%s) AS board_messages,
            (SELECT count(*) FROM inbox_messages WHERE run_id=%s) AS mail_messages,
            (SELECT count(*) FROM owner_resources WHERE run_id=%s) AS resources,
            (SELECT count(*) FROM market_ledger WHERE run_id=%s) AS trades""",
            (run_id, run_id, run["cluster_id"], run_id, run_id, run_id),
        )
        kinds = await rows(
            conn, "SELECT DISTINCT kind FROM run_events WHERE run_id=%s ORDER BY kind", (run_id,)
        )
        campaign_ids = await rows(
            conn,
            """SELECT DISTINCT campaign_id FROM model_spend_attempts
            WHERE run_id=%s ORDER BY campaign_id""",
            (run_id,),
        )
        report = (
            None
            if compact
            else await row(conn, "SELECT report FROM run_reports WHERE run_id=%s", (run_id,))
        )
    costs = [
        await CampaignLedger(request.app.state.pool).summary(item["campaign_id"], recent_limit=0)
        for item in campaign_ids
    ]
    return await english_view(
        request,
        {
            "run": run,
            "participants": participants,
            "jobs": jobs,
            "counts": counts,
            "event_kinds": [item["kind"] for item in kinds],
            "campaigns": costs,
            "report": report["report"] if report else None,
        },
    )


@router.get("/runs/{run_id}/events")
async def events(
    run_id: UUID,
    request: Request,
    principal: OPERATOR_AUTH,
    owner_id: Owner = None,
    kind: Annotated[str | None, Query(max_length=100)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        await get_run(conn, run_id)
        records = await rows(
            conn,
            """SELECT id,kind,"window",owner_id,payload,created_at
            FROM run_events WHERE run_id=%s AND (%s::text IS NULL OR owner_id=%s)
            AND (%s::text IS NULL OR kind=%s)
            ORDER BY "window" DESC,created_at DESC,id DESC LIMIT %s OFFSET %s""",
            (run_id, owner_id, owner_id, kind, kind, limit + 1, offset),
        )
    return page(records, limit, offset)


COLLECTIONS = {
    "chat": ("owner_messages", "owner_id", '"window" DESC,created_at DESC,id DESC'),
    "mail": ("inbox_messages", "recipient_owner_id", "sequence DESC"),
    "resources": ("owner_resources", "owner_id", "owner_id,resource_id"),
    "market": ("market_listings", "seller_id", "seller_id,listing_id"),
    "receipts": ("market_requests", "owner_id", "owner_id,request_id"),
    "trades": ("market_ledger", "owner_id", "window_index DESC,owner_id,request_id"),
    "accounts": ("world_accounts", "owner_id", "owner_id"),
    "reservations": ("world_requests", "owner_id", "owner_id,request_id"),
    "jobs": ("run_jobs", "owner_id", '"window" DESC,created_at DESC,id DESC'),
}
Collection = Literal[
    "chat",
    "mail",
    "resources",
    "market",
    "receipts",
    "trades",
    "accounts",
    "reservations",
    "jobs",
    "board",
]


@router.get("/runs/{run_id}/records/{collection}")
async def records(
    run_id: UUID,
    collection: Collection,
    request: Request,
    principal: OPERATOR_AUTH,
    owner_id: Owner = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        run = await get_run(conn, run_id)
        if collection == "board":
            result = await rows(
                conn,
                """SELECT m.id,m.author_id,a.name AS author_name,
                p.owner_id,m.parent_id,m.thread_id,m.title,m.body,m.created_at FROM messages m
                JOIN agents a ON a.id=m.author_id JOIN run_participants p ON p.agent_id=m.author_id
                AND p.run_id=%s WHERE m.cluster_id=%s AND (%s::text IS NULL OR p.owner_id=%s)
                ORDER BY m.seq DESC LIMIT %s OFFSET %s""",
                (run_id, run["cluster_id"], owner_id, owner_id, limit + 1, offset),
            )
        elif collection == "mail":
            result = await rows(
                conn,
                """SELECT m.id,m.sender_owner_id,m.sender_source,m.recipient_owner_id,
                m.subject,m.body,m.in_reply_to,m."window",m.created_at,r.read_at,
                (r.read_at IS NOT NULL) AS acknowledged
                FROM inbox_messages m LEFT JOIN inbox_receipts r ON r.run_id=m.run_id
                AND r.message_id=m.id AND r.owner_id=m.recipient_owner_id
                WHERE m.run_id=%s AND (%s::text IS NULL OR m.recipient_owner_id=%s
                OR m.sender_owner_id=%s) ORDER BY m.sequence DESC LIMIT %s OFFSET %s""",
                (run_id, owner_id, owner_id, owner_id, limit + 1, offset),
            )
        else:
            table, field, order = COLLECTIONS[collection]
            condition = sql.SQL("(%s::text IS NULL OR {}=%s)").format(sql.Identifier(field))
            args = [run_id, owner_id, owner_id]
            statement = sql.SQL(
                "SELECT * FROM {} WHERE run_id=%s AND {} ORDER BY {} LIMIT %s OFFSET %s"
            ).format(sql.Identifier(table), condition, sql.SQL(order))
            result = await rows(conn, statement, (*args, limit + 1, offset))
            # Operational hashes and leases are not useful in the observer.
            hidden = {"payload_hash", "sender_key", "lease_token", "input_hash"}
            result = [
                {key: value for key, value in item.items() if key not in hidden} for item in result
            ]
    return page(result, limit, offset)


@router.get("/campaigns")
async def campaigns(
    request: Request, principal: OPERATOR_AUTH, limit: Limit = 50, offset: Offset = 0
):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        values = await rows(
            conn,
            "SELECT id FROM model_campaigns ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
    result = [
        await CampaignLedger(request.app.state.pool).summary(item["id"], recent_limit=0)
        for item in values[:limit]
    ]
    return {**page(values, limit, offset), "items": result}


@router.get("/campaigns/{campaign_id}")
async def campaign(campaign_id: UUID, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    try:
        return await CampaignLedger(request.app.state.pool).summary(campaign_id)
    except BudgetError as error:
        raise HTTPException(404, str(error)) from None
