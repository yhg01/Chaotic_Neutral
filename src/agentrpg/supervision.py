"""Opt-in durable retry deadlines and a commerce progress watchdog.

This module never invokes a model, changes authority, or repeats a world action.
It resumes only a classified temporary worker failure, using existing job IDs.
"""

from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentrpg.director import DirectorError, emit, fingerprint, row, rows
from agentrpg.shop import ShopService


class SupervisionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_retries: int = Field(default=2, strict=True, ge=0, le=5)
    base_delay_seconds: float = Field(default=1.0, strict=True, ge=0.01, le=3600)
    max_delay_seconds: float = Field(default=30.0, strict=True, ge=0.01, le=3600)
    stagnant_windows: int = Field(default=3, strict=True, ge=2, le=100)

    @model_validator(mode="after")
    def delay_order(self):
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("Maximum retry delay cannot be shorter than its base delay.")
        return self


class RuntimeSupervisor:
    def __init__(self, pool):
        self.pool = pool

    async def bind(self, run_id, policy: SupervisionPolicy):
        """Freeze recovery before the first job; resume cannot change its limits."""
        run_id = UUID(str(run_id))
        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
            )
            if run is None:
                raise DirectorError("Run not found.")
            saved = await row(conn, "SELECT * FROM run_supervision WHERE run_id=%s", (run_id,))
            if saved:
                if saved["policy"] != policy.model_dump():
                    raise DirectorError("Resume must keep the original supervision policy.")
                return
            if (
                run["status"] not in {"pending", "running"}
                or run["current_window"] != 0
                or await row(conn, "SELECT 1 FROM run_jobs WHERE run_id=%s LIMIT 1", (run_id,))
            ):
                raise DirectorError("Enable supervision before the first participant job.")
            if policy.max_retries > run["scenario"].get("limits", {}).get("recovery_attempts", 2):
                raise DirectorError("Supervision cannot exceed the frozen scenario recovery limit.")
            await conn.execute(
                "INSERT INTO run_supervision(run_id,policy) VALUES (%s,%s)",
                (run_id, Jsonb(policy.model_dump())),
            )

    async def recover(self, run_id):
        """Return waiting, resumed, or stopped. Deadlines survive a new process."""
        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
            )
            saved = await row(conn, "SELECT * FROM run_supervision WHERE run_id=%s", (run_id,))
            if (
                not run
                or not saved
                or run["status"] != "paused"
                or (run["result"] or {}).get("reason") != "worker_failure"
            ):
                return "stopped"
            policy = SupervisionPolicy.model_validate(saved["policy"])
            kind = {"actors": "actor", "agents": "agent"}.get(run["phase"])
            if kind is None:
                return "stopped"
            failed = await rows(
                conn,
                "SELECT id,attempts,failure_kind,failure_retryable FROM run_jobs "
                "WHERE run_id=%s AND \"window\"=%s AND kind=%s AND status<>'completed' "
                "ORDER BY owner_id",
                (run_id, run["current_window"], kind),
            )
            signature = fingerprint(
                [
                    {"job": str(j["id"]), "attempt": j["attempts"], "failure": j["failure_kind"]}
                    for j in failed
                ]
            )
            if not failed or any(
                not j["failure_retryable"] or not 1 <= j["attempts"] <= policy.max_retries
                for j in failed
            ):
                await conn.execute(
                    "UPDATE run_supervision SET retry_due_at=NULL WHERE run_id=%s", (run_id,)
                )
                await emit(
                    conn,
                    run_id,
                    "recovery_blocked",
                    run["current_window"],
                    {"failures": sorted({j["failure_kind"] or "unknown" for j in failed})},
                    f"recovery_blocked:{signature}",
                )
                return "stopped"
            if signature != saved["retry_signature"] or saved["retry_due_at"] is None:
                attempt = max(j["attempts"] for j in failed)
                jitter = 1 + int(signature[:4], 16) / 65535 * 0.2
                delay = min(
                    policy.max_delay_seconds,
                    policy.base_delay_seconds * 2 ** (attempt - 1) * jitter,
                )
                scheduled = await row(
                    conn,
                    "UPDATE run_supervision SET retry_signature=%s,"
                    "retry_due_at=clock_timestamp()+%s*interval '1 second' "
                    "WHERE run_id=%s RETURNING retry_due_at",
                    (signature, delay, run_id),
                )
                await emit(
                    conn,
                    run_id,
                    "recovery_scheduled",
                    run["current_window"],
                    {
                        "attempt": attempt,
                        "retry_at": scheduled["retry_due_at"].isoformat(),
                        "failure_kinds": sorted({j["failure_kind"] for j in failed}),
                    },
                    f"recovery_scheduled:{signature}",
                )
                return "waiting"
            clock = await row(conn, "SELECT clock_timestamp() AS now")
            if clock["now"] < saved["retry_due_at"]:
                return "waiting"
            # The run row is locked, also by Director.control. A human pause
            # changes its reason and therefore cannot be undone by this path.
            await conn.execute(
                "UPDATE environment_runs SET status='running',result=NULL WHERE id=%s", (run_id,)
            )
            await conn.execute(
                "UPDATE run_supervision SET retry_due_at=NULL,recoveries=recoveries+1 "
                "WHERE run_id=%s",
                (run_id,),
            )
            await emit(
                conn,
                run_id,
                "recovery_resumed",
                run["current_window"],
                {
                    "job_ids": [str(j["id"]) for j in failed],
                    "attempts_preserved": [j["attempts"] for j in failed],
                },
                f"recovery_resumed:{signature}",
            )
            return "resumed"

    async def check_progress(self, run_id, completed_window):
        """Pause executable purchases that make no business progress across windows.

        Conversation, tool-call count, cart version and unrelated peer activity
        cannot reset this check. Waiting for stock, authority or settlement can.
        """
        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,)
            )
            saved = await row(conn, "SELECT * FROM run_supervision WHERE run_id=%s", (run_id,))
            if (
                not run
                or not saved
                or run["current_window"] != completed_window + 1
                or run["phase"] != "actors"
                or saved["last_health_window"] >= completed_window
            ):
                return False
            if run["status"] not in {"running", "completed"}:
                return False
            policy = SupervisionPolicy.model_validate(saved["policy"])
            stalled = []
            if await ShopService(self.pool).enabled(conn, run_id):
                participants = await rows(
                    conn, "SELECT owner_id FROM run_participants WHERE run_id=%s", (run_id,)
                )
                for person in participants:
                    state = await ShopService(self.pool).state(
                        run_id, person["owner_id"], connection=conn
                    )
                    for task in state["tasks"]:
                        previous = await row(
                            conn,
                            "SELECT * FROM run_task_health WHERE run_id=%s AND task_id=%s",
                            (run_id, task["id"]),
                        )
                        if task["status"] in {"completed", "cancelled"} and not previous:
                            continue
                        blocked = blocked_reason(task, state)
                        progress = fingerprint(task_progress(task, state))
                        seen = previous["seen_progress_hashes"] if previous else []
                        changed = (
                            not previous
                            or progress not in seen
                            or previous["blocked_reason"] != blocked
                        )
                        if progress not in seen:
                            seen = [*seen, progress][-256:]
                        stagnant = 0 if changed or blocked else previous["stagnant_windows"] + 1
                        last_progress = (
                            completed_window if changed else previous["last_progress_window"]
                        )
                        await conn.execute(
                            "INSERT INTO run_task_health VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                            "ON CONFLICT(run_id,owner_id,task_id) DO UPDATE SET "
                            "progress_hash=EXCLUDED.progress_hash,seen_progress_hashes=EXCLUDED.seen_progress_hashes,"
                            "last_progress_window=EXCLUDED.last_progress_window,"
                            "last_checked_window=EXCLUDED.last_checked_window,stagnant_windows=EXCLUDED.stagnant_windows,"
                            "blocked_reason=EXCLUDED.blocked_reason",
                            (
                                run_id,
                                person["owner_id"],
                                task["id"],
                                progress,
                                seen,
                                last_progress,
                                completed_window,
                                stagnant,
                                blocked,
                            ),
                        )
                        if stagnant >= policy.stagnant_windows:
                            stalled.append(str(task["id"]))
                            await emit(
                                conn,
                                run_id,
                                "task_stalled",
                                completed_window,
                                {
                                    "task_id": str(task["id"]),
                                    "task_key": task["task_key"],
                                    "unchanged_windows": stagnant,
                                    "progress_hash": progress,
                                },
                                f"task_stalled:{task['id']}:{completed_window}",
                                person["owner_id"],
                            )
            await conn.execute(
                "UPDATE run_supervision SET last_health_window=%s WHERE run_id=%s",
                (completed_window, run_id),
            )
            if stalled and run["status"] == "running":
                await conn.execute(
                    "UPDATE environment_runs SET status='paused',result=%s WHERE id=%s",
                    (Jsonb({"reason": "no_progress", "task_ids": stalled}), run_id),
                )
                return True
            return False


def task_progress(task, state):
    return {
        "status": task["status"],
        "cart": sorted(
            (line["origin_id"], line["quantity"], line["quoted_price"])
            for line in state["cart"]["lines"]
        )
        if state["cart"]["task_id"] == task["id"]
        else [],
        "orders": sorted(
            (o["cart_version"], o["status"], o["paid_total"])
            for o in state["orders"]
            if o["task_id"] == task["id"]
        ),
    }


def blocked_reason(task, state):
    if task["status"] != "open":
        return "waiting_settlement" if task["status"] == "submitted" else "closed"
    consent = state["consent"]
    allowed = set(consent["buy_items"])
    if not consent["buy_budget"] or not allowed.intersection(task["item_ids"]):
        return "awaiting_authority"
    expiry = consent.get("expires_window")
    if expiry is not None and state["run"]["current_window"] > expiry:
        return "awaiting_authority"
    if any(
        item["origin_id"] in task["item_ids"]
        and item["origin_id"] in allowed
        and item["quantity"] >= task["quantity"]
        and item["unit_price"] * task["quantity"]
        <= min(task["max_total"], consent["buy_budget"], state["balance"])
        for item in state["catalog"]
    ):
        return None
    return "awaiting_stock_funds_or_terms"
