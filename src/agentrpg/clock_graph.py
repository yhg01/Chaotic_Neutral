"""One real Deep Agents node per durable Clock boundary; no alternate agent loop."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid5

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict
from psycopg.types.json import Jsonb

from agentrpg.activation import ActivationEnvelope, ActivationResult
from agentrpg.agent_model import local_model_calls
from agentrpg.deep_agent import (
    AgentBusyError,
    AgentIdentityError,
    AgentRuntimeError,
    _previous_activation,
    _unobserved_mail,
)
from agentrpg.director import row


class ClockGraph:
    """Stop before tools/model and persist each node's response before timing it.

    An interrupted model without a durable AI checkpoint is uncertain. It is never
    regenerated automatically. Tool retries retain the graph's exact call IDs.
    """

    def __init__(self, agent):
        self.agent = agent

    @asynccontextmanager
    async def locked(self, envelope):
        a = self.agent
        if (
            not isinstance(envelope, ActivationEnvelope)
            or a.environment is None
            or str(envelope.run_id) != a.environment.run_id
            or envelope.participant_id != a.agent_id
        ):
            raise AgentIdentityError("The elapsed activation does not match this Agent.")
        if len(envelope.prompt()) > 50000:
            raise AgentRuntimeError("The elapsed activation exceeds the native prompt bound.")
        if not a.runtime._open or a._active_task_id is not None:
            raise AgentBusyError("The elapsed Agent runtime is closed or busy.")
        a._active_task_id, a._activation_mode, a._elapsed_mode = envelope.activation_id, True, True
        try:
            async with asyncio.timeout(a.timeout_seconds):
                await a._verify_identity()
                async with a.runtime.lock_pool.connection() as lease:
                    got = await row(lease, "SELECT pg_try_advisory_lock(%s) AS ok", (a._lock_key,))
                    if not got["ok"]:
                        raise AgentBusyError("This Agent has a live graph worker.")
                    try:
                        await a._claim_task(envelope.prompt(), envelope.activation_id)
                        a.board.operation_namespace = uuid5(
                            a._session_namespace, str(envelope.activation_id)
                        )
                        a.environment.operation_namespace = a.board.operation_namespace
                        with local_model_calls():
                            yield
                    finally:
                        await lease.execute("SELECT pg_advisory_unlock(%s)", (a._lock_key,))
        finally:
            a._active_task_id, a._activation_mode, a._elapsed_mode = None, False, False

    async def _stage(self, snapshot):
        if snapshot.next and snapshot.next not in {("model",), ("tools",)}:
            await self.agent.graph.ainvoke(
                None, self.agent.config, interrupt_before=["model", "tools"], durability="sync"
            )
            snapshot = await self.agent.graph.aget_state(self.agent.config)
        return snapshot

    @staticmethod
    def boundary(snapshot):
        values = snapshot.values
        last = values.get("messages", [])[-1] if values.get("messages") else None
        phase = snapshot.next[0] if snapshot.next else "done"
        if phase not in {"model", "tools", "done"}:
            raise AgentRuntimeError("The graph stopped outside an elapsed node boundary.")
        result = values.get("activation_result")
        if phase == "done":
            if result is None:
                if not isinstance(last, AIMessage) or last.tool_calls:
                    raise AgentRuntimeError("The elapsed graph has no final activation result.")
                result = ActivationResult(status="responded", text=last.text).model_dump(
                    mode="json"
                )
            result = ActivationResult.model_validate(result).model_dump(mode="json")
        return {
            "phase": phase,
            "checkpoint_id": snapshot.config["configurable"]["checkpoint_id"],
            "tool_calls": last.tool_calls
            if phase == "tools" and isinstance(last, AIMessage)
            else [],
            "result": result if phase == "done" else None,
        }

    async def open(self, envelope):
        async with self.locked(envelope):
            a = self.agent
            snapshot = await a.graph.aget_state(a.config)
            if snapshot.values.get("active_task_id") != str(envelope.activation_id):
                await a.graph.ainvoke(
                    {
                        "messages": [
                            HumanMessage(content=envelope.prompt(), id=str(envelope.activation_id))
                        ],
                        "active_task_id": str(envelope.activation_id),
                        "activation_mode": True,
                        "activation_result": None,
                        "previous_activation": _previous_activation(snapshot.values)
                        if a.include_previous_activation_context
                        else None,
                        "activation_envelope": envelope.model_dump(mode="json"),
                        "notified_mail_ids": _unobserved_mail(
                            envelope.model_dump(mode="json"),
                            snapshot.values.get("messages", []),
                            snapshot.values.get("notified_mail_ids", []),
                        ),
                    },
                    a.config,
                    interrupt_before=["model", "tools"],
                    durability="sync",
                )
                snapshot = await a.graph.aget_state(a.config)
            return self.boundary(await self._stage(snapshot))

    async def advance(self, envelope, step_id, phase):
        step_id = UUID(str(step_id))
        async with self.locked(envelope):
            a, pool = self.agent, self.agent.runtime.pool
            async with pool.connection() as conn:
                prior = await row(
                    conn,
                    "SELECT * FROM clock_graph_steps WHERE session_key=%s AND step_id=%s",
                    (a.session_key, step_id),
                )
            if prior and (prior["phase"] != phase or prior["task_id"] != envelope.activation_id):
                raise AgentRuntimeError("The elapsed graph step identity has changed.")
            if prior and prior["status"] == "saved":
                return prior["result"]
            snapshot = await a.graph.aget_state(a.config)
            if prior:
                old = prior["input_messages"]
                current = messages_to_dict(snapshot.values.get("messages", []))
                added = current[len(old) :] if current[: len(old)] == old else []
                recovered = (phase == "model" and any(m["type"] == "ai" for m in added)) or (
                    phase == "tools"
                    and snapshot.next != ("tools",)
                    and any(m["type"] == "tool" for m in added)
                )
                if not recovered and phase == "model":
                    async with pool.connection() as conn:
                        await conn.execute(
                            "UPDATE clock_graph_steps SET status='uncertain' WHERE "
                            "session_key=%s AND step_id=%s",
                            (a.session_key, step_id),
                        )
                    raise AgentRuntimeError(
                        "Model outcome is uncertain; retain the original graph step."
                    )
            else:
                if snapshot.next != (phase,):
                    raise AgentRuntimeError("The requested elapsed graph node is not ready.")
                old = messages_to_dict(snapshot.values.get("messages", []))
                async with pool.connection() as conn:
                    task = await row(
                        conn,
                        "SELECT model_calls FROM agent_tasks WHERE session_key=%s AND task_id=%s",
                        (a.session_key, envelope.activation_id),
                    )
                    await conn.execute(
                        "INSERT INTO "
                        "clock_graph_steps(session_key,task_id,step_id,phase,checkpoint_id,input_messages,model_calls,status)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,'running')",
                        (
                            a.session_key,
                            envelope.activation_id,
                            step_id,
                            phase,
                            snapshot.config["configurable"]["checkpoint_id"],
                            Jsonb(old),
                            task["model_calls"],
                        ),
                    )
                recovered = False
            if not recovered:
                await a.graph.ainvoke(None, a.config, interrupt_after=[phase], durability="sync")
                snapshot = await a.graph.aget_state(a.config)
            current = messages_to_dict(snapshot.values.get("messages", []))
            added = current[len(old) :] if current[: len(old)] == old else []
            tokens = 0
            if phase == "model":
                ai = [m for m in added if m["type"] == "ai"]
                if len(ai) != 1:
                    raise AgentRuntimeError("No exact durable model response at this boundary.")
                tokens = (ai[0]["data"].get("usage_metadata") or {}).get("output_tokens")
                if type(tokens) is not int or tokens < 0:
                    raise AgentRuntimeError(
                        "Elapsed generation requires reported output-token usage."
                    )
            snapshot = await self._stage(snapshot)
            result = {**self.boundary(snapshot), "output_tokens": tokens}
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "UPDATE clock_graph_steps SET status='saved',result=%s WHERE "
                    "session_key=%s AND step_id=%s",
                    (Jsonb(result), a.session_key, step_id),
                )
                if result["phase"] == "done":
                    await conn.execute(
                        "UPDATE agent_tasks SET "
                        "status='completed',result=%s,completed_at=clock_timestamp() WHERE"
                        " session_key=%s AND task_id=%s",
                        (json.dumps(result["result"]), a.session_key, envelope.activation_id),
                    )
            return result
