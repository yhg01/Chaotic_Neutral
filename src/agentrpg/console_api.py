"""Read-only replay of saved simulation evidence, with story-time views for clocked runs.

The observer routes keep local operator access. The /api/v1/runs clock routes need a Bearer
credential, because local operator access stays limited to observer and console routes.
"""

import base64
import binascii
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request

from agentrpg.clock import ClockError, parse_at, parse_start
from agentrpg.director import row, rows
from agentrpg.drama import LEVELS
from agentrpg.english_display import english_view
from agentrpg.main import (
    AUTH,
    MESSAGE_SELECT,
    OPERATOR_AUTH,
    Principal,
    add_participants,
    message,
    require_admin,
)

OBSERVER = "/api/v1/observer"
router = APIRouter(tags=["run console"])
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
    "voice",
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
    "clock",
}
AGENT_FIELDS = {
    "run_id",
    "participant_id",
    "activation_id",
    "window_id",
    "source_event_ids",
    "owner_messages",
    "world_observations",
    "now_text",
    "wake_reason",
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
        "SELECT id,cluster_id,status,phase,current_window,max_windows,"
        "scenario->'clock' AS clock,scenario->'owners' AS owners "
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


def saved_perspectives(jobs):
    """Keep the saved input fields, and give one Actor view for each Actor job."""
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
                    **({"at": job["at"], "story_s": job["story_s"]} if "at" in job else {}),
                }
            )
    return perspectives


@router.get(OBSERVER + "/runs/{run_id}/turns")
async def turns(run_id: UUID, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run = await run_state(conn, run_id)
        clock = await story_clock(conn, run)
        items = await (clock_summaries(conn, run, clock) if clock else summaries(conn, run))
    return {
        "run_id": run_id,
        "current_window": run["current_window"],
        "max_windows": run["max_windows"],
        "items": items,
        **({"clocked": True} if clock else {}),
    }


@router.get(OBSERVER + "/runs/{run_id}/turns/{window}")
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
        clock = await story_clock(conn, run)
        if clock:
            page_view = await clocked_turn(conn, run, clock, window, limit, offset)
            return await english_view(request, safe_value(page_view))
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
               m.thread_id,m.title,m.body,m.created_at,%s::integer AS "window",
               (SELECT r.audience FROM messages r WHERE r.id=m.thread_id) AS audience
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
    perspectives = saved_perspectives(jobs)
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


# Clocked runs. A window is one page of story time, usually two hours. One owner can have many
# jobs on one page, and created_at holds story time. Window runs never reach the code below,
# except the board read.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_WINDOW = 2**31 - 1
TIME_BOUND = 10**11
QUEUE_LIMIT = 20
MAX_BLOCKS = 2400
BOARD_PAGE = 500
# The timeline already has its own row for these records.
TIMELINE_HIDDEN = [*AUDIT_EVENTS, "owner_message", "inbox_received", "board_posted", "board_batch"]
PLOT_EVENTS = {"director_plan", "beat", "perturbation", "director_review"}


@dataclass(frozen=True)
class StoryClock:
    epoch: datetime
    zone: timezone
    epoch_s: int
    horizon_s: int
    now_s: int
    started: bool
    config: dict

    def moment(self, seconds) -> datetime:
        return self.epoch + timedelta(seconds=seconds)

    def story_s(self, moment: datetime) -> float:
        return (moment - self.epoch).total_seconds()

    def label(self, seconds) -> str:
        """Weekday and time in the zone of start_at. A later week is named, earlier days counted."""
        if seconds < 0:
            days = int(-seconds // 86400)
            if days:
                return f"{days} day{'' if days == 1 else 's'} before the start"
            hours = math.ceil(-seconds / 3600)
            return f"{hours} hour{'' if hours == 1 else 's'} before the start"
        local = self.moment(seconds).astimezone(self.zone)
        first = self.epoch.astimezone(self.zone).date()
        week = ((local.date() - first).days + first.weekday()) // 7
        return f"{f'Wk{week + 1} ' if week else ''}{WEEKDAYS[local.weekday()]} {local:%H:%M}"

    def clock_text(self, seconds) -> str:
        return f"{self.moment(seconds).astimezone(self.zone):%H:%M}"

    def days(self) -> list[dict]:
        """Day marks for the week axis: the start, then each local midnight before the horizon."""
        start = self.epoch.astimezone(self.zone)
        marks = [{"start_s": 0, "label": WEEKDAYS[start.weekday()]}]
        midnight = datetime(start.year, start.month, start.day, tzinfo=self.zone)
        while True:
            midnight += timedelta(days=1)
            seconds = int((midnight - self.epoch).total_seconds())
            if seconds >= self.horizon_s:
                return marks
            marks.append({"start_s": seconds, "label": WEEKDAYS[midnight.weekday()]})


async def story_clock(conn, run) -> StoryClock | None:
    """Read the clock of a clocked run. A run with no clock section returns None."""
    section = run.get("clock")
    if not section:
        return None
    state = await row(
        conn, "SELECT epoch,sim_s,config FROM clock_state WHERE run_id=%s", (run["id"],)
    )
    config = {**section, **((state or {}).get("config") or {})}
    try:
        start = parse_start(config.get("start_at"))
    except ClockError:
        raise HTTPException(409, "This run's clock has no valid start time.") from None
    return StoryClock(
        epoch=state["epoch"] if state else start,
        zone=timezone(start.utcoffset()),
        epoch_s=max(1, int(config.get("epoch_minutes") or 120)) * 60,
        horizon_s=max(1, int(config.get("duration_minutes") or 10080)) * 60,
        now_s=state["sim_s"] if state else 0,
        started=state is not None and (run["status"] != "pending" or state["sim_s"] > 0),
        config=config,
    )


async def clocked_run(conn, run_id) -> tuple[dict, StoryClock]:
    run = await run_state(conn, run_id)
    clock = await story_clock(conn, run)
    if clock is None:
        raise HTTPException(404, "This run has no clock.")
    return run, clock


def stamp(record: dict, clock: StoryClock, seconds=None) -> dict:
    value = clock.story_s(record["created_at"]) if seconds is None else seconds
    record["story_s"] = value
    record["at"] = clock.label(value)
    return record


def stamp_action(record: dict, clock: StoryClock) -> dict:
    stamp(record, clock, record["start_s"])
    record["ends"] = clock.label(record["start_s"] + (record["duration_s"] or 0))
    return record


def stamp_job(record: dict, clock: StoryClock) -> dict:
    stamp(record, clock, record["start_s"])
    if record["end_s"] is not None:
        record["ends"] = clock.label(record["end_s"])
    return record


def page_bounds(run, window) -> tuple[int, int]:
    """The final page also holds anything saved at or after the end of the run."""
    return window, MAX_WINDOW if window >= run["max_windows"] - 1 else window


def page_moments(run, clock, window) -> tuple[datetime, datetime]:
    upper = TIME_BOUND if window >= run["max_windows"] - 1 else (window + 1) * clock.epoch_s
    return clock.moment(window * clock.epoch_s), clock.moment(upper)


async def clock_summaries(conn, run, clock):
    """One summary for each page from the start to the live cursor."""
    run_id = run["id"]
    last = run["max_windows"] - 1
    top = min(run["current_window"], last)
    tally: dict[int, dict] = {window: {} for window in range(top + 1)}

    def merge(records):
        for item in records:
            page = min(int(item["page"]), last)
            target = tally.get(page)
            if target is None:
                continue
            for key, value in item.items():
                if key == "settled":
                    target[key] = target.get(key, False) or value
                elif key != "page":
                    target[key] = target.get(key, 0) + value

    merge(
        await rows(
            conn,
            """SELECT "window" AS page,count(*) FILTER (WHERE source='owner') AS owner_chat,
            count(*) FILTER (WHERE source='agent') AS agent_chat
            FROM owner_messages WHERE run_id=%s GROUP BY 1""",
            (run_id,),
        )
    )
    merge(
        await rows(
            conn,
            """SELECT "window" AS page,count(*) AS events,
            bool_or(kind='window_completed') AS settled
            FROM run_events WHERE run_id=%s AND kind<>ALL(%s) GROUP BY 1""",
            (run_id, AUDIT_EVENTS),
        )
    )
    merge(
        await rows(
            conn,
            """SELECT "window" AS page,count(*) AS mail FROM inbox_messages
            WHERE run_id=%s GROUP BY 1""",
            (run_id,),
        )
    )
    merge(
        await rows(
            conn,
            """SELECT "window" AS page,count(*) AS jobs,
            count(*) FILTER (WHERE kind='actor') AS actor_jobs,
            count(*) FILTER (WHERE kind='agent') AS agent_jobs,
            count(*) FILTER (WHERE status='completed') AS completed_jobs,
            count(*) FILTER (WHERE status='failed') AS failed_jobs
            FROM run_jobs WHERE run_id=%s GROUP BY 1""",
            (run_id,),
        )
    )
    merge(
        await rows(
            conn,
            """SELECT floor(EXTRACT(EPOCH FROM (m.created_at-%s::timestamptz))/%s)::bigint AS page,
            count(*) AS board,
            count(*) FILTER (WHERE root.audience IS NOT NULL) AS direct
            FROM messages m JOIN messages root ON root.id=m.thread_id
            WHERE m.cluster_id=%s AND m.created_at>=%s::timestamptz GROUP BY 1""",
            (clock.epoch, clock.epoch_s, run["cluster_id"], clock.epoch),
        )
    )
    merge(
        await rows(
            conn,
            "SELECT start_s/%s AS page,count(*) AS actions FROM clock_actions "
            "WHERE run_id=%s GROUP BY 1",
            (clock.epoch_s, run_id),
        )
    )
    merge(
        await rows(
            conn,
            "SELECT fire_s/%s AS page,count(*) AS perturbations FROM clock_draws "
            "WHERE run_id=%s AND outcome<>'none' AND fire_s IS NOT NULL GROUP BY 1",
            (clock.epoch_s, run_id),
        )
    )
    result = []
    for window, item in sorted(tally.items()):
        is_current = window == run["current_window"]
        settled = item.get("settled", False)
        start = window * clock.epoch_s
        end = min(start + clock.epoch_s, clock.horizon_s)
        result.append(
            {
                "window": window,
                "phase": "completed" if settled else run["phase"] if is_current else "recorded",
                "status": "completed" if settled else run["status"] if is_current else "recorded",
                "counts": {
                    key: item.get(key, 0)
                    for key in (
                        "owner_chat",
                        "agent_chat",
                        "events",
                        "mail",
                        "board",
                        "direct",
                        "actions",
                        "perturbations",
                    )
                },
                "jobs": {
                    "total": item.get("jobs", 0),
                    "actor": item.get("actor_jobs", 0),
                    "agent": item.get("agent_jobs", 0),
                    "completed": item.get("completed_jobs", 0),
                    "failed": item.get("failed_jobs", 0),
                },
                "has_actor_view": bool(item.get("actor_jobs", 0)),
                "start_s": start,
                "end_s": end,
                "at": clock.label(start),
                "until": clock.clock_text(end),
            }
        )
    return result


async def board_owners(conn, run) -> dict:
    records = await rows(
        conn, "SELECT agent_id,owner_id FROM run_participants WHERE run_id=%s", (run["id"],)
    )
    return {item["agent_id"]: item["owner_id"] for item in records}


async def board_posts(conn, run, condition, args, limit, offset=0):
    """Read posts with the board's own admin select, labelled public or direct."""
    records = await rows(
        conn,
        MESSAGE_SELECT + " WHERE m.cluster_id=%s" + condition + " ORDER BY m.created_at,m.seq"
        " LIMIT %s OFFSET %s",
        (run["cluster_id"], *args, limit, offset),
    )
    await add_participants(conn, records)
    owners = await board_owners(conn, run)
    return [
        {
            **message(record),
            "seq": record["seq"],
            "owner_id": owners.get(record["author_id"]),
            "visibility": "public" if record["audience"] is None else "direct",
            "participants": record.get("participants", []),
        }
        for record in records
    ]


def page_metadata(collections, totals, offset):
    return {
        key: {
            "total": totals[key],
            "returned": len(value),
            "has_more": offset + len(value) < totals[key],
        }
        for key, value in collections.items()
    }


async def clocked_turn(conn, run, clock, window, limit, offset):
    """Every record of one page, in story order, with no one-job-per-owner limit."""
    run_id = run["id"]
    items = await clock_summaries(conn, run, clock)
    summary = next((item for item in items if item["window"] == window), None)
    if summary is None:
        raise HTTPException(404, "Turn has not been recorded.")
    first, final = page_bounds(run, window)
    lower, upper = page_moments(run, clock, window)
    start_s = window * clock.epoch_s
    end_s = TIME_BOUND if final == MAX_WINDOW else start_s + clock.epoch_s
    chat = await rows(
        conn,
        """SELECT id,owner_id,source,content,"window",created_at FROM owner_messages
           WHERE run_id=%s AND "window" BETWEEN %s AND %s
           ORDER BY created_at,id LIMIT %s OFFSET %s""",
        (run_id, first, final, limit, offset),
    )
    mail = await rows(
        conn,
        """SELECT id,sender_owner_id,sender_source,recipient_owner_id,subject,body,
           in_reply_to,"window",created_at FROM inbox_messages
           WHERE run_id=%s AND "window" BETWEEN %s AND %s
           ORDER BY created_at,id LIMIT %s OFFSET %s""",
        (run_id, first, final, limit, offset),
    )
    board = await board_posts(
        conn, run, " AND m.created_at>=%s AND m.created_at<%s", (lower, upper), limit, offset
    )
    events = await rows(
        conn,
        """SELECT id,kind,"window",owner_id,payload,cause_process,created_at FROM run_events
           WHERE run_id=%s AND "window" BETWEEN %s AND %s AND kind<>ALL(%s)
           ORDER BY created_at,id LIMIT %s OFFSET %s""",
        (run_id, first, final, AUDIT_EVENTS, limit, offset),
    )
    jobs = await rows(
        conn,
        """SELECT id,owner_id,"window",kind,input,output,output_meta,status,attempts,error,
           failure_kind,created_at,clock_seq,start_s,end_s FROM run_jobs
           WHERE run_id=%s AND "window" BETWEEN %s AND %s
           ORDER BY start_s NULLS LAST,created_at,clock_seq,id LIMIT %s OFFSET %s""",
        (run_id, first, final, limit, offset),
    )
    actions = await rows(
        conn,
        """SELECT id,owner_id,process,label,detail,start_s,duration_s,status,reachable,
           replaced_by FROM clock_actions WHERE run_id=%s AND start_s>=%s AND start_s<%s
           ORDER BY start_s,key LIMIT %s OFFSET %s""",
        (run_id, start_s, end_s, limit, offset),
    )
    perturbations = []
    if offset == 0:
        perturbations = await rows(
            conn,
            """SELECT d.action_id,d.level,d.u,d.v,d.outcome,d.fire_s,a.owner_id,a.label
               FROM clock_draws d LEFT JOIN clock_actions a
                 ON a.run_id=d.run_id AND a.id=d.action_id
               WHERE d.run_id=%s AND d.outcome<>'none' AND d.fire_s>=%s AND d.fire_s<%s
               ORDER BY d.fire_s,d.action_id""",
            (run_id, start_s, end_s),
        )
    for record in (*chat, *mail, *events):
        stamp(record, clock)
    for record in board:
        stamp(record, clock)
        record["window"] = window
        record.pop("seq")
    for record in actions:
        stamp_action(record, clock)
    for record in jobs:
        stamp_job(record, clock)
    for record in perturbations:
        stamp(perturbation_values(record), clock, record["fire_s"])
    perspectives = saved_perspectives(jobs)
    collections = {
        "chat": chat,
        "mail": mail,
        "board": board,
        "events": events,
        "jobs": jobs,
        "actions": actions,
    }
    counts = summary["counts"]
    totals = {
        **counts,
        "chat": counts["owner_chat"] + counts["agent_chat"],
        "jobs": summary["jobs"]["total"],
    }
    metadata = page_metadata(collections, totals, offset)
    has_more = any(item["has_more"] for item in metadata.values())
    return {
        "run_id": run["id"],
        **summary,
        **collections,
        "perspectives": perspectives,
        "perturbations": perturbations,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if has_more else None,
            "complete": not has_more,
            "collections": metadata,
            "jobs_included": True,
            "jobs_total": summary["jobs"]["total"],
            "jobs_truncated": False,
        },
    }


def perturbation_values(record: dict) -> dict:
    for key in ("u", "v"):
        record[key] = None if record[key] is None else float(record[key])
    return record


def action_blocks(records, horizon_s):
    """Join very short neighbours so a week of tool calls stays readable and bounded."""
    resolution = max(60, horizon_s // 1000)
    while True:
        blocks = []
        open_blocks = {}
        for item in records:
            director = item["process"] == "director"
            lane = ("", "director") if director else (item["owner_id"], item["process"])
            duration = item["duration_s"] or 0
            previous = open_blocks.get(lane)
            if (
                previous is not None
                and duration < resolution
                and (previous["duration_s"] < resolution or previous["count"] > 1)
                and previous["status"] == item["status"]
                and item["start_s"] - (previous["start_s"] + previous["duration_s"]) <= resolution
            ):
                previous["duration_s"] = max(
                    previous["duration_s"], item["start_s"] + duration - previous["start_s"]
                )
                previous["count"] += 1
                continue
            block = {
                "owner_id": item["owner_id"],
                "process": item["process"],
                "label": item["label"],
                "category": item["category"],
                "status": item["status"],
                "start_s": item["start_s"],
                "duration_s": duration,
                "count": 1,
            }
            blocks.append(block)
            open_blocks[lane] = block
        if len(blocks) <= MAX_BLOCKS:
            return blocks
        resolution *= 2


async def clock_view(conn, run, clock):
    """The complete research view. participant_clock removes private parts for an Agent."""
    run_id = run["id"]
    names = {
        item.get("id"): item.get("name")
        for item in run.get("owners") or []
        if isinstance(item, dict)
    }
    processes = await rows(
        conn,
        """SELECT owner_id,kind,bar_s,state,until_s,activation_seq,last_wake_s
        FROM clock_processes WHERE run_id=%s ORDER BY owner_id,kind""",
        (run_id,),
    )
    current = await rows(
        conn,
        """SELECT DISTINCT ON (owner_id,process) id,owner_id,process,label,start_s,duration_s,
        status,reachable,detail->>'category' AS category FROM clock_actions
        WHERE run_id=%s AND status='active' ORDER BY owner_id,process,start_s DESC,key""",
        (run_id,),
    )
    current_actions = {
        (item["owner_id"], item["process"]): stamp_action(item, clock) for item in current
    }
    for item in processes:
        item["owner_name"] = names.get(item["owner_id"]) or item["owner_id"]
        item["bar"] = clock.label(item["bar_s"])
        item["until"] = None if item["until_s"] is None else clock.label(item["until_s"])
        item["current_action"] = current_actions.get((item["owner_id"], item["kind"]))
    queue = await rows(
        conn,
        """SELECT id,due_s,class,key,kind,owner_id,payload,source,created_sim_s FROM clock_events
        WHERE run_id=%s AND status='queued' ORDER BY due_s,class,tie,key LIMIT %s""",
        (run_id, QUEUE_LIMIT),
    )
    for item in queue:
        stamp(item, clock, item["due_s"])
    drawn = await rows(
        conn,
        """SELECT d.action_id,d.level,d.u,d.v,d.outcome,d.fire_s,a.owner_id,a.label,
        a.start_s AS action_start_s,a.duration_s AS action_duration_s,a.status AS action_status
        FROM clock_draws d LEFT JOIN clock_actions a ON a.run_id=d.run_id AND a.id=d.action_id
        WHERE d.run_id=%s AND d.outcome<>'none' ORDER BY d.fire_s NULLS LAST,d.action_id""",
        (run_id,),
    )
    for item in drawn:
        perturbation_values(item)
        item["fired"] = item["fire_s"] is not None and item["fire_s"] <= clock.now_s
        if item["fire_s"] is not None:
            stamp(item, clock, item["fire_s"])
    drama = clock.config.get("drama", "off")
    level = LEVELS.get(drama)
    sites = await rows(
        conn,
        """SELECT site_id,name,kind,version,source,sim_s,spec->>'tagline' AS tagline
        FROM web_sites WHERE run_id=%s ORDER BY sim_s,site_id""",
        (run_id,),
    )
    for item in sites:
        item["url"] = f"/sim/{run_id}/{item['site_id']}/"
        item["at"] = clock.label(item["sim_s"])
    actions = await rows(
        conn,
        """SELECT owner_id,process,label,start_s,duration_s,status,
        detail->>'category' AS category FROM clock_actions WHERE run_id=%s
        ORDER BY start_s,key""",
        (run_id,),
    )
    return {
        "run_id": run_id,
        "role": "admin",
        "started": clock.started,
        "status": run["status"],
        "current_window": run["current_window"],
        "max_windows": run["max_windows"],
        "now_s": clock.now_s,
        "now": clock.label(clock.now_s),
        "now_at": clock.moment(clock.now_s),
        "start": clock.label(0),
        "start_at": clock.epoch,
        "horizon_s": clock.horizon_s,
        "horizon": clock.label(clock.horizon_s),
        "horizon_at": clock.moment(clock.horizon_s),
        "epoch_s": clock.epoch_s,
        "days": clock.days(),
        "drama": drama,
        "processes": processes,
        "blocks": action_blocks(actions, clock.horizon_s),
        "queue": queue,
        "perturbations": drawn,
        "interference": await interference(conn, run_id, level.rate if level else None),
        "sites": sites,
        "assistants": await assistant_status(conn, run, clock, processes, names),
        "spend": await run_spend(conn, run_id),
    }


async def interference(conn, run_id, target_rate):
    """The measured share of saved draws that interfered. Draws of no effect also count."""
    records = await rows(
        conn,
        "SELECT outcome,count(*) AS total FROM clock_draws WHERE run_id=%s GROUP BY outcome",
        (run_id,),
    )
    totals = {item["outcome"]: item["total"] for item in records}
    drawn = sum(totals.values())
    interfered = totals.get("mild", 0) + totals.get("extreme", 0)
    return {
        "drawn": drawn,
        "mild": totals.get("mild", 0),
        "extreme": totals.get("extreme", 0),
        "interfered": interfered,
        "rate": interfered / drawn if drawn else None,
        "target_rate": target_rate,
    }


async def assistant_status(conn, run, clock, processes, names):
    """One entry for each assistant, with the bills that keep it running."""
    run_id = run["id"]
    expired = await rows(
        conn,
        "SELECT owner_id,created_at FROM run_events WHERE run_id=%s AND kind='assistant_expired'",
        (run_id,),
    )
    expired_at = {item["owner_id"]: item["created_at"] for item in expired}
    bills = await rows(
        conn,
        "SELECT site_id,bill_id,owner_id,title,amount,due_text,status,paid_sim_s "
        "FROM web_bills WHERE run_id=%s",
        (run_id,),
    )
    known = {(item["site_id"], item["bill_id"]): item for item in bills}
    plans: dict = {}
    for entry in clock.config.get("lifecycle") or []:
        if not isinstance(entry, dict):
            continue
        bill = known.get((entry.get("site_id"), entry.get("bill_id")))
        try:
            due_s = parse_at(entry.get("due_at"), clock.epoch.astimezone(clock.zone)) * 60
        except ClockError:
            due_s = None
        plans.setdefault(entry.get("owner_id"), []).append(
            {
                "kind": entry.get("kind"),
                "site_id": entry.get("site_id"),
                "bill_id": entry.get("bill_id"),
                "due_at": entry.get("due_at"),
                "due_s": due_s,
                "amount": bill["amount"] if bill else entry.get("amount"),
                "status": bill["status"] if bill else "not created",
                "paid_at": clock.label(bill["paid_sim_s"])
                if bill and bill["paid_sim_s"] is not None
                else None,
            }
        )
    result = []
    for item in processes:
        if item["kind"] != "agent":
            continue
        owner = item["owner_id"]
        ended = item["state"] == "dead" or owner in expired_at
        result.append(
            {
                "owner_id": owner,
                "owner_name": names.get(owner) or owner,
                "state": item["state"],
                "status": "expired" if ended else "active",
                "expired_at": clock.label(clock.story_s(expired_at[owner]))
                if owner in expired_at
                else None,
                "bills": plans.get(owner, []),
            }
        )
    return result


async def run_spend(conn, run_id):
    """Spend of this run. Uncertain cost reservations stay in the reserved total."""
    records = await rows(
        conn,
        """SELECT role,count(*) AS attempts,COALESCE(sum(reported_usd),0) AS spent,
        COALESCE(sum(CASE WHEN status='reported' THEN 0
            ELSE GREATEST(reserved_usd-COALESCE(reported_usd,0),0) END),0) AS reserved
        FROM model_spend_attempts WHERE run_id=%s GROUP BY role ORDER BY role""",
        (run_id,),
    )
    campaigns = await rows(
        conn,
        """SELECT c.id,c.limit_usd,c.halted FROM model_campaigns c WHERE c.id IN
        (SELECT campaign_id FROM model_spend_attempts WHERE run_id=%s) ORDER BY c.id""",
        (run_id,),
    )
    return {
        "spent_usd": str(sum(item["spent"] for item in records)),
        "reserved_usd": str(sum(item["reserved"] for item in records)),
        "attempts": sum(item["attempts"] for item in records),
        "by_role": [
            {
                "role": item["role"],
                "attempts": item["attempts"],
                "spent_usd": str(item["spent"]),
                "reserved_usd": str(item["reserved"]),
            }
            for item in records
        ],
        "campaigns": [
            {"id": item["id"], "limit_usd": str(item["limit_usd"]), "halted": item["halted"]}
            for item in campaigns
        ],
    }


def participant_clock(view: dict, owner_id: str) -> dict:
    """An Agent credential sees time and its own process. Plot and peer state stay private."""

    def process(item):
        if item["owner_id"] == owner_id:
            return item
        return {key: item[key] for key in ("owner_id", "owner_name", "kind", "state")}

    def queued(item):
        own = item["owner_id"] in (None, owner_id)
        visible = own and item["kind"] not in PLOT_EVENTS
        return {
            "due_s": item["due_s"],
            "at": item["at"],
            "kind": item["kind"] if visible else "scheduled",
        }

    return {
        **view,
        "role": "agent",
        "drama": None,
        "processes": [process(item) for item in view["processes"]],
        "blocks": [],
        "queue": [queued(item) for item in view["queue"]],
        "perturbations": [],
        "interference": None,
        "sites": [
            {key: item[key] for key in ("site_id", "name", "kind", "tagline")}
            for item in view["sites"]
        ],
        "assistants": [item for item in view["assistants"] if item["owner_id"] == owner_id],
        "spend": None,
    }


async def read_clock(request, run_id):
    async with request.app.state.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run, clock = await clocked_run(conn, run_id)
        return await clock_view(conn, run, clock)


@router.get(OBSERVER + "/runs/{run_id}/clock")
async def observer_clock(run_id: UUID, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    return await english_view(request, safe_value(await read_clock(request, run_id)))


@router.get("/api/v1/runs/{run_id}/clock")
async def run_clock(run_id: UUID, request: Request, principal: AUTH):
    if principal.role == "admin":
        return await english_view(request, safe_value(await read_clock(request, run_id)))
    owner = await participant_owner(request, run_id, principal)
    return safe_value(participant_clock(await read_clock(request, run_id), owner))


async def participant_owner(request, run_id, principal: Principal) -> str:
    """Another cluster's run does not exist for this credential."""
    async with request.app.state.pool.connection() as conn:
        record = await row(
            conn,
            "SELECT owner_id FROM run_participants WHERE run_id=%s AND agent_id=%s",
            (run_id, principal.agent["id"]),
        )
    if record is None:
        raise HTTPException(404, "Run not found.")
    return record["owner_id"]


def encode_position(scope: str, *values) -> str:
    raw = json.dumps([1, scope, *values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_position(value: str | None, scope: str, kinds: tuple) -> tuple | None:
    """Read a page position. A position from another run or route is refused."""
    if value is None:
        return None
    try:
        decoded = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2 + len(kinds)
            or decoded[0] != 1
            or decoded[1] != scope
            or any(type(item) is not kind for item, kind in zip(decoded[2:], kinds, strict=True))
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(422, "Invalid page position.") from None
    return tuple(decoded[2:])


def story_us(column: str) -> str:
    return f"round(EXTRACT(EPOCH FROM ({column}-%(epoch)s::timestamptz))*1000000)::bigint"


JOB_US = story_us("j.created_at")
EVENT_US = story_us("e.created_at")
CHAT_US = story_us("c.created_at")
MAIL_US = story_us("i.created_at")
TIMELINE_SQL = f"""WITH merged AS (
    SELECT 'action' AS type,0 AS rank,a.id::text AS id,a.start_s*1000000 AS story_us,
        a.owner_id,a.process AS source,a.label AS title,NULL::text AS summary,a.status,
        a.duration_s,a.detail AS detail
    FROM clock_actions a WHERE a.run_id=%(run)s
    UNION ALL
    SELECT 'job',1,j.id::text,COALESCE(j.start_s*1000000,{JOB_US}),j.owner_id,
        j.kind,j.kind||' job',j.error,j.status,j.end_s-j.start_s,
        jsonb_build_object('window',j."window",'clock_seq',j.clock_seq,'attempts',j.attempts,
            'failure_kind',j.failure_kind)
    FROM run_jobs j WHERE j.run_id=%(run)s
    UNION ALL
    SELECT 'perturbation',2,d.action_id::text,d.fire_s*1000000,a.owner_id,d.level,d.outcome,
        a.label,CASE WHEN d.fire_s<=%(now)s THEN 'fired' ELSE 'drawn' END,NULL::bigint,
        jsonb_build_object('u',d.u,'v',d.v,'level',d.level,'outcome',d.outcome)
    FROM clock_draws d LEFT JOIN clock_actions a ON a.run_id=d.run_id AND a.id=d.action_id
    WHERE d.run_id=%(run)s AND d.outcome<>'none' AND d.fire_s IS NOT NULL
    UNION ALL
    SELECT 'event',3,e.id::text,{EVENT_US},e.owner_id,e.cause_process,e.kind,
        NULL::text,NULL::text,NULL::bigint,e.payload
    FROM run_events e WHERE e.run_id=%(run)s AND e.kind<>ALL(%(hidden)s)
    UNION ALL
    SELECT 'chat',4,c.id::text,{CHAT_US},c.owner_id,c.source,c.source||' message',
        left(c.content,280),NULL::text,NULL::bigint,NULL::jsonb
    FROM owner_messages c WHERE c.run_id=%(run)s
    UNION ALL
    SELECT 'mail',5,i.id::text,{MAIL_US},i.recipient_owner_id,i.sender_owner_id,
        i.subject,left(i.body,280),NULL::text,NULL::bigint,NULL::jsonb
    FROM inbox_messages i WHERE i.run_id=%(run)s
    UNION ALL
    SELECT CASE WHEN root.audience IS NULL THEN 'board' ELSE 'direct' END,6,
        m.id::text,{story_us("m.created_at")},p.owner_id,au.name,m.title,left(m.body,280),
        NULL::text,NULL::bigint,NULL::jsonb
    FROM messages m JOIN agents au ON au.id=m.author_id
    JOIN messages root ON root.id=m.thread_id
    LEFT JOIN run_participants p ON p.agent_id=m.author_id AND p.run_id=%(run)s
    WHERE m.cluster_id=%(cluster)s
) SELECT * FROM merged
WHERE story_us>=%(from_us)s AND story_us<%(to_us)s
  AND (story_us,rank,id)>(%(after_us)s::bigint,%(after_rank)s::integer,%(after_id)s::text)
ORDER BY story_us,rank,id LIMIT %(limit)s"""


async def read_timeline(request, run_id, start, end, after, limit):
    """Actions, jobs, perturbations, events and messages merged into one story-order list."""
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "The from time must not be later than the to time.")
    scope = f"timeline:{run_id}"
    position = decode_position(after, scope, (int, int, str))
    async with request.app.state.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run, clock = await clocked_run(conn, run_id)
        records = await rows(
            conn,
            TIMELINE_SQL,
            {
                "epoch": clock.epoch,
                "run": run["id"],
                "cluster": run["cluster_id"],
                "now": clock.now_s,
                "hidden": TIMELINE_HIDDEN,
                "from_us": -TIME_BOUND * 1000000 if start is None else start * 1000000,
                "to_us": TIME_BOUND * 1000000 if end is None else end * 1000000,
                "after_us": position[0] if position else -(2**62),
                "after_rank": position[1] if position else -1,
                "after_id": position[2] if position else "",
                "limit": limit + 1,
            },
        )
    selected = records[:limit]
    items = []
    for record in selected:
        seconds = record["story_us"] / 1000000
        items.append(
            {
                "type": record["type"],
                "id": record["id"],
                "story_s": seconds,
                "at": clock.label(seconds),
                "window": None
                if seconds < 0
                else min(int(seconds // clock.epoch_s), run["max_windows"] - 1),
                "owner_id": record["owner_id"],
                "source": record["source"],
                "title": record["title"],
                "summary": record["summary"],
                "status": record["status"],
                "duration_s": record["duration_s"],
                "detail": record["detail"],
            }
        )
    following = None
    if len(records) > limit:
        last = selected[-1]
        following = encode_position(scope, last["story_us"], last["rank"], last["id"])
    return {
        "run_id": run_id,
        "items": items,
        "next_after": following,
        "complete": following is None,
    }


async def read_board(request, run_id, after, limit):
    """Research read of the whole board in story order. Agent credentials cannot use it."""
    scope = f"board:{run_id}"
    position = decode_position(after, scope, (str, int))
    if position:
        try:
            position = (datetime.fromisoformat(position[0]), position[1])
        except ValueError:
            raise HTTPException(422, "Invalid page position.") from None
    async with request.app.state.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run = await run_state(conn, run_id)
        clock = await story_clock(conn, run)
        condition, args = "", ()
        if position:
            condition = " AND (m.created_at,m.seq)>(%s::timestamptz,%s::bigint)"
            args = position
        records = await board_posts(conn, run, condition, args, limit + 1)
        counts = await row(
            conn,
            """SELECT count(*) FILTER (WHERE root.audience IS NULL) AS public,
            count(*) FILTER (WHERE root.audience IS NOT NULL) AS direct
            FROM messages m JOIN messages root ON root.id=m.thread_id WHERE m.cluster_id=%s""",
            (run["cluster_id"],),
        )
    selected = records[:limit]
    following = None
    if len(records) > limit:
        last = selected[-1]
        following = encode_position(scope, last["created_at"].isoformat(), last["seq"])
    for record in selected:
        record.pop("seq")
        if clock:
            stamp(record, clock)
    return {
        "run_id": run_id,
        "items": selected,
        "counts": counts,
        "next_after": following,
        "complete": following is None,
    }


TimelineLimit = Annotated[int, Query(ge=1, le=500)]
BoardLimit = Annotated[int, Query(ge=1, le=BOARD_PAGE)]
From = Annotated[int | None, Query(alias="from", ge=-TIME_BOUND, le=TIME_BOUND)]
To = Annotated[int | None, Query(alias="to", ge=-TIME_BOUND, le=TIME_BOUND)]
After = Annotated[str | None, Query(max_length=1024)]


@router.get(OBSERVER + "/runs/{run_id}/timeline")
async def observer_timeline(
    run_id: UUID,
    request: Request,
    principal: OPERATOR_AUTH,
    start: From = None,
    end: To = None,
    after: After = None,
    limit: TimelineLimit = 100,
):
    require_admin(principal)
    result = await read_timeline(request, run_id, start, end, after, limit)
    return await english_view(request, safe_value(result))


@router.get("/api/v1/runs/{run_id}/timeline")
async def run_timeline(
    run_id: UUID,
    request: Request,
    principal: AUTH,
    start: From = None,
    end: To = None,
    after: After = None,
    limit: TimelineLimit = 100,
):
    require_admin(principal)
    result = await read_timeline(request, run_id, start, end, after, limit)
    return await english_view(request, safe_value(result))


@router.get(OBSERVER + "/runs/{run_id}/board")
async def observer_board(
    run_id: UUID,
    request: Request,
    principal: OPERATOR_AUTH,
    after: After = None,
    limit: BoardLimit = BOARD_PAGE,
):
    require_admin(principal)
    return await english_view(request, safe_value(await read_board(request, run_id, after, limit)))


@router.get("/api/v1/runs/{run_id}/board")
async def run_board(
    run_id: UUID,
    request: Request,
    principal: AUTH,
    after: After = None,
    limit: BoardLimit = BOARD_PAGE,
):
    require_admin(principal)
    return await english_view(request, safe_value(await read_board(request, run_id, after, limit)))
