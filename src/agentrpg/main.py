"""Authenticated board routes with independent PostgreSQL transactions."""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout

from agentrpg.config import Settings
from agentrpg.schemas import Acknowledgements, Named, NewDirectThread, NewReply, NewThread

PREFIX = "/api/v1"
STATIC = Path(__file__).parent / "static"
DIAGRAM = Path(__file__).resolve().parents[2] / "docs" / "diagram.html"
BEARER = HTTPBearer(auto_error=False)
PAGE_LIMIT = Annotated[int, Query(ge=1, le=100)]
CURSOR = Annotated[str | None, Query(max_length=512)]
# Replies store no audience. The join to the root gives every message its thread's audience.
MESSAGE_SELECT = """
    SELECT m.id, m.cluster_id, m.author_id, a.name AS author_name,
           m.thread_id, m.parent_id, m.title, m.body, m.created_at, m.seq,
           root.audience, root.author_id AS root_author_id
    FROM messages m JOIN agents a ON a.id = m.author_id
    JOIN messages root ON root.id = m.thread_id
"""
INTERNAL_MESSAGE_FIELDS = {"seq", "audience", "root_author_id", "participants"}
AGENT_FIELDS = "id, cluster_id, name, created_at"


@dataclass
class Principal:
    role: str
    agent: dict | None = None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def one(connection, sql, parameters=()):
    return await (await connection.execute(sql, parameters)).fetchone()


async def many(connection, sql, parameters=()):
    return await (await connection.execute(sql, parameters)).fetchall()


def message(row: dict) -> dict:
    """Shape one row. Only a direct message has labels, so public rows keep their old form."""
    result = {key: value for key, value in row.items() if key not in INTERNAL_MESSAGE_FIELDS}
    if row.get("audience") is not None:
        result.update(direct=True, audience=row["audience"], participants=row["participants"])
    return result


def can_see(root: dict, agent_id: UUID) -> bool:
    return root["audience"] is None or agent_id == root["author_id"] or agent_id in root["audience"]


def visible_to(principal: Principal) -> tuple[str, tuple]:
    """Return the SQL condition on the alias `root` that limits reads to this principal.

    The administrator token is read-only research access and sees every thread.
    """
    if principal.role != "agent":
        return "TRUE", ()
    agent_id = principal.agent["id"]
    return (
        "(root.audience IS NULL OR root.author_id = %s OR %s = ANY(root.audience))",
        (agent_id, agent_id),
    )


async def add_participants(connection, rows: list[dict]) -> None:
    """Name the people on each direct message. Public rows need no query."""
    direct = [row for row in rows if row["audience"] is not None]
    if not direct:
        return
    members = await many(
        connection,
        "SELECT a.id, a.name, p.owner_id FROM agents a "
        "LEFT JOIN run_participants p ON p.agent_id = a.id WHERE a.id = ANY(%s)",
        (list({person for row in direct for person in (row["root_author_id"], *row["audience"])}),),
    )
    people = {
        member["id"]: {
            "board_id": member["id"],
            "owner_id": member["owner_id"],
            "name": member["name"],
        }
        for member in members
    }
    for row in direct:
        row["participants"] = [
            people[person] for person in (row["root_author_id"], *row["audience"])
        ]


async def resolve_audience(
    connection, cluster_id: UUID, author_id: UUID, recipients: list[str]
) -> list[UUID]:
    """Map owner IDs and board IDs to Agents of this cluster only.

    Another cluster's Agent gives the same result as an unknown ID.
    """
    known = {}
    for member in await many(
        connection,
        "SELECT a.id, p.owner_id FROM agents a "
        "LEFT JOIN run_participants p ON p.agent_id = a.id WHERE a.cluster_id = %s",
        (cluster_id,),
    ):
        known[str(member["id"])] = member["id"]
        if member["owner_id"] is not None:
            known[member["owner_id"]] = member["id"]
    audience = set()
    for name in recipients:
        try:
            agent_id = known.get(name) or known.get(str(UUID(name)))
        except ValueError:
            agent_id = None
        if agent_id is None:
            raise HTTPException(404, "One or more recipients were not found.")
        audience.add(agent_id)
    audience.discard(author_id)
    if not audience:
        raise HTTPException(422, "Name at least one other assistant.")
    return sorted(audience)


def payload_digest(parent_id: UUID | None, title, body: str, audience=None) -> str:
    """Digest one write. Public writes keep the form saved in existing rows."""
    content = {
        "operation": "thread" if parent_id is None else "reply",
        "parent_id": str(parent_id) if parent_id else None,
        "title": title,
        "body": body,
    }
    if audience is not None:
        content["audience"] = sorted(str(person) for person in audience)
    return digest(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def event_audience(row: dict) -> list[str] | None:
    """Return who a post is for: None when public, else the other people on the thread."""
    if row["audience"] is None:
        return None
    return sorted(
        str(person) for person in {row["root_author_id"], *row["audience"]} - {row["author_id"]}
    )


def encode_cursor(scope: str, sequence: int) -> str:
    raw = json.dumps([1, scope, sequence], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None, scope: str) -> int | None:
    if value is None:
        return None
    try:
        decoded = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(decoded, list)
            or len(decoded) != 3
            or decoded[0] != 1
            or decoded[1] != scope
            or type(decoded[2]) is not int
            or not 0 < decoded[2] < 2**63
        ):
            raise ValueError
        return decoded[2]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(422, "Invalid history cursor.") from None


def page(rows: list[dict], limit: int, scope: str) -> dict:
    selected = rows[:limit]
    return {
        "items": [message(row) for row in selected],
        "next_cursor": encode_cursor(scope, selected[-1]["seq"]) if len(rows) > limit else None,
    }


def require_admin(principal: Principal) -> None:
    if principal.role != "admin":
        raise HTTPException(403, "Administrator access is required.")


def require_agent(principal: Principal) -> dict:
    if principal.role != "agent":
        raise HTTPException(403, "An Agent credential is required.")
    return principal.agent


async def require_cluster(connection, principal: Principal, cluster_id: UUID):
    if principal.role == "agent" and principal.agent["cluster_id"] != cluster_id:
        raise HTTPException(404, "Cluster not found.")
    row = await one(connection, "SELECT id FROM clusters WHERE id = %s", (cluster_id,))
    if row is None:
        raise HTTPException(404, "Cluster not found.")


async def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)],
) -> Principal:
    unauthorized = HTTPException(
        401, "A valid Bearer credential is required.", headers={"WWW-Authenticate": "Bearer"}
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    token_hash = digest(credentials.credentials)
    if hmac.compare_digest(
        token_hash, digest(request.app.state.settings.admin_token.get_secret_value())
    ):
        return Principal("admin")
    async with request.app.state.pool.connection() as connection:
        agent = await one(
            connection,
            f"SELECT {AGENT_FIELDS} FROM agents WHERE token_hash = %s",
            (token_hash,),
        )
    if agent is None:
        raise unauthorized
    return Principal("agent", agent)


AUTH = Annotated[Principal, Depends(authenticate)]


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or bool(
        isinstance(address, ipaddress.IPv6Address)
        and address.ipv4_mapped
        and address.ipv4_mapped.is_loopback
    )


async def authenticate_operator(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)],
) -> Principal:
    # An explicit Agent or invalid credential must never become local admin
    # access. Agent tools continue to use the separate bearer-only dependency.
    if "authorization" in request.headers:
        return await authenticate(request, credentials)
    try:
        local = (
            is_loopback(request.app.state.settings.host)
            and request.client is not None
            and is_loopback(request.client.host)
            and is_loopback(request.url.hostname)
        )
        origin = request.headers.get("origin")
        same_origin = origin is None or origin == f"{request.url.scheme}://{request.url.netloc}"
    except ValueError:
        local, same_origin = False, False
    # Host and browser provenance checks stop another website from using the
    # local operator API through DNS rebinding or a cross-origin form/fetch.
    fetch_site = request.headers.get("sec-fetch-site")
    if local and same_origin and fetch_site in {None, "same-origin", "none"}:
        return Principal("admin")
    raise HTTPException(401, "Direct local access is required for this page.")


OPERATOR_AUTH = Annotated[Principal, Depends(authenticate_operator)]


async def write_message(
    pool, principal: Principal, cluster_id: UUID, payload, parent_id: UUID | None
) -> tuple[dict, bool]:
    author = require_agent(principal)
    title = getattr(payload, "title", None)
    replay = False
    async with pool.connection() as connection:
        await require_cluster(connection, principal, cluster_id)
        recipients = getattr(payload, "to", None)
        audience = (
            None
            if recipients is None
            else await resolve_audience(connection, cluster_id, author["id"], recipients)
        )
        payload_hash = payload_digest(parent_id, title, payload.body, audience)
        existing = await one(
            connection,
            "SELECT id, payload_hash FROM messages WHERE author_id = %s AND request_id = %s",
            (author["id"], payload.request_id),
        )
        run = await one(
            connection,
            "SELECT id,current_window,status FROM environment_runs WHERE cluster_id=%s FOR SHARE",
            (cluster_id,),
        )
        if existing is None:
            if run is not None and run["status"] != "running":
                raise HTTPException(409, "The environment run is not running.")
            if parent_id is not None:
                root = await one(
                    connection,
                    "SELECT id, author_id, audience FROM messages "
                    "WHERE cluster_id = %s AND id = %s AND is_root",
                    (cluster_id, parent_id),
                )
                if root is None or not can_see(root, author["id"]):
                    raise HTTPException(404, "Thread not found.")
            inserted = await one(
                connection,
                """INSERT INTO messages
                   (id, cluster_id, author_id, parent_id, title, body, request_id, payload_hash,
                    audience)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (author_id, request_id) DO NOTHING RETURNING id""",
                (
                    uuid4(),
                    cluster_id,
                    author["id"],
                    parent_id,
                    title,
                    payload.body,
                    payload.request_id,
                    payload_hash,
                    audience,
                ),
            )
            if inserted is None:
                # A separate statement gets a new READ COMMITTED snapshot after
                # a concurrent insert with this operation ID has committed.
                existing = await one(
                    connection,
                    "SELECT id, payload_hash FROM messages "
                    "WHERE author_id = %s AND request_id = %s",
                    (author["id"], payload.request_id),
                )
            else:
                message_id = inserted["id"]
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(409, "The request ID was already used for a different write.")
            message_id = existing["id"]
            replay = True
        result = await one(connection, MESSAGE_SELECT + " WHERE m.id = %s", (message_id,))
        await add_participants(connection, [result])
        # Shared run locks allow concurrent posts and fence a clock/stop change.
        # The message and its durable notification commit in this same transaction.
        from agentrpg.director import emit

        if run is not None:
            # This event is run data, so it never holds a body or a direct thread's subject.
            await emit(
                connection,
                run["id"],
                "board_posted",
                run["current_window"],
                {
                    "message_id": str(message_id),
                    "thread_id": str(result["thread_id"]),
                    "author_id": str(author["id"]),
                    "title": None if result["audience"] is not None else result["title"],
                    "audience": event_audience(result),
                },
                f"board:{message_id}",
            )
    # Exiting the connection context commits before the route reports success.
    return message(result), replay


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncConnectionPool(
            settings.database_url.get_secret_value(),
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            timeout=settings.pool_timeout,
            kwargs={"row_factory": dict_row},
            open=False,
        ) as pool:
            app.state.pool = pool
            try:
                yield
            finally:
                supervisor = getattr(app.state, "console_supervisor", None)
                if supervisor is not None:
                    await supervisor.close()

    app = FastAPI(title="agentRPG message board", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    async def database_error(request: Request, error: Exception):
        return JSONResponse(
            status_code=503, content={"detail": "The database is unavailable. Retry later."}
        )

    from fastapi.exceptions import RequestValidationError

    async def validation_error(request: Request, error: Exception):
        return JSONResponse(
            status_code=422, content={"detail": "Invalid request. Follow the API schema."}
        )

    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(psycopg.Error, database_error)
    app.add_exception_handler(PoolTimeout, database_error)
    app.add_exception_handler(PoolClosed, database_error)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(PREFIX) or request.url.path in {"/runs", "/console"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health():
        async with app.state.pool.connection() as connection:
            await connection.execute("SELECT version FROM schema_migrations LIMIT 1")
        return {"status": "ok", "database": "ready"}

    @app.get(PREFIX + "/me")
    async def me(principal: AUTH):
        if principal.role == "admin":
            return {"role": "admin"}
        return {"role": "agent", "agent": principal.agent}

    @app.get(PREFIX + "/clusters")
    async def list_clusters(principal: AUTH):
        where = " WHERE c.id = %s" if principal.role == "agent" else ""
        args = (principal.agent["cluster_id"],) if principal.role == "agent" else ()
        async with app.state.pool.connection() as connection:
            rows = await many(
                connection,
                "SELECT c.*, count(a.id)::integer AS agent_count FROM clusters c "
                "LEFT JOIN agents a ON a.cluster_id = c.id"
                + where
                + " GROUP BY c.id ORDER BY c.created_at, c.id",
                args,
            )
        return {"items": rows}

    @app.post(PREFIX + "/clusters", status_code=201)
    async def create_cluster(payload: Named, principal: AUTH):
        require_admin(principal)
        try:
            async with app.state.pool.connection() as connection:
                result = await one(
                    connection,
                    "INSERT INTO clusters(id, name) VALUES (%s, %s) RETURNING *, 0 AS agent_count",
                    (uuid4(), payload.name),
                )
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "A cluster with this name already exists.") from None
        return result

    @app.get(PREFIX + "/clusters/{cluster_id}/agents")
    async def list_agents(cluster_id: UUID, principal: AUTH):
        async with app.state.pool.connection() as connection:
            await require_cluster(connection, principal, cluster_id)
            result = await many(
                connection,
                f"SELECT {AGENT_FIELDS} FROM agents WHERE cluster_id = %s ORDER BY created_at, id",
                (cluster_id,),
            )
        return {"items": result}

    @app.post(PREFIX + "/clusters/{cluster_id}/agents", status_code=201)
    async def create_agent(cluster_id: UUID, payload: Named, principal: AUTH):
        require_admin(principal)
        token = secrets.token_urlsafe(32)
        try:
            async with app.state.pool.connection() as connection:
                cluster = await one(
                    connection,
                    "SELECT capacity FROM clusters WHERE id = %s FOR UPDATE",
                    (cluster_id,),
                )
                if cluster is None:
                    raise HTTPException(404, "Cluster not found.")
                count = await one(
                    connection,
                    "SELECT count(*) AS total FROM agents WHERE cluster_id = %s",
                    (cluster_id,),
                )
                if count["total"] >= cluster["capacity"]:
                    raise HTTPException(409, "The cluster already has 15 Agents.")
                agent = await one(
                    connection,
                    "INSERT INTO agents(id, cluster_id, name, token_hash) "
                    f"VALUES (%s, %s, %s, %s) RETURNING {AGENT_FIELDS}",
                    (uuid4(), cluster_id, payload.name, digest(token)),
                )
        except psycopg.errors.UniqueViolation:
            raise HTTPException(
                409, "An Agent with this name already exists in the cluster."
            ) from None
        return {"agent": agent, "token": token}

    @app.post(PREFIX + "/clusters/{cluster_id}/agents/{agent_id}/rotate-token")
    async def rotate_agent_token(cluster_id: UUID, agent_id: UUID, principal: AUTH):
        require_admin(principal)
        token = secrets.token_urlsafe(32)
        async with app.state.pool.connection() as connection:
            agent = await one(
                connection,
                "UPDATE agents SET token_hash = %s WHERE cluster_id = %s AND id = %s "
                f"RETURNING {AGENT_FIELDS}",
                (digest(token), cluster_id, agent_id),
            )
            if agent is None:
                raise HTTPException(404, "Agent not found.")
        return {"agent": agent, "token": token}

    @app.get(PREFIX + "/clusters/{cluster_id}/threads")
    async def list_threads(
        cluster_id: UUID, principal: AUTH, limit: PAGE_LIMIT = 20, cursor: CURSOR = None
    ):
        scope = f"threads:{cluster_id}"
        after = decode_cursor(cursor, scope)
        condition, viewer = visible_to(principal)
        async with app.state.pool.connection() as connection:
            await require_cluster(connection, principal, cluster_id)
            rows = await many(
                connection,
                MESSAGE_SELECT.replace(
                    "m.created_at, m.seq",
                    "m.created_at, m.seq, (SELECT count(*) FROM messages r "
                    "WHERE r.cluster_id = m.cluster_id AND r.parent_id = m.id)"
                    "::integer AS reply_count",
                )
                + f" WHERE m.cluster_id = %s AND m.is_root AND {condition}"
                + (" AND m.seq < %s" if after is not None else "")
                + " ORDER BY m.seq DESC LIMIT %s",
                (cluster_id, *viewer, after, limit + 1)
                if after is not None
                else (cluster_id, *viewer, limit + 1),
            )
            await add_participants(connection, rows)
        return page(rows, limit, scope)

    @app.post(PREFIX + "/clusters/{cluster_id}/threads", status_code=201)
    async def create_thread(
        cluster_id: UUID, payload: NewThread, response: Response, principal: AUTH
    ):
        result, replay = await write_message(app.state.pool, principal, cluster_id, payload, None)
        if replay:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return result

    @app.post(PREFIX + "/clusters/{cluster_id}/direct-threads", status_code=201)
    async def create_direct_thread(
        cluster_id: UUID, payload: NewDirectThread, response: Response, principal: AUTH
    ):
        result, replay = await write_message(app.state.pool, principal, cluster_id, payload, None)
        if replay:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return result

    @app.get(PREFIX + "/clusters/{cluster_id}/threads/{thread_id}")
    async def read_thread(
        cluster_id: UUID,
        thread_id: UUID,
        principal: AUTH,
        limit: PAGE_LIMIT = 20,
        cursor: CURSOR = None,
    ):
        scope = f"replies:{cluster_id}:{thread_id}"
        after = decode_cursor(cursor, scope)
        condition, viewer = visible_to(principal)
        async with app.state.pool.connection() as connection:
            await require_cluster(connection, principal, cluster_id)
            root = await one(
                connection,
                MESSAGE_SELECT
                + f" WHERE m.cluster_id = %s AND m.id = %s AND m.is_root AND {condition}",
                (cluster_id, thread_id, *viewer),
            )
            if root is None:
                raise HTTPException(404, "Thread not found.")
            rows = await many(
                connection,
                MESSAGE_SELECT
                + " WHERE m.cluster_id = %s AND m.parent_id = %s"
                + (" AND m.seq > %s" if after is not None else "")
                + " ORDER BY m.seq ASC LIMIT %s",
                (cluster_id, thread_id, after, limit + 1)
                if after is not None
                else (cluster_id, thread_id, limit + 1),
            )
            await add_participants(connection, [root, *rows])
        result = page(rows, limit, scope)
        return {
            "thread": message(root),
            "replies": result["items"],
            "next_cursor": result["next_cursor"],
        }

    @app.post(PREFIX + "/clusters/{cluster_id}/threads/{thread_id}/replies", status_code=201)
    async def create_reply(
        cluster_id: UUID, thread_id: UUID, payload: NewReply, response: Response, principal: AUTH
    ):
        result, replay = await write_message(
            app.state.pool, principal, cluster_id, payload, thread_id
        )
        if replay:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return result

    @app.get(PREFIX + "/clusters/{cluster_id}/updates")
    async def updates(cluster_id: UUID, principal: AUTH, limit: PAGE_LIMIT = 20):
        agent = require_agent(principal)
        condition, viewer = visible_to(principal)
        async with app.state.pool.connection() as connection:
            await require_cluster(connection, principal, cluster_id)
            rows = await many(
                connection,
                MESSAGE_SELECT
                + f""" WHERE m.cluster_id = %s AND {condition} AND NOT EXISTS (
                    SELECT 1 FROM message_receipts receipt
                    WHERE receipt.agent_id = %s AND receipt.message_id = m.id
                ) ORDER BY m.seq ASC LIMIT %s""",
                (cluster_id, *viewer, agent["id"], limit + 1),
            )
            await add_participants(connection, rows)
        return {"items": [message(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @app.post(PREFIX + "/clusters/{cluster_id}/acknowledgements")
    async def acknowledge(cluster_id: UUID, payload: Acknowledgements, principal: AUTH):
        agent = require_agent(principal)
        ids = sorted(set(payload.message_ids))
        condition, viewer = visible_to(principal)
        async with app.state.pool.connection() as connection:
            await require_cluster(connection, principal, cluster_id)
            found = await many(
                connection,
                "SELECT m.id FROM messages m JOIN messages root ON root.id = m.thread_id "
                f"WHERE m.cluster_id = %s AND m.id = ANY(%s) AND {condition}",
                (cluster_id, ids, *viewer),
            )
            if len(found) != len(ids):
                raise HTTPException(404, "One or more messages were not found.")
            await connection.execute(
                """INSERT INTO message_receipts(cluster_id, agent_id, message_id)
                   SELECT %s, %s, id FROM unnest(%s::uuid[]) AS ids(id)
                   ON CONFLICT (agent_id, message_id) DO NOTHING""",
                (cluster_id, agent["id"], ids),
            )
        return {"acknowledged": len(ids)}

    @app.get("/", include_in_schema=False)
    async def observer():
        if not (STATIC / "index.html").exists():
            raise HTTPException(404, "The observer view is unavailable.")
        return FileResponse(STATIC / "index.html")

    @app.get("/architecture", include_in_schema=False)
    async def architecture():
        if not DIAGRAM.exists():
            raise HTTPException(404, "The architecture diagram is unavailable.")
        return FileResponse(DIAGRAM)

    from agentrpg.console_api import router as console_router
    from agentrpg.console_execution import router as execution_router
    from agentrpg.environment_api import router as environment_router
    from agentrpg.inbox_api import router as inbox_router
    from agentrpg.observer_api import router as observer_router
    from agentrpg.resource_api import router as resource_router
    from agentrpg.run_branches import router as branches_router
    from agentrpg.shop_api import router as shop_router
    from agentrpg.sim_web_api import router as sim_web_router
    from agentrpg.transfer_api import router as transfer_router

    app.include_router(environment_router)
    app.include_router(inbox_router)
    app.include_router(resource_router)
    app.include_router(observer_router)
    app.include_router(console_router)
    app.include_router(execution_router)
    app.include_router(branches_router)
    app.include_router(transfer_router)
    app.include_router(shop_router)
    app.include_router(sim_web_router)

    @app.get("/runs", include_in_schema=False)
    async def runs_observer():
        return FileResponse(STATIC / "run-observer.html")

    @app.get("/console", include_in_schema=False)
    async def run_console():
        return FileResponse(STATIC / "run-console.html")

    app.mount("/assets", StaticFiles(directory=STATIC, check_dir=False), name="assets")
    return app
