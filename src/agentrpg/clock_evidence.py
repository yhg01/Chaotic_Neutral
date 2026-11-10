"""Export elapsed-world evidence before cleanup; original dialogue is preserved."""

import hashlib
import json
from functools import reduce
from pathlib import Path
from typing import get_args, get_type_hints
from uuid import UUID, uuid5

from deepagents.middleware.filesystem import FilesystemState
from langchain_core.messages import messages_to_dict
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.message import add_messages
from psycopg import sql

from agentrpg.console_execution import save_private
from agentrpg.director import row, rows
from agentrpg.director_clock import DirectorClock


async def export_clock_evidence(pool, run_id, directory):
    run_id = UUID(str(run_id))
    directory = Path(directory)
    async with pool.connection() as conn:
        clock = await row(
            conn, "SELECT * FROM simulation_clocks WHERE manager_run_id=%s", (run_id,)
        )
        if not clock:
            raise ValueError("Run has no elapsed Clock.")
        tables = {}
        run_tables = [
            "confirmation_tasks",
            "confirmation_operations",
            "clock_runtime_actor_rejections",
            "run_events",
            "run_participants",
            "owner_messages",
            "director_clock_jobs",
            "clock_runtime_profiles",
            "clock_runtime_activations",
            "clock_runtime_tools",
            "clock_runtime_plans",
            "clock_transfer_operations",
            "clock_transfer_calls",
            "transfer_grants",
            "transfer_tasks",
            "transfer_task_operations",
            "credit_transfers",
            "world_requests",
            "world_accounts",
            "world_ledger",
            "inbox_messages",
            "inbox_receipts",
        ]
        for name, column, value in [
            *((n, "run_id", run_id) for n in run_tables),
            ("environment_runs", "id", run_id),
            *(
                (n, "clock_id", clock["id"])
                for n in [
                    "simulation_clock_processes",
                    "simulation_clock_events",
                    "simulation_clock_operations",
                    "director_clock_deliveries",
                ]
            ),
        ]:
            tables[name] = await rows(
                conn,
                sql.SQL("SELECT * FROM {} WHERE {}=%s").format(
                    sql.Identifier(name), sql.Identifier(column)
                ),
                (value,),
            )
        tables["simulation_clocks"] = [clock]
        tables["clock_graph_steps"] = await rows(
            conn,
            "SELECT s.* FROM clock_graph_steps s JOIN "
            "clock_runtime_activations a ON a.id=s.task_id WHERE a.run_id=%s",
            (run_id,),
        )
        tables["clock_runtime_actor_outputs"] = await rows(
            conn,
            "SELECT o.* FROM clock_runtime_actor_outputs o JOIN "
            "director_clock_jobs j ON j.id=o.job_id WHERE j.run_id=%s",
            (run_id,),
        )
        tables["director_clock_recovery"] = await rows(
            conn,
            "SELECT r.* FROM director_clock_recovery r JOIN "
            "director_clock_jobs j ON j.id=r.job_id WHERE j.run_id=%s",
            (run_id,),
        )
        cluster_id = tables["environment_runs"][0]["cluster_id"]
        for name in ("messages", "message_receipts"):
            tables[name] = await rows(
                conn,
                sql.SQL("SELECT * FROM {} WHERE cluster_id=%s").format(sql.Identifier(name)),
                (cluster_id,),
            )
        threads = [
            f"agentrpg:{cluster_id}:{p['agent_id']}:{p['session_id']}"
            for p in tables["run_participants"]
        ]
        checkpoint_usage = await row(
            conn,
            "SELECT count(*) AS versions,COALESCE(sum(octet_length(blob)),0) "
            "AS bytes FROM checkpoint_blobs WHERE thread_id=ANY(%s)",
            (threads,),
        )
    latest = {}
    saver = AsyncPostgresSaver(pool)
    files_template = next(
        (
            value
            for value in get_args(get_type_hints(FilesystemState, include_extras=True)["files"])
            if isinstance(value, DeltaChannel)
        ),
        None,
    )
    for thread in threads:
        saved = await saver.aget_tuple({"configurable": {"thread_id": thread}})
        if saved:
            values = dict(saved.checkpoint["channel_values"])
            # Current LangGraph message channels store deltas between snapshots.
            # An absent inline value does not mean the session has no messages.
            history = await saver.aget_delta_channel_history(
                config=saved.config,
                channels=["messages", "files"] if files_template else ["messages"],
            )
            channel = DeltaChannel(
                lambda state, writes: reduce(add_messages, writes, state), list
            ).from_checkpoint(history["messages"].get("seed", []))
            channel.replay_writes(history["messages"]["writes"])
            values["messages"] = messages_to_dict(channel.get())
            if files_template:
                files = files_template.from_checkpoint(history["files"].get("seed", {}))
                files.replay_writes(history["files"]["writes"])
                values["files"] = files.get()
            latest[thread] = {"config": saved.config, "values": values}
    snapshot = await DirectorClock(pool).clock.snapshot(clock["id"])
    if snapshot["revision"] != clock["revision"]:
        raise ValueError("The Clock changed during export; stop the conductor and export again.")
    balances = {a["owner_id"]: a["balance"] for a in tables["world_accounts"]}
    initial = tables["environment_runs"][0]["scenario"]["world"]["balances"]
    expected = dict(initial)
    for transfer in tables["credit_transfers"]:
        expected[transfer["sender_owner_id"]] -= transfer["amount"]
        expected[transfer["recipient_owner_id"]] += transfer["amount"]
    delivered = [e for e in tables["simulation_clock_events"] if e["status"] == "delivered"]
    delivered.sort(key=lambda e: e["delivered_revision"])
    order = [(e["at_seconds"], e["priority"], e["event_key"]) for e in delivered]
    summary = {
        "run_id": str(run_id),
        "clock_id": str(clock["id"]),
        "simulated_seconds": snapshot["now_seconds"],
        "clock_status": snapshot["status"],
        "task_success": None,
        "initial_balances": initial,
        "final_balances": balances,
        "native_payments": len(tables["credit_transfers"]),
        "native_board_messages": len(tables["messages"]),
        "native_agent_mail": sum(m["sender_source"] == "agent" for m in tables["inbox_messages"]),
        "confirmation_tasks": [
            {
                k: task[k]
                for k in (
                    "id",
                    "task_key",
                    "activation_event_key",
                    "owner_id",
                    "peer_owner_id",
                    "source_message_id",
                    "request_mail_id",
                    "question_message_id",
                    "decision",
                    "decision_message_id",
                    "reply_mail_id",
                    "reply_body_read",
                    "owner_report_id",
                    "peer_report_id",
                )
            }
            for task in tables["confirmation_tasks"]
        ],
        "financial_invariants": {
            "total_conserved": sum(balances.values()) == sum(initial.values()),
            "ledger_matches_balances": expected == balances,
            "no_duplicate_payment_grant": len(
                {t["authorization_id"] for t in tables["credit_transfers"]}
            )
            == len(tables["credit_transfers"]),
        },
        "delivered_events": len(delivered),
        "ordered_event_delivery": order == sorted(order),
        "director_jobs": len(tables["director_clock_jobs"]),
        "graph_nodes": len(tables["clock_graph_steps"]),
        "uncertain_jobs": sum(j["status"] == "uncertain" for j in tables["director_clock_jobs"]),
        "pending_events_at_cutoff": snapshot["pending_events"],
        "pending_events_truncated": snapshot.get("pending_events_truncated", False),
        "original_message_count": len(tables["owner_messages"]),
        "business_tasks_by_status": {
            status: sum(t["status"] == status for t in tables["transfer_tasks"])
            for status in ("active", "blocked", "completed", "cancelled")
        },
        "checkpoint_blob_versions": checkpoint_usage["versions"],
        "virtual_scratch_files": sum(len(s["values"].get("files") or {}) for s in latest.values()),
        "checkpoint_blob_bytes": checkpoint_usage["bytes"],
        "largest_saved_graph_input_chars": max(
            (len(json.dumps(s["input_messages"])) for s in tables["clock_graph_steps"]), default=0
        ),
        "largest_observed_model_input_tokens": max(
            (
                (m["data"].get("usage_metadata") or {}).get("input_tokens", 0)
                for state in latest.values()
                for m in state["values"]["messages"]
                if m["type"] == "ai"
            ),
            default=0,
        ),
    }
    save_private(
        directory / "trajectory.json",
        {"summary": summary, "tables": tables, "latest_graph_states": latest},
    )
    by_message = {
        str(j["owner_message_id"]): j
        for j in tables["director_clock_jobs"]
        if j["owner_message_id"]
    }
    by_request = {
        str(uuid5(b["namespace"], c["id"])): b["due_seconds"]
        for b in tables["clock_runtime_tools"]
        for c in b["calls"]
        if c["name"] == "send_owner_message"
    }
    lines = [
        "# Elapsed Actor and personal Agent trajectory",
        "",
        f"Run: `{run_id}`",
        "",
        "Original committed dialogue follows. Clock completion is separate from task success.",
        "",
    ]
    for message in sorted(tables["owner_messages"], key=lambda m: (m["created_at"], str(m["id"]))):
        job = by_message.get(str(message["id"]))
        seconds = job["applied_at_seconds"] if job else by_request.get(str(message["request_id"]))
        when = f"t={seconds}s" if seconds is not None else "unmapped time metadata"
        lines += [
            f"## {when} · {message['owner_id']} · {message['source']}",
            "",
            message["content"],
            "",
        ]
    path = directory / "trajectory.md"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    summary["trajectory_sha256"] = hashlib.sha256(
        (directory / "trajectory.json").read_bytes()
    ).hexdigest()
    save_private(directory / "summary.json", summary)
    return json.loads(json.dumps(summary, default=str))
