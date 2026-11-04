"""Read-only, turn-scoped replay of saved simulation evidence."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request

from agentrpg.director import row, rows
from agentrpg.english_display import english_view
from agentrpg.main import OPERATOR_AUTH, require_admin

router = APIRouter(prefix="/api/v1/observer", tags=["run console"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=1000000)]
Window = Annotated[int, Path(ge=0, le=99)]
AUDIT_EVENTS = ["control_audit_enabled", "operator_control", "trial_started", "trial_finished"]

ACTOR_FIELDS = {
    "owner_id",
    "owner_name",
    "request",
    "goals",
    "acceptance_rules",
    "known_facts",
    "beliefs",
    "disclosures",
    "max_spend",
    "assets",
    "cue",
    "source_event_id",
    "window",
    "conversation",
    "observations",
    "participants",
    "inbox",
    "resources",
    "market",
    "transfers",
    "assistant_wait_for",
}
AGENT_FIELDS = {
    "run_id",
    "participant_id",
    "activation_id",
    "window_id",
    "source_event_ids",
    "owner_messages",
    "world_observations",
}
HIDDEN_FIELDS = {
    "token",
    "api_key",
    "authorization",
    "credentials",
    "password",
    "secret",
    "lease_until",
    "sender_key",
    "operation_key",
}


def safe_value(value):
    """Remove operational secrets from structured saved data, including nested payloads."""
    if isinstance(value, dict):
        return {
            key: safe_value(item)
            for key, item in value.items()
            if key.lower() not in HIDDEN_FIELDS
            and not key.lower().endswith(("_hash", "_token", "_api_key"))
        }
    if isinstance(value, list):
        return [safe_value(item) for item in value]
    return value


async def run_state(conn, run_id):
    run = await row(
        conn,
        "SELECT id,cluster_id,status,phase,current_window,max_windows "
        "FROM environment_runs WHERE id=%s",
        (run_id,),
    )
    if run is None:
        raise HTTPException(404, "Run not found.")
    return run


async def summaries(conn, run):
    # The saved event establishes a board post's turn. Text equality avoids a
    # UUID cast error from an old or orphan event payload.
    records = await rows(
        conn,
        '''WITH windows AS (
            SELECT "window" FROM run_events WHERE run_id=%s AND kind<>ALL(%s)
            UNION SELECT "window" FROM owner_messages WHERE run_id=%s
            UNION SELECT "window" FROM inbox_messages WHERE run_id=%s
            UNION SELECT "window" FROM run_jobs WHERE run_id=%s
            UNION SELECT %s::integer
        ) SELECT w."window",
          (SELECT count(*) FROM owner_messages m WHERE m.run_id=%s
            AND m."window"=w."window" AND m.source='owner') AS owner_chat,
          (SELECT count(*) FROM owner_messages m WHERE m.run_id=%s
            AND m."window"=w."window" AND m.source='agent') AS agent_chat,
          (SELECT count(*) FROM run_events e WHERE e.run_id=%s
            AND e."window"=w."window" AND e.kind<>ALL(%s)) AS events,
          (SELECT count(*) FROM inbox_messages m WHERE m.run_id=%s
            AND m."window"=w."window") AS mail,
          (SELECT count(*) FROM messages m WHERE m.cluster_id=%s AND EXISTS
            (SELECT 1 FROM run_events e WHERE e.run_id=%s
             AND e."window"=w."window" AND e.kind='board_posted'
             AND e.payload->>'message_id'=m.id::text)) AS board,
          (SELECT count(*) FROM run_jobs j WHERE j.run_id=%s
            AND j."window"=w."window") AS jobs,
          (SELECT count(*) FROM run_jobs j WHERE j.run_id=%s
            AND j."window"=w."window" AND j.kind='actor') AS actor_jobs,
          (SELECT count(*) FROM run_jobs j WHERE j.run_id=%s
            AND j."window"=w."window" AND j.kind='agent') AS agent_jobs,
          (SELECT count(*) FROM run_jobs j WHERE j.run_id=%s
            AND j."window"=w."window" AND j.status='completed') AS completed_jobs,
          (SELECT count(*) FROM run_jobs j WHERE j.run_id=%s
            AND j."window"=w."window" AND j.status='failed') AS failed_jobs,
          EXISTS (SELECT 1 FROM run_events e WHERE e.run_id=%s
            AND e."window"=w."window" AND e.kind='window_completed') AS settled
        FROM windows w WHERE w."window">=0 AND w."window"<=%s
          AND w."window"<%s ORDER BY w."window"''',
        (
            run["id"],
            AUDIT_EVENTS,
            run["id"],
            run["id"],
            run["id"],
            run["current_window"],
            run["id"],
            run["id"],
            run["id"],
            AUDIT_EVENTS,
            run["id"],
            run["cluster_id"],
            run["id"],
            run["id"],
            run["id"],
            run["id"],
            run["id"],
            run["id"],
            run["id"],
            run["current_window"],
            run["max_windows"],
        ),
    )
    result = []
    for item in records:
        is_current = item["window"] == run["current_window"]
        result.append(
            {
                "window": item["window"],
                "phase": "completed"
                if item["settled"]
                else run["phase"]
                if is_current
                else "recorded",
                "status": "completed"
                if item["settled"]
                else run["status"]
                if is_current
                else "recorded",
                "counts": {
                    key: item[key]
                    for key in ("owner_chat", "agent_chat", "events", "mail", "board")
                },
                "jobs": {
                    "total": item["jobs"],
                    "actor": item["actor_jobs"],
                    "agent": item["agent_jobs"],
                    "completed": item["completed_jobs"],
                    "failed": item["failed_jobs"],
                },
                "has_actor_view": bool(item["actor_jobs"]),
            }
        )
    return result


@router.get("/runs/{run_id}/turns")
async def turns(run_id: UUID, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run = await run_state(conn, run_id)
        items = await summaries(conn, run)
    return {
        "run_id": run_id,
        "current_window": run["current_window"],
        "max_windows": run["max_windows"],
        "items": items,
    }


@router.get("/runs/{run_id}/turns/{window}")
async def turn(
    run_id: UUID,
    window: Window,
    request: Request,
    principal: OPERATOR_AUTH,
    limit: Limit = 100,
    offset: Offset = 0,
):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run = await run_state(conn, run_id)
        if window >= run["max_windows"]:
            raise HTTPException(422, "Turn is outside this run's configured window range.")
        summary = next(
            (item for item in await summaries(conn, run) if item["window"] == window), None
        )
        if summary is None:
            raise HTTPException(404, "Turn has not been recorded.")
        chat = await rows(
            conn,
            """SELECT id,owner_id,source,content,"window",created_at FROM owner_messages
               WHERE run_id=%s AND "window"=%s ORDER BY created_at,id LIMIT %s OFFSET %s""",
            (run_id, window, limit, offset),
        )
        mail = await rows(
            conn,
            """SELECT id,sender_owner_id,sender_source,recipient_owner_id,subject,body,
               in_reply_to,"window",created_at FROM inbox_messages
               WHERE run_id=%s AND "window"=%s ORDER BY created_at,id LIMIT %s OFFSET %s""",
            (run_id, window, limit, offset),
        )
        board = await rows(
            conn,
            """SELECT m.id,m.author_id,a.name AS author_name,p.owner_id,m.parent_id,
               m.thread_id,m.title,m.body,m.created_at,%s::integer AS "window"
               FROM messages m JOIN agents a ON a.id=m.author_id
               LEFT JOIN run_participants p ON p.agent_id=m.author_id AND p.run_id=%s
               WHERE m.cluster_id=%s AND EXISTS (SELECT 1 FROM run_events e
                 WHERE e.run_id=%s AND e."window"=%s AND e.kind='board_posted'
                 AND e.payload->>'message_id'=m.id::text)
               ORDER BY m.created_at,m.id LIMIT %s OFFSET %s""",
            (window, run_id, run["cluster_id"], run_id, window, limit, offset),
        )
        events = await rows(
            conn,
            """SELECT id,kind,"window",owner_id,payload,created_at FROM run_events
               WHERE run_id=%s AND "window"=%s AND kind<>ALL(%s)
               ORDER BY created_at,id LIMIT %s OFFSET %s""",
            (run_id, window, AUDIT_EVENTS, limit, offset),
        )
        jobs = []
        if offset == 0:
            jobs = await rows(
                conn,
                """SELECT id,owner_id,"window",kind,input,output,output_meta,status,
                   attempts,error,failure_kind,created_at FROM run_jobs
                   WHERE run_id=%s AND "window"=%s
                   ORDER BY created_at,id LIMIT 30""",
                (run_id, window),
            )
    perspectives = []
    for job in jobs:
        fields = ACTOR_FIELDS if job["kind"] == "actor" else AGENT_FIELDS
        saved_input = job["input"] if isinstance(job["input"], dict) else {}
        job["input"] = {key: value for key, value in saved_input.items() if key in fields}
        if job["kind"] == "actor" and job["input"]:
            perspectives.append(
                {
                    "owner_id": job["owner_id"],
                    "job_id": job["id"],
                    "view": {
                        key: value for key, value in job["input"].items() if key != "resources"
                    },
                    "resources": {
                        "source": "actor_job_input",
                        "view_label": "Start of Actor activation",
                        "snapshot": job["input"].get("resources", {}),
                    },
                }
            )
    collections = {"chat": chat, "mail": mail, "board": board, "events": events}
    totals = {
        **summary["counts"],
        "chat": summary["counts"]["owner_chat"] + summary["counts"]["agent_chat"],
    }
    metadata = {
        key: {
            "total": totals[key],
            "returned": len(value),
            "has_more": offset + len(value) < totals[key],
        }
        for key, value in collections.items()
    }
    has_more = any(item["has_more"] for item in metadata.values())
    return await english_view(
        request,
        safe_value(
            {
                "run_id": run_id,
                **summary,
                **collections,
                "jobs": jobs,
                "perspectives": perspectives,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "next_offset": offset + limit if has_more else None,
                    "complete": not has_more,
                    "collections": metadata,
                    "jobs_included": offset == 0,
                    "jobs_total": summary["jobs"]["total"],
                    "jobs_truncated": offset == 0 and summary["jobs"]["total"] > len(jobs),
                },
            }
        ),
    )
