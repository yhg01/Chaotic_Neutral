"""Local, cost-bounded execution and durable private console interventions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentrpg.budget import SUPPORTED_MODELS
from agentrpg.director import emit, fingerprint, row, rows, save_message
from agentrpg.main import OPERATOR_AUTH, require_admin
from agentrpg.model_assignments import validate_owner_models

ROOT = Path(__file__).resolve().parents[2]
router = APIRouter(prefix="/api/v1/console", tags=["console controls"])
ACTIVE = {"queued", "running"}
TERMINAL_RUNS = {"completed", "incomplete", "invalid"}
CLOCKED_REFUSAL = (
    "The run script drives this clocked run (scripts/run_clocked.py). Step and Continue "
    "are not available here. The console replays its saved records."
)


def is_clocked(run) -> bool:
    return bool((run.get("scenario") or {}).get("clock"))


class ConsoleError(ValueError):
    """A safe operational error with no credential values."""


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class ExecutionRequest(Operation):
    mode: Literal["step", "continue"]


class InterventionRequest(Operation):
    owner_id: str = Field(min_length=1, max_length=120)
    target: Literal["actor", "agent"]
    prompt: str = Field(min_length=1, max_length=12000)
    expected_window: int = Field(ge=0, le=99, strict=True)

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Enter a prompt.")
        return value


def lock_key(run_id):
    return int.from_bytes(
        hashlib.sha256(f"console-execution:{run_id}".encode()).digest()[:8], "big", signed=True
    )


async def external_worker_active(conn, run_id):
    for prefix in ("scenario-runner", "director"):
        key = int.from_bytes(hashlib.sha256(f"{prefix}:{run_id}".encode()).digest()[:8], "big")
        # Observe locks without briefly claiming one. A read must not make a
        # newly launched worker fail its nonblocking conductor acquisition.
        active = await row(
            conn,
            """SELECT EXISTS(SELECT 1 FROM pg_locks
            WHERE locktype='advisory' AND classid=%s::oid AND objid=%s::oid
            AND objsubid=1 AND granted AND pid<>pg_backend_pid()) AS active""",
            (key >> 32, key & 0xFFFFFFFF),
        )
        if active["active"]:
            return True
    return False


def save_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".saving-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_manifest(root: Path, run_id) -> dict:
    """Read one fixed local manifest. The browser never supplies a path."""
    run_id = UUID(str(run_id))
    directory = (Path(root) / ".local/scenario-runs").resolve()
    path = directory / str(run_id) / "credentials.json"
    try:
        if path.resolve() != path or path.stat().st_size > 262144:
            raise ValueError
        value = json.loads(path.read_text())
        if UUID(value["run_id"]) != run_id:
            raise ValueError
        UUID(value["cluster_id"])
        participants = value["participants"]
        if not isinstance(participants, list) or not 1 <= len(participants) <= 15:
            raise ValueError
        owners, agents = set(), set()
        for participant in participants:
            UUID(participant["agent_id"])
            UUID(participant["session_id"])
            if not isinstance(participant["owner_id"], str) or not participant["owner_id"]:
                raise ValueError
            token = participant["token"]
            if not isinstance(token, str) or not token or token != token.strip():
                raise ValueError
            owners.add(participant["owner_id"])
            agents.add(participant["agent_id"])
        if len(owners) != len(participants) or len(agents) != len(participants):
            raise ValueError
        config = value["configuration"]
        assigned = validate_owner_models(config.get("owner_models", {}), owners)
        if not set(assigned.values()) <= SUPPORTED_MODELS:
            raise ValueError
        if config["actors"] not in {"scripted", "model"}:
            raise ValueError
        if any(config[key] not in SUPPORTED_MODELS for key in ("model", "actor_model")):
            raise ValueError
        for key, default, limit in (("max_model_calls", 18, 100), ("timeout_seconds", 240, 1800)):
            setting = config.get(key, default)
            if type(setting) is not int or not 1 <= setting <= limit:
                raise ValueError
        if not isinstance(config["instructions"], str) or not config["instructions"].strip():
            raise ValueError
        origin = urlsplit(config.get("base_url", "http://127.0.0.1:8000"))
        if (
            origin.scheme != "http"
            or origin.hostname not in {"localhost", "127.0.0.1", "::1"}
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or (origin.port is not None and not 1 <= origin.port <= 65535)
        ):
            raise ValueError
        budget = config["budget"]
        UUID(budget["campaign_id"])
        limit = Decimal(budget["limit_usd"])
        if not limit.is_finite() or not 0 < limit <= 1000000:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        raise ConsoleError(
            "This run needs a valid local credential file, saved model settings, "
            "and a bounded campaign."
        ) from None
    return value


async def checked_manifest(conn, root, run):
    manifest = load_manifest(root, run["id"])
    if manifest["cluster_id"] != str(run["cluster_id"]):
        raise ConsoleError("The saved run identities do not match the database.")
    participants = await rows(
        conn,
        """SELECT p.owner_id,p.agent_id,p.session_id,a.token_hash
        FROM run_participants p JOIN agents a ON a.id=p.agent_id WHERE p.run_id=%s""",
        (run["id"],),
    )
    expected = {
        (p["owner_id"], str(p["agent_id"]), str(p["session_id"]), p["token_hash"])
        for p in participants
    }
    actual = {
        (
            p["owner_id"],
            p["agent_id"],
            p["session_id"],
            hashlib.sha256(p["token"].encode()).hexdigest(),
        )
        for p in manifest["participants"]
    }
    if expected != actual:
        raise ConsoleError("The saved run identities do not match the database.")
    saved = manifest["configuration"]["budget"]
    campaign = await row(
        conn,
        "SELECT id,limit_usd,halted FROM model_campaigns WHERE id=%s",
        (UUID(saved["campaign_id"]),),
    )
    if campaign is None or campaign["limit_usd"] != Decimal(saved["limit_usd"]):
        raise ConsoleError("The saved campaign or its original ceiling is unavailable.")
    prior = await rows(
        conn, "SELECT DISTINCT campaign_id FROM model_spend_attempts WHERE run_id=%s", (run["id"],)
    )
    if any(str(p["campaign_id"]) != saved["campaign_id"] for p in prior):
        raise ConsoleError("The saved campaign does not match this run's cost history.")
    return manifest, campaign


async def campaign_totals(conn, campaign_id):
    # Keep the full uncertain reservation. A breach also retains whichever
    # amount is larger, consistent with the provider reservation ledger.
    return await row(
        conn,
        """SELECT COALESCE(sum(reported_usd),0) AS spent,
        COALESCE(sum(CASE WHEN status='reported' THEN 0
            ELSE GREATEST(reserved_usd-COALESCE(reported_usd,0),0) END),0) AS reserved
        FROM model_spend_attempts WHERE campaign_id=%s""",
        (campaign_id,),
    )


async def next_intervention_window(conn, run):
    if run["status"] in TERMINAL_RUNS:
        return None
    prepared = await row(
        conn,
        'SELECT id FROM run_jobs WHERE run_id=%s AND "window"=%s LIMIT 1',
        (run["id"], run["current_window"]),
    )
    boundary = await row(
        conn,
        "SELECT run_id FROM run_boundaries WHERE run_id=%s AND window_index=%s",
        (run["id"], run["current_window"]),
    )
    window = run["current_window"] + int(
        run["phase"] != "actors" or bool(prepared) or bool(boundary)
    )
    return window if window < run["max_windows"] else None


def intervention_record(value):
    return {
        key: value[key]
        for key in (
            "id",
            "request_id",
            "owner_id",
            "target",
            "prompt",
            "effective_window",
            "status",
            "created_at",
            "applied_at",
        )
    }


async def apply_interventions(conn, run, window):
    """Called under the run row lock, before any Actor input is saved."""
    pending = await rows(
        conn,
        "SELECT * FROM console_interventions WHERE run_id=%s AND effective_window=%s "
        "AND status='pending' ORDER BY created_at,id FOR UPDATE",
        (run["id"], window),
    )
    for item in pending:
        payload = {
            "source": "console",
            "intervention_id": str(item["id"]),
            "target": item["target"],
            "prompt": item["prompt"],
        }
        if item["target"] == "actor":
            await emit(
                conn,
                run["id"],
                "actor_cue",
                window,
                {**payload, "cue": item["prompt"]},
                f"console-cue:{item['id']}",
                item["owner_id"],
            )
        else:
            message = await save_message(
                conn, run["id"], item["owner_id"], "owner", item["prompt"], item["id"], window
            )
            payload["message_id"] = str(message["id"])
        await emit(
            conn,
            run["id"],
            "console_intervention_applied",
            window,
            payload,
            f"console-intervention:{item['id']}",
            item["owner_id"],
        )
        await conn.execute(
            "UPDATE console_interventions SET status='applied',applied_at=clock_timestamp() "
            "WHERE id=%s",
            (item["id"],),
        )


async def recover_stale(conn, run_id):
    stale = await row(
        conn,
        "SELECT id FROM console_executions WHERE run_id=%s AND status IN ('queued','running') "
        "AND heartbeat_at<clock_timestamp()-interval '60 seconds'",
        (run_id,),
    )
    if stale is None:
        return
    # A surviving worker retains its session lock across an API restart.
    acquired = await row(conn, "SELECT pg_try_advisory_xact_lock(%s) AS ok", (lock_key(run_id),))
    if acquired["ok"]:
        await conn.execute(
            "UPDATE console_executions SET status='interrupted',finished_at=clock_timestamp(),"
            "error='The worker stopped. Saved work can be resumed.' WHERE id=%s",
            (stale["id"],),
        )


async def execution_state(pool, root, run_id):
    async with pool.connection() as conn, conn.transaction():
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,))
        if run is None:
            raise HTTPException(404, "Run not found.")
        await recover_stale(conn, run_id)
        operation = await row(
            conn,
            "SELECT * FROM console_executions WHERE run_id=%s "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (run_id,),
        )
        next_window = await next_intervention_window(conn, run)
        reason, budget = None, None
        try:
            _, campaign = await checked_manifest(conn, root, run)
            totals = await campaign_totals(conn, campaign["id"])
            available = max(
                Decimal(0), campaign["limit_usd"] - totals["spent"] - totals["reserved"]
            )
            budget = {
                "campaign_id": campaign["id"],
                "limit_usd": str(campaign["limit_usd"]),
                "spent_usd": str(totals["spent"]),
                "reserved_usd": str(totals["reserved"]),
                "available_usd": str(available),
            }
            if campaign["halted"] or available <= 0:
                reason = "The campaign has no available model budget."
        except ConsoleError as error:
            reason = str(error)
        active = operation is not None and operation["status"] in ACTIVE
        external_active = not active and await external_worker_active(conn, run_id)
        if run["status"] in TERMINAL_RUNS:
            reason = "This run has ended. Create a branch from a saved turn to continue."
        if active or external_active:
            reason = "A worker is active for this run."
        if is_clocked(run):
            reason = CLOCKED_REFUSAL
        interventions = await rows(
            conn,
            "SELECT * FROM console_interventions WHERE run_id=%s "
            "ORDER BY created_at DESC,id DESC LIMIT 100",
            (run_id,),
        )
    public_operation = (
        None
        if operation is None
        else {
            key: operation[key]
            for key in (
                "id",
                "request_id",
                "mode",
                "status",
                "pause_requested",
                "created_at",
                "started_at",
                "finished_at",
                "error",
            )
        }
    )
    return {
        "run_id": run_id,
        "status": run["status"],
        "phase": run["phase"],
        "current_window": run["current_window"],
        "max_windows": run["max_windows"],
        "execution": public_operation,
        "capabilities": {
            "can_execute": reason is None,
            "can_pause": (active or run["status"] == "running")
            and not bool(active and operation["pause_requested"]),
            "can_intervene": next_window is not None,
            "reason": reason,
        },
        "next_intervention_window": next_window,
        "budget": budget,
        "interventions": [intervention_record(item) for item in interventions],
    }


def service(request):
    return request.app.state.pool, Path(getattr(request.app.state, "console_root", ROOT))


@router.get("/runs/{run_id}/execution")
async def get_execution(run_id: UUID, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    pool, root = service(request)
    return await execution_state(pool, root, run_id)


@router.post("/runs/{run_id}/execution", status_code=202)
async def start_execution(
    run_id: UUID, body: ExecutionRequest, request: Request, principal: OPERATOR_AUTH
):
    require_admin(principal)
    pool, root = service(request)
    payload_hash = fingerprint({"mode": body.mode})
    launch = None
    async with pool.connection() as conn, conn.transaction():
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,))
        if run is None:
            raise HTTPException(404, "Run not found.")
        if is_clocked(run):
            raise HTTPException(409, CLOCKED_REFUSAL)
        await recover_stale(conn, run_id)
        existing = await row(
            conn,
            "SELECT * FROM console_executions WHERE run_id=%s AND request_id=%s",
            (run_id, body.request_id),
        )
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(409, "This request ID has different execution settings.")
        else:
            if run["status"] in TERMINAL_RUNS:
                raise HTTPException(409, "This run has ended. Branch from a saved turn first.")
            active = await row(
                conn,
                "SELECT id FROM console_executions WHERE run_id=%s "
                "AND status IN ('queued','running')",
                (run_id,),
            )
            if active:
                raise HTTPException(409, "A worker is already active for this run.")
            if await external_worker_active(conn, run_id):
                raise HTTPException(409, "A scenario worker is already active for this run.")
            try:
                _, campaign = await checked_manifest(conn, root, run)
                totals = await campaign_totals(conn, campaign["id"])
                if (
                    campaign["halted"]
                    or totals["spent"] + totals["reserved"] >= campaign["limit_usd"]
                ):
                    raise ConsoleError("The campaign has no available model budget.")
            except ConsoleError as error:
                raise HTTPException(409, str(error)) from None
            launch = uuid4()
            await conn.execute(
                "INSERT INTO console_executions(id,run_id,request_id,mode,payload_hash) "
                "VALUES(%s,%s,%s,%s,%s)",
                (launch, run_id, body.request_id, body.mode, payload_hash),
            )
    if launch:
        supervisor = getattr(request.app.state, "console_supervisor", None)
        if supervisor is None:
            supervisor = request.app.state.console_supervisor = ExecutionSupervisor(
                pool, root, request.app.state.settings
            )
        await supervisor.launch(launch, run_id)
    return await execution_state(pool, root, run_id)


@router.post("/runs/{run_id}/execution/pause")
async def pause_execution(
    run_id: UUID, body: Operation, request: Request, principal: OPERATOR_AUTH
):
    require_admin(principal)
    pool, root = service(request)
    async with pool.connection() as conn, conn.transaction():
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,))
        if run is None:
            raise HTTPException(404, "Run not found.")
        prior = await row(
            conn,
            "SELECT request_id FROM console_pause_requests WHERE run_id=%s AND request_id=%s",
            (run_id, body.request_id),
        )
        if not prior:
            if run["status"] in TERMINAL_RUNS:
                raise HTTPException(409, "This run has ended.")
            await conn.execute(
                "INSERT INTO console_pause_requests(run_id,request_id) VALUES(%s,%s)",
                (run_id, body.request_id),
            )
            await conn.execute(
                "UPDATE environment_runs SET status='paused',result=%s WHERE id=%s",
                (Jsonb({"reason": "operator_pause"}), run_id),
            )
            await conn.execute(
                """UPDATE console_executions SET pause_requested=true,
                status=CASE WHEN status='queued' THEN 'paused' ELSE status END,
                finished_at=CASE WHEN status='queued' THEN clock_timestamp() ELSE finished_at END
                WHERE run_id=%s AND status IN ('queued','running')""",
                (run_id,),
            )
    return await execution_state(pool, root, run_id)


@router.post("/runs/{run_id}/interventions", status_code=201)
async def queue_intervention(
    run_id: UUID,
    body: InterventionRequest,
    request: Request,
    response: Response,
    principal: OPERATOR_AUTH,
):
    require_admin(principal)
    pool, _ = service(request)
    payload_hash = fingerprint(body.model_dump(mode="json", exclude={"request_id"}))
    async with pool.connection() as conn, conn.transaction():
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,))
        if run is None:
            raise HTTPException(404, "Run not found.")
        prior = await row(
            conn,
            "SELECT * FROM console_interventions WHERE run_id=%s AND request_id=%s",
            (run_id, body.request_id),
        )
        if prior:
            if prior["payload_hash"] != payload_hash:
                raise HTTPException(409, "This request ID has a different prompt or target.")
            response.status_code = 200
            return intervention_record(prior)
        if run["current_window"] != body.expected_window:
            raise HTTPException(
                409, "The run advanced. Refresh the turn before sending this prompt."
            )
        window = await next_intervention_window(conn, run)
        if window is None:
            raise HTTPException(409, "There is no unopened turn. Branch from a saved turn first.")
        participant = await row(
            conn,
            "SELECT owner_id FROM run_participants WHERE run_id=%s AND owner_id=%s",
            (run_id, body.owner_id),
        )
        if participant is None:
            raise HTTPException(404, "Run participant not found.")
        item = await row(
            conn,
            """INSERT INTO console_interventions
            (id,run_id,request_id,owner_id,target,prompt,expected_window,effective_window,payload_hash)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                uuid4(),
                run_id,
                body.request_id,
                body.owner_id,
                body.target,
                body.prompt,
                body.expected_window,
                window,
                payload_hash,
            ),
        )
    return intervention_record(item)


class ExecutionSupervisor:
    """Launch fixed local workers. Persisted state survives an API restart."""

    def __init__(self, pool, root=ROOT, settings=None):
        self.pool, self.root, self.settings = pool, Path(root), settings
        self.processes = {}
        self.tasks = set()

    async def launch(self, operation_id, run_id):
        directory = self.root / ".local/scenario-runs" / str(run_id)
        path = directory / f"console-{operation_id}.log"
        env = {**os.environ, "AGENTRPG_CONSOLE_ROOT": str(self.root)}
        if self.settings:
            env["DATABASE_URL"] = self.settings.database_url.get_secret_value()
            env["ADMIN_TOKEN"] = self.settings.admin_token.get_secret_value()
        process = None
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "agentrpg.console_execution",
                    "--worker",
                    str(operation_id),
                    cwd=self.root,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stream,
                    stderr=stream,
                )
            self.processes[operation_id] = process
            # Register the process before any database await. A failed PID write
            # must not leave a running child outside lifecycle supervision.
            task = asyncio.create_task(self._watch(operation_id, process))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE console_executions SET pid=%s WHERE id=%s", (process.pid, operation_id)
                )
        except Exception:
            if process is not None:
                # The worker also records its PID and durable status itself.
                # Keep its watcher when only this supplementary write failed.
                return
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE console_executions SET status='failed',finished_at=clock_timestamp(),"
                    "error='The local worker could not start.' WHERE id=%s "
                    "AND status IN ('queued','running')",
                    (operation_id,),
                )

    async def _watch(self, operation_id, process):
        await process.wait()
        self.processes.pop(operation_id, None)
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE console_executions SET status='interrupted',finished_at=clock_timestamp(),"
                "error='The worker stopped. Saved work can be resumed.' WHERE id=%s "
                "AND status IN ('queued','running')",
                (operation_id,),
            )

    async def close(self):
        # Only terminate process handles created by this supervisor, never a
        # stored PID which may now identify a different operating-system process.
        for process in list(self.processes.values()):
            with suppress(ProcessLookupError):
                process.terminate()
        if self.tasks:
            completion = asyncio.gather(*list(self.tasks), return_exceptions=True)
            try:
                await asyncio.wait_for(asyncio.shield(completion), 5)
            except TimeoutError:
                for process in list(self.processes.values()):
                    with suppress(ProcessLookupError):
                        process.kill()
                await asyncio.wait_for(completion, 5)


async def worker(operation_id, *, root=ROOT, execute=None):
    """One OS process owns this run until its bounded invocation ends."""
    env = {**dotenv_values(Path(root) / ".env"), **os.environ}
    async with AsyncConnectionPool(
        env["DATABASE_URL"], min_size=1, max_size=4, kwargs={"row_factory": dict_row}
    ) as pool:
        async with pool.connection() as lease:
            operation = await row(
                lease, "SELECT * FROM console_executions WHERE id=%s", (operation_id,)
            )
            if operation is None or operation["status"] != "queued":
                return
            acquired = await row(
                lease, "SELECT pg_try_advisory_lock(%s) AS ok", (lock_key(operation["run_id"]),)
            )
            await lease.commit()
            if not acquired["ok"]:
                raise ConsoleError("This run already has an active console worker.")
            heartbeat = None
            try:
                async with pool.connection() as conn, conn.transaction():
                    run = await row(
                        conn,
                        "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE",
                        (operation["run_id"],),
                    )
                    operation = await row(
                        conn,
                        "SELECT * FROM console_executions WHERE id=%s FOR UPDATE",
                        (operation_id,),
                    )
                    if operation["status"] != "queued" or operation["pause_requested"]:
                        return
                    await checked_manifest(conn, root, run)
                    await conn.execute(
                        "UPDATE console_executions SET status='running',"
                        "started_at=clock_timestamp(),"
                        "heartbeat_at=clock_timestamp(),pid=%s WHERE id=%s",
                        (os.getpid(), operation_id),
                    )

                async def pulse():
                    while True:
                        await asyncio.sleep(5)
                        async with pool.connection() as conn:
                            await conn.execute(
                                "UPDATE console_executions SET heartbeat_at=clock_timestamp() "
                                "WHERE id=%s",
                                (operation_id,),
                            )

                heartbeat = asyncio.create_task(pulse())
                spec = importlib.util.spec_from_file_location(
                    "console_scenario_runner", ROOT / "scripts/run_scenario.py"
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                args = [
                    "--resume",
                    str(Path(root) / ".local/scenario-runs" / str(run["id"]) / "credentials.json"),
                    "--console-operation",
                    str(operation_id),
                ]
                if operation["mode"] == "step":
                    args += ["--windows", "1"]
                report = await (execute or module.execute)(
                    module.parse_args(args), root=Path(root), env=env
                )
                status = "paused" if report["status"] == "paused" else "completed"
                error = None
            except Exception:
                status, error = (
                    "failed",
                    "Execution failed. Saved inputs, outputs, and cost records were retained.",
                )
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
                if "status" in locals():
                    async with pool.connection() as conn:
                        await conn.execute(
                            "UPDATE console_executions SET status=%s,error=%s,"
                            "finished_at=clock_timestamp(),heartbeat_at=clock_timestamp() "
                            "WHERE id=%s",
                            (status, error, operation_id),
                        )
                await lease.execute(
                    "SELECT pg_advisory_unlock(%s)", (lock_key(operation["run_id"]),)
                )
                await lease.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=UUID, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(worker(args.worker, root=Path(os.environ.get("AGENTRPG_CONSOLE_ROOT", ROOT))))
    except Exception:
        raise SystemExit(
            "The console worker stopped. Inspect the saved execution status."
        ) from None


if __name__ == "__main__":
    main()
