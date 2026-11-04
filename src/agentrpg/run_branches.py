"""Complete turn boundaries and isolated child runs with durable assistant memory.

A boundary precedes the whole Actor/Agent/settlement cycle. Model spending and
provider credentials are outside snapshots. Only completed Agent state is copied.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import ToolMessage
from langgraph.channels import DeltaChannel
from langgraph.channels.binop import _DeltaSnapshot
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentrpg.agent_surface import LEGACY, session_hash
from agentrpg.director import DirectorError, emit, fingerprint, row, rows, save_message
from agentrpg.main import OPERATOR_AUTH, require_admin
from agentrpg.scenario_schema import ScenarioSpec

ROOT = Path(__file__).resolve().parents[2]
router = APIRouter(prefix="/api/v1/console", tags=["run branches"])
# Foreign-key order. Reports, real spending, credentials and live worker leases
# deliberately have no place in this list.
TABLES = (
    "console_interventions",
    "run_beats",
    "run_events",
    "owner_messages",
    "run_jobs",
    "event_deliveries",
    "messages",
    "message_receipts",
    "inbox_messages",
    "inbox_receipts",
    "world_state",
    "world_accounts",
    "world_assets",
    "world_reservations",
    "world_requests",
    "transfer_grants",
    "credit_transfers",
    "transfer_task_tracking",
    "transfer_tasks",
    "transfer_task_operations",
    "world_intents",
    "world_settlements",
    "world_ledger",
    "world_journal",
    "resource_state",
    "owner_resources",
    "resource_requests",
    "resource_journal",
    "market_permissions",
    "market_requests",
    "market_intents",
    "market_listings",
    "market_settlements",
    "market_ledger",
    "shop_profiles",
    "shop_tasks",
    "shop_carts",
    "shop_cart_lines",
    "shop_orders",
    "shop_order_lines",
    "shop_operations",
    "run_supervision",
    "run_task_health",
)
GLOBAL_IDS = (
    "console_interventions",
    "run_events",
    "owner_messages",
    "run_jobs",
    "messages",
    "inbox_messages",
    "world_ledger",
)
UUID_TEXT = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)


def session_key(cluster, participant):
    return f"agentrpg:{cluster}:{participant['agent_id']}:{participant['session_id']}"


def conductor_key(run_id):
    return int.from_bytes(
        hashlib.sha256(f"director:{run_id}".encode()).digest()[:8], "big", signed=True
    )


def remap(value, identities):
    """Map exact identities in records, message content and private scratch files."""
    if isinstance(value, UUID):
        return UUID(identities.get(str(value), str(value)))
    if isinstance(value, str):
        if value.startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except (TypeError, ValueError):
                pass
            else:
                mapped = remap(structured, identities)
                return value if mapped == structured else json.dumps(mapped, ensure_ascii=False)
        return UUID_TEXT.sub(lambda match: identities.get(match[0].lower(), match[0]), value)
    if isinstance(value, BaseModel):
        mapped = value.model_copy(
            update={key: remap(item, identities) for key, item in value.__dict__.items()}
        )
        if isinstance(mapped, ToolMessage) and mapped.name in {
            "list_threads",
            "read_thread",
            "list_mail",
        }:
            try:
                payload = json.loads(mapped.content)
            except (TypeError, ValueError):
                return mapped
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                data = payload["data"]
                if data.get("next_cursor"):
                    data["next_cursor"] = None
                    data["branch_pagination_reset_required"] = True
                    data["branch_pagination_note"] = "Start this query again from its first page."
                    mapped = mapped.model_copy(update={"content": json.dumps(payload)})
        return mapped
    if isinstance(value, dict):
        return {remap(key, identities): remap(item, identities) for key, item in value.items()}
    if isinstance(value, list):
        return [remap(item, identities) for item in value]
    if isinstance(value, tuple):
        mapped = tuple(remap(item, identities) for item in value)
        return type(value)(*mapped) if hasattr(value, "_fields") else mapped
    return value


def unavailable(snapshot):
    for owner, memory in snapshot["memory"].items():
        if memory.get("missing"):
            return f"The saved assistant memory for {owner} is incomplete. Restart from Turn 01."
    for table in ("world_requests", "resource_requests", "market_requests"):
        if any(record.get("request_payload") is None for record in snapshot["tables"][table]):
            return (
                "This boundary contains older request records without their input payloads. "
                "Restart from Turn 01."
            )
    return None


async def capture_boundary(pool, run_id, agents=None):
    """Called with the conductor lock held. Returns None within a prepared turn."""
    async with pool.connection() as conn, conn.transaction():
        # One database snapshot includes both domain rows and checkpoint blobs.
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s FOR UPDATE", (run_id,))
        if not run or run["phase"] != "actors" or run["current_window"] >= run["max_windows"]:
            return None
        window = run["current_window"]
        previous = await row(
            conn,
            "SELECT window_index FROM run_boundaries WHERE run_id=%s AND window_index=%s",
            (run_id, window),
        )
        if previous:
            return window
        if await row(
            conn,
            'SELECT id FROM run_jobs WHERE run_id=%s AND "window">=%s LIMIT 1',
            (run_id, window),
        ):
            return None
        members = await rows(
            conn, "SELECT * FROM run_participants WHERE run_id=%s ORDER BY owner_id", (run_id,)
        )
        keys = [session_key(run["cluster_id"], member) for member in members]
        if await row(
            conn,
            "SELECT task_id FROM agent_tasks WHERE session_key=ANY(%s) AND status='pending' "
            "LIMIT 1",
            (keys,),
        ):
            raise DirectorError(
                "A turn boundary cannot be saved while an assistant task is unfinished."
            )
        snapshot = {
            "version": 1,
            "run": json.loads(json.dumps(run, default=str)),
            "participants": json.loads(json.dumps(members, default=str)),
            "tables": {},
            "memory": {},
        }
        for table in TABLES:
            field, scope = (
                ("cluster_id", run["cluster_id"])
                if table in {"messages", "message_receipts"}
                else ("run_id", run_id)
            )
            query = sql.SQL("SELECT to_jsonb(t) AS record FROM {} t WHERE {}=%s").format(
                sql.Identifier(table), sql.Identifier(field)
            )
            snapshot["tables"][table] = [
                item["record"] for item in await rows(conn, query, (scope,))
            ]
        saver = AsyncPostgresSaver(conn)
        checkpoints_exist = (await row(conn, "SELECT to_regclass('checkpoints') AS name"))[
            "name"
        ] is not None
        for member, key in zip(members, keys, strict=True):
            tasks = await rows(
                conn,
                "SELECT to_jsonb(t) AS record FROM agent_tasks t WHERE session_key=%s ORDER BY "
                "created_at,task_id",
                (key,),
            )
            session = await row(
                conn, "SELECT instructions_hash FROM agent_sessions WHERE session_key=%s", (key,)
            )
            saved = (
                await saver.aget_tuple({"configurable": {"thread_id": key}})
                if checkpoints_exist
                else None
            )
            completed_jobs = [
                item
                for item in snapshot["tables"]["run_jobs"]
                if item["owner_id"] == member["owner_id"] and item["kind"] == "agent"
            ]
            memory = {
                "tasks": [item["record"] for item in tasks],
                "instructions_hash": session["instructions_hash"] if session else None,
                "checkpoint": None,
                "missing": bool(completed_jobs and saved is None),
            }
            if saved is not None:
                # An end-of-task receipt is required for every checkpoint copied.
                active = saved.checkpoint.get("channel_values", {}).get("active_task_id")
                if active and not any(str(item["record"]["task_id"]) == active for item in tasks):
                    raise DirectorError(
                        "Assistant state has no completed task receipt at this boundary."
                    )
                checkpoint = copy.deepcopy(saved.checkpoint)
                agent = (agents or {}).get(member["owner_id"])
                if agent is not None and hasattr(agent, "graph"):
                    resolved = await agent.graph.aget_state(saved.config)
                    if resolved.next:
                        raise DirectorError("Assistant graph work remains at this boundary.")
                    for name, channel in agent.graph.channels.items():
                        if isinstance(channel, DeltaChannel) and name in resolved.values:
                            checkpoint["channel_values"][name] = _DeltaSnapshot(
                                resolved.values[name]
                            )
                elif completed_jobs:
                    memory["missing"] = True
                kind, content = saver.serde.dumps_typed(checkpoint)
                memory["checkpoint"] = {
                    "type": kind,
                    "data": base64.b64encode(content).decode(),
                    "metadata": saved.metadata,
                }
            snapshot["memory"][member["owner_id"]] = memory
        await conn.execute(
            "INSERT INTO run_boundaries(run_id,window_index,snapshot,snapshot_hash) "
            "VALUES(%s,%s,%s,%s)",
            (run_id, window, Jsonb(snapshot), fingerprint(snapshot)),
        )
        return window


async def insert_record(conn, table, record):
    # Obtain types/defaults from the actual migrated table; generated sequence
    # and board-parent columns belong to the child's database inserts.
    fields = [
        item["column_name"]
        for item in await rows(
            conn,
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_schema=current_schema() AND table_name=%s AND is_generated='NEVER' AND "
            "is_identity='NO' ORDER BY ordinal_position",
            (table,),
        )
        if item["column_name"] in record
    ]
    columns = sql.SQL(",").join(map(sql.Identifier, fields))
    await conn.execute(
        sql.SQL("INSERT INTO {} ({}) SELECT {} FROM jsonb_populate_record(NULL::{},%s)").format(
            sql.Identifier(table), columns, columns, sql.Identifier(table)
        ),
        (Jsonb(record),),
    )


async def clone_snapshot(conn, snapshot, child_id, manifest):
    source = snapshot["run"]
    identities = {source["id"]: str(child_id), source["cluster_id"]: manifest["cluster_id"]}
    child_members = {member["owner_id"]: member for member in manifest["participants"]}
    for original in snapshot["participants"]:
        child = child_members[original["owner_id"]]
        for key in ("agent_id", "session_id"):
            identities[original[key]] = child[key]
    for table in GLOBAL_IDS:
        for record in snapshot["tables"][table]:
            identities[record["id"]] = str(uuid5(child_id, "record:" + record["id"]))
    for job in snapshot["tables"]["run_jobs"]:
        identities[job["id"]] = str(
            uuid5(child_id, f"{job['kind']}:{job['owner_id']}:{job['window']}")
        )
    # Keep derived request and resource identities consistent with child APIs.
    for job in snapshot["tables"]["run_jobs"]:
        old_job, new_job = UUID(job["id"]), UUID(identities[job["id"]])
        labels = ["consent", "commerce-consent", "transfer-consent", "finalreply"]
        labels += [
            f"{prefix}:{index}"
            for prefix in ("mail", "resource", "finalreply", "transfer-task", "shopping-task")
            for index in range(100)
        ]
        for label in labels:
            identities[str(uuid5(old_job, label))] = str(uuid5(new_job, label))
    for task in snapshot["tables"].get("transfer_tasks", []):
        identities[task["id"]] = str(
            uuid5(child_id, f"transfer-task:{task['owner_id']}:{task['task_key']}")
        )
    for task in snapshot["tables"].get("shop_tasks", []):
        identities[task["id"]] = str(
            uuid5(child_id, f"shop-task:{task['owner_id']}:{task['task_key']}")
        )
    for order in snapshot["tables"].get("shop_orders", []):
        identities[order["id"]] = str(
            uuid5(child_id, f"shop-order:{order['owner_id']}:{order['cart_version']}")
        )
    for resource in snapshot["tables"]["owner_resources"]:
        label = f"holding:{resource['owner_id']}:{resource['origin_id']}"
        if resource["resource_id"] == str(uuid5(UUID(source["id"]), label)):
            identities[resource["resource_id"]] = str(uuid5(child_id, label))
    for table, prefix in (("resource_requests", "document"), ("market_requests", "listing")):
        for record in snapshot["tables"][table]:
            old_label = f"{prefix}:{record['owner_id']}:{record['request_id']}"
            new_label = f"{prefix}:{record['owner_id']}:{remap(record['request_id'], identities)}"
            identities[str(uuid5(UUID(source["id"]), old_label))] = str(uuid5(child_id, new_label))
    for line in snapshot["tables"].get("shop_order_lines", []):
        identities[line["request_id"]] = str(
            uuid5(
                UUID(identities[line["order_id"]]),
                f"line:{remap(line['listing_id'], identities)}",
            )
        )
    # These deterministic identities are also returned on committed retries.
    for record in snapshot["tables"]["owner_messages"]:
        identities[record["id"]] = str(
            uuid5(
                child_id,
                f"message:{record['owner_id']}:{record['source']}:"
                f"{remap(record['request_id'], identities)}",
            )
        )
    for record in snapshot["tables"]["inbox_messages"]:
        identities[record["id"]] = str(
            uuid5(
                child_id, f"mail:{record['sender_key']}:{remap(record['request_id'], identities)}"
            )
        )
    # First resolve the non-event references, then derive event identities from
    # their operation keys. No saved operation key is allowed to name itself.
    for event in snapshot["tables"]["run_events"]:
        identities[event["id"]] = str(uuid5(child_id, remap(event["operation_key"], identities)))
    copied = remap(snapshot["tables"], identities)
    from agentrpg.world import _hash

    for table in TABLES:
        # Older boundaries predate credit transfers and have no transfer rows.
        records = copied.get(table, [])
        if table == "messages":
            records = sorted(records, key=lambda item: (item["parent_id"] is not None, item["seq"]))
        elif table == "inbox_messages":
            records = sorted(records, key=lambda item: item["sequence"])
        for record in records:
            if table == "run_jobs":
                record["input_hash"] = fingerprint(record["input"])
                record["lease_token"] = None
                record["lease_until"] = None
            elif table == "owner_messages":
                record["payload_hash"] = fingerprint({"content": record["content"]})
            elif table == "inbox_messages":
                record["payload_hash"] = fingerprint(
                    {
                        key: record[key]
                        for key in ("recipient_owner_id", "subject", "body", "in_reply_to")
                    }
                )
            elif table == "messages":
                payload = {
                    "operation": "thread" if record["parent_id"] is None else "reply",
                    "parent_id": record["parent_id"],
                    "title": record["title"],
                    "body": record["body"],
                }
                record["payload_hash"] = hashlib.sha256(
                    json.dumps(
                        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest()
            elif table in {"world_requests", "resource_requests", "market_requests"}:
                record["payload_hash"] = _hash(record["request_payload"])
            elif table in {"transfer_task_operations", "shop_operations"}:
                record["payload_hash"] = fingerprint(record["request_payload"])
            elif table == "console_interventions":
                record["payload_hash"] = fingerprint(
                    {
                        key: record[key]
                        for key in ("owner_id", "target", "prompt", "expected_window")
                    }
                )
            elif table == "resource_journal":
                # UUID remapping can change the canonical order used by the
                # live Resource snapshot. Keep journal replay identical.
                payload = record["payload"]
                payload["resources"].sort(key=lambda item: item["id"])
                payload["permissions"].sort(key=lambda item: item["owner_id"])
                payload["listings"].sort(key=lambda item: item["id"])
                payload["ledger"].sort(
                    key=lambda item: (item["window_index"], item["owner_id"], item["request_id"])
                )
            await insert_record(conn, table, record)
    saver = AsyncPostgresSaver(conn)
    instructions = manifest["configuration"]["instructions"]
    runtime_policy = {
        "instructions": instructions,
        **{
            key: True
            for key in (
                "require_explicit_finish",
                "guard_empty_scratch",
                "include_previous_activation_context",
                "track_transfer_tasks",
                "guard_transfer_task_yield",
            )
            if manifest["configuration"].get(key, False)
        },
    }
    surface = manifest["configuration"].get("agent_surface", LEGACY)
    for original in snapshot["participants"]:
        owner_policy = dict(runtime_policy)
        owner_policy["instructions"] = (
            manifest["configuration"]
            .get("instructions_by_owner", {})
            .get(original["owner_id"], instructions)
        )
        if original["owner_id"] in manifest["configuration"].get("browser_shop_owners", []):
            from agentrpg.shop_browser import ShopBrowser

            owner_policy["browser_shop_profile"] = ShopBrowser.profile
        memory = snapshot["memory"][original["owner_id"]]
        member = child_members[original["owner_id"]]
        old_hash = session_hash(owner_policy, source["id"], surface)
        if memory["instructions_hash"] and memory["instructions_hash"] != old_hash:
            raise DirectorError("Saved assistant instructions differ from the run configuration.")
        if not memory["checkpoint"]:
            continue
        key = session_key(manifest["cluster_id"], member)
        new_hash = session_hash(owner_policy, str(child_id), surface)
        await conn.execute(
            "INSERT INTO "
            "agent_sessions(session_key,cluster_id,agent_id,session_id,instructions_hash) "
            "VALUES(%s,%s,%s,%s,%s)",
            (key, manifest["cluster_id"], member["agent_id"], member["session_id"], new_hash),
        )
        for task in memory["tasks"]:
            task = remap(task, identities)
            task["session_key"] = key
            job = next(
                (
                    item
                    for item in copied["run_jobs"]
                    if item["id"] == task["task_id"] and item["kind"] == "agent"
                ),
                None,
            )
            if job is None:
                raise DirectorError(
                    "The saved assistant task is outside this simulation's turn history."
                )
            from agentrpg.activation import ActivationEnvelope

            task["prompt_hash"] = hashlib.sha256(
                ActivationEnvelope.model_validate(job["input"]).prompt(surface).encode()
            ).hexdigest()
            await insert_record(conn, "agent_tasks", task)
        saved = memory["checkpoint"]
        checkpoint = remap(
            saver.serde.loads_typed((saved["type"], base64.b64decode(saved["data"]))), identities
        )
        checkpoint["id"] = empty_checkpoint()["id"]
        checkpoint["channel_values"]["active_task_id"] = ""
        checkpoint["channel_values"]["activation_result"] = None
        checkpoint["channel_values"]["activation_mode"] = False
        await saver.aput(
            {"configurable": {"thread_id": key, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "update", "step": saved["metadata"].get("step", -1), "parents": {}},
            checkpoint["channel_versions"],
        )


class BranchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    window: int = Field(ge=0, le=99)
    owner_id: str | None = Field(default=None, max_length=80)
    target: Literal["actor", "agent"] = "actor"
    prompt: str = Field(default="", max_length=12000)

    @field_validator("prompt")
    @classmethod
    def trim(cls, value):
        return value.strip()


async def create_branch(pool, root, run_id, payload):
    from agentrpg.console_execution import checked_manifest, save_private
    from agentrpg.inbox import InboxService
    from agentrpg.resources import ResourceService
    from agentrpg.world import WorldService

    digest = fingerprint(payload.model_dump(mode="json"))
    # Loading this trusted file never sends its Agent credentials to a browser.
    child_id = uuid5(run_id, f"branch:{payload.request_id}")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (conductor_key(child_id),))
        previous = await row(
            conn,
            "SELECT * FROM run_branches WHERE parent_run_id=%s AND request_id=%s",
            (run_id, payload.request_id),
        )
        if previous:
            if previous["payload_hash"] != digest:
                raise HTTPException(409, "This branch request ID has different content.")
            return {
                "run_id": str(previous["child_run_id"]),
                "parent_run_id": str(run_id),
                "window": previous["window_index"],
                "source": previous["source"],
                "replayed": True,
            }
        source_run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s", (run_id,))
        if source_run is None:
            raise HTTPException(404, "Run not found.")
        parent_manifest, _ = await checked_manifest(conn, root, source_run)
        config = copy.deepcopy(parent_manifest["configuration"])
        if payload.window >= source_run["max_windows"]:
            raise HTTPException(422, "This turn is outside the frozen scenario.")
        saved = await row(
            conn,
            "SELECT * FROM run_boundaries WHERE run_id=%s AND window_index=%s",
            (run_id, payload.window),
        )
        snapshot = None
        if saved:
            snapshot = copy.deepcopy(saved["snapshot"])
            if saved["snapshot_hash"] != fingerprint(snapshot):
                raise HTTPException(409, "The saved boundary failed its integrity check.")
            if "run_supervision" not in snapshot["tables"] and await row(
                conn, "SELECT 1 FROM run_supervision WHERE run_id=%s", (run_id,)
            ):
                raise HTTPException(409, "This older boundary has no supervision state.")
            if "shop_profiles" not in snapshot["tables"] and await row(
                conn, "SELECT 1 FROM shop_profiles WHERE run_id=%s", (run_id,)
            ):
                raise HTTPException(
                    409,
                    "This older boundary has no storefront state. "
                    "Use a complete browser-shop checkpoint or restart the run.",
                )
            if "console_interventions" not in snapshot["tables"]:
                # Earlier version-1 snapshots predate the prompt queue copy.
                # Queue insertion, application and capture hold the same run
                # row lock. These timestamps therefore recover its exact state
                # at capture, without bringing later prompts into the branch.
                snapshot["tables"]["console_interventions"] = [
                    item["record"]
                    for item in await rows(
                        conn,
                        "SELECT to_jsonb(i) || CASE WHEN applied_at>%s THEN "
                        '\'{"status":"pending","applied_at":null}\'::jsonb ELSE \'{}\'::jsonb END '
                        "AS record FROM console_interventions i WHERE run_id=%s AND created_at<=%s",
                        (saved["created_at"], run_id, saved["created_at"]),
                    )
                ]
            if reason := unavailable(snapshot):
                if payload.window != 0:
                    raise HTTPException(409, reason)
                snapshot = None
        if payload.window != 0 and snapshot is None:
            raise HTTPException(
                409, "No complete checkpoint exists before this turn. Restart from Turn 01."
            )
        spec = ScenarioSpec.model_validate(source_run["scenario"])
        if spec.content_hash() != source_run["scenario_hash"]:
            raise HTTPException(409, "The frozen scenario failed its integrity check.")
        if payload.prompt and payload.owner_id not in {owner.id for owner in spec.owners}:
            raise HTTPException(422, "Select the owner who will receive the prompt.")
        cluster = uuid4()
        manifest = {
            "run_id": str(child_id),
            "cluster_id": str(cluster),
            "participants": [],
            "configuration": config,
        }
        await conn.execute(
            "INSERT INTO clusters(id,name) VALUES(%s,%s)", (cluster, f"Branch {child_id}")
        )
        await conn.execute(
            "INSERT INTO "
            "environment_runs(id,cluster_id,scenario,scenario_hash,status,current_window,phase,"
            "seed,max_windows) VALUES(%s,%s,%s,%s,'pending',%s,'actors',%s,%s)",
            (
                child_id,
                cluster,
                Jsonb(source_run["scenario"]),
                source_run["scenario_hash"],
                payload.window,
                source_run["seed"],
                source_run["max_windows"],
            ),
        )
        for owner in spec.owners:
            member = {
                "owner_id": owner.id,
                "agent_id": str(uuid4()),
                "session_id": str(uuid4()),
                "token": secrets.token_urlsafe(32),
            }
            manifest["participants"].append(member)
            await conn.execute(
                "INSERT INTO agents(id,cluster_id,name,token_hash) VALUES(%s,%s,%s,%s)",
                (
                    member["agent_id"],
                    cluster,
                    f"Assistant for {owner.name[:60]}",
                    hashlib.sha256(member["token"].encode()).hexdigest(),
                ),
            )
            await conn.execute(
                "INSERT INTO run_participants(run_id,owner_id,agent_id,session_id) "
                "VALUES(%s,%s,%s,%s)",
                (child_id, owner.id, member["agent_id"], member["session_id"]),
            )
        source = "checkpoint" if snapshot else "initial"
        if snapshot:
            await clone_snapshot(conn, snapshot, child_id, manifest)
        else:
            await WorldService(pool).initialize(
                child_id, spec.model_dump(mode="json"), connection=conn
            )
            await ResourceService(pool).initialize(child_id, spec.resources, connection=conn)
            await InboxService(pool).initialize(child_id, spec.initial_mail, connection=conn)
            if manifest["configuration"].get("browser_shop_owners"):
                await conn.execute("INSERT INTO shop_profiles VALUES (%s)", (child_id,))
                await conn.execute(
                    "INSERT INTO shop_carts(run_id,owner_id) "
                    "SELECT run_id,owner_id FROM world_accounts WHERE run_id=%s",
                    (child_id,),
                )
        if not snapshot or payload.window == 0:
            await emit(conn, child_id, "run_started", 0, {}, "run_started")
        await conn.execute("UPDATE environment_runs SET status='paused' WHERE id=%s", (child_id,))
        branch_marker = uuid4()
        await emit(
            conn,
            child_id,
            "run_branched",
            payload.window,
            {"parent_run_id": str(run_id), "before_window": payload.window, "source": source},
            f"branch-created:{branch_marker}",
        )
        if snapshot:
            await emit(
                conn,
                child_id,
                "world_changed",
                payload.window,
                {
                    "kind": "branch_restored",
                    "text": "A saved turn boundary was restored into this independent run. "
                    "Saved pagination cursors have expired. Start each paged query from its first "
                    "page and use only the new cursors returned by this run. Prior completed "
                    "transactions remain recorded; do not repeat them as new actions.",
                },
                f"branch-memory-context:{branch_marker}",
            )
        if payload.prompt:
            await emit(
                conn,
                child_id,
                "operator_intervention",
                payload.window,
                {"target": payload.target, "prompt": payload.prompt, "source": "operator"},
                f"branch-prompt:{payload.request_id}",
                payload.owner_id,
            )
            if payload.target == "actor":
                await emit(
                    conn,
                    child_id,
                    "actor_cue",
                    payload.window,
                    {"cue": payload.prompt, "source": "operator"},
                    f"branch-cue:{payload.request_id}",
                    payload.owner_id,
                )
            else:
                await save_message(
                    conn,
                    child_id,
                    payload.owner_id,
                    "owner",
                    payload.prompt,
                    payload.request_id,
                    payload.window,
                )
        await conn.execute(
            "INSERT INTO "
            "run_branches(parent_run_id,request_id,payload_hash,child_run_id,window_index,source) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (run_id, payload.request_id, digest, child_id, payload.window, source),
        )
        # Save and fsync before commit. A crash cannot leave a visible child
        # without its credentials; an uncommitted orphan file is safe to replace.
        folder = root / ".local/scenario-runs" / str(child_id)
        save_private(folder / "credentials.json", manifest)
        save_private(folder / "scenario.json", source_run["scenario"])
        for directory in (folder, folder.parent):
            handle = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
    return {
        "run_id": str(child_id),
        "parent_run_id": str(run_id),
        "window": payload.window,
        "source": source,
        "replayed": False,
    }


@router.get("/runs/{run_id}/boundaries")
async def boundaries(run_id: UUID, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s", (run_id,))
        if run is None:
            raise HTTPException(404, "Run not found.")
        saved = {
            item["window_index"]: item
            for item in await rows(conn, "SELECT * FROM run_boundaries WHERE run_id=%s", (run_id,))
        }
        parent = await row(
            conn,
            "SELECT parent_run_id,window_index FROM run_branches WHERE child_run_id=%s",
            (run_id,),
        )
    items = []
    for window in range(min(run["max_windows"], run["current_window"] + 1)):
        reason = (
            unavailable(saved[window]["snapshot"])
            if window in saved
            else "No complete checkpoint was saved before this turn."
        )
        available = window == 0 or (window in saved and not reason)
        items.append(
            {
                "window": window,
                "available": available,
                "source": "checkpoint"
                if window in saved and not reason
                else "initial"
                if window == 0
                else None,
                "reason": None if available else reason,
            }
        )
    return {
        "run_id": str(run_id),
        "items": items,
        "parent_run_id": str(parent["parent_run_id"]) if parent else None,
        "parent_window": parent["window_index"] if parent else None,
    }


@router.post("/runs/{run_id}/branches")
async def branch(run_id: UUID, body: BranchInput, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    try:
        return await create_branch(
            request.app.state.pool, getattr(request.app.state, "console_root", ROOT), run_id, body
        )
    except (DirectorError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None
