"""Free seven-day native Clock/graph control; supplied choices are not model evidence."""

import json
import runpy
from argparse import Namespace
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError
from test_actor import Responses
from test_clock_runtime import keep_control, tool
from test_confirmations import DroppedReceipt, action_output, contract_view
from test_deep_agent import ScriptedModel

from agentrpg.actor import (
    ActorContractError,
    ActorOutput,
    ModelActor,
    ModelGenerationError,
    _candidate_schema,
)
from agentrpg.agent_model import UsageTracker
from agentrpg.agent_tools import BoardTools
from agentrpg.budget import BudgetExceeded
from agentrpg.clock_actor_recovery import recover_confirmation_schema
from agentrpg.clock_runtime import ClockRuntime
from agentrpg.clock_transfers import TimedTransfers
from agentrpg.community_experiment import install_community_events
from agentrpg.confirmation_control import CONFIRMATION_INSTRUCTIONS
from agentrpg.confirmation_daily import DAILY_CONFIRMATION_INSTRUCTIONS, DailyConfirmationPlan
from agentrpg.confirmations import required_actions, visible
from agentrpg.deep_agent import AgentBudgetError, AgentRuntime
from agentrpg.director import Director, row, rows
from agentrpg.director_clock import DirectorClock
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.simulation_clock import digest


class FreeOwner:
    def __init__(self):
        self.usage = UsageTracker()
        self.last_attempts = []

    async def respond(self, view):
        self.usage.output_tokens += 10
        needed = required_actions(view)
        tasks = [t for t in view.confirmations.get("tasks", []) if str(t["id"]) in needed]
        if not tasks:
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)
        return ActorOutput(
            kind="reply",
            source_event_id=view.source_event_id,
            text=" ".join(
                f"I explicitly request or confirm: {t['terms']} {t['condition']}" for t in tasks
            ),
            confirmation_actions=[
                {
                    "task_id": t["id"],
                    "kind": "request" if t["owner_id"] == view.owner_id else "confirm",
                }
                for t in tasks
            ],
        )


class RejectOnceOwner(FreeOwner):
    def __init__(self):
        super().__init__()
        self.rejected = False

    async def respond(self, view):
        if not self.rejected and required_actions(view):
            self.rejected = True
            self.usage.output_tokens += 10
            self.last_attempts = [
                {
                    "accepted": False,
                    "stage": "role_contract",
                    "review": None,
                    "generated_candidate": {
                        "text": "Please send the current request.",
                        "confirmation_actions": [],
                    },
                    "issues": [
                        {
                            "message": "Each current confirmation decision requires "
                            "exactly one typed owner action."
                        }
                    ],
                }
            ]
            raise ActorContractError("Known missing confirmation field.")
        return await super().respond(view)


class FreeAgent(ScriptedModel):
    pool: Any = None
    run: UUID | None = None
    owner: str = ""
    stop_after: int = 0

    async def _agenerate(self, messages, **kwargs):
        self.calls += 1
        if self.stop_after and self.calls > self.stop_after:
            raise BudgetExceeded("Supplied budget checkpoint; no provider request.")
        async with self.pool.connection() as conn:
            tasks = await visible(conn, self.run, self.owner)
            receipts = await rows(
                conn,
                "SELECT message_id FROM inbox_receipts WHERE run_id=%s AND owner_id=%s",
                (self.run, self.owner),
            )
            clock = await row(
                conn,
                "SELECT c.now_seconds FROM simulation_clocks c WHERE manager_run_id=%s",
                (self.run,),
            )
        acknowledged = {str(r["message_id"]) for r in receipts}
        response = None
        for t in tasks:
            if t["activation_event_key"]:
                day = int(t["task_key"].rsplit("-", 1)[1]) - 1
                assert clock["now_seconds"] >= day * 86400 + 43200
            args = {"task_id": str(t["id"])}
            if t["owner_id"] == self.owner:
                if not t["source_message_id"]:
                    continue
                if not t["request_mail_id"]:
                    name = "request_confirmation"
                elif not t["reply_mail_id"]:
                    continue
                elif not t["reply_body_read"]:
                    name, args = "read_mail", {"message_id": str(t["reply_mail_id"])}
                elif str(t["reply_mail_id"]) not in acknowledged:
                    name, args = "acknowledge_mail", {"message_ids": [str(t["reply_mail_id"])]}
                elif not t["owner_report_id"]:
                    name = "report_confirmation"
                else:
                    continue
            else:
                if str(t["request_mail_id"]) not in acknowledged:
                    a = tool("read_mail", {"message_id": str(t["request_mail_id"])}, uuid4().hex)
                    b = tool(
                        "acknowledge_mail",
                        {"message_ids": [str(t["request_mail_id"])]},
                        uuid4().hex,
                    )
                    response = a.model_copy(update={"tool_calls": a.tool_calls + b.tool_calls})
                    break
                if not t["question_message_id"]:
                    name = "ask_confirmation_owner"
                elif not t["decision"]:
                    continue
                elif not t["reply_mail_id"]:
                    name = "reply_confirmation"
                elif not t["peer_report_id"]:
                    name = "report_confirmation"
                else:
                    continue
            response = tool(name, args, uuid4().hex)
            break
        if response is None:
            waiting_owner = any(
                t["peer_owner_id"] == self.owner and t["question_message_id"] and not t["decision"]
                for t in tasks
            )
            waiting_peer = any(
                t["owner_id"] == self.owner and t["source_message_id"] and not t["reply_mail_id"]
                for t in tasks
            )
            response = tool(
                "finish_activation",
                {
                    "status": "waiting" if waiting_owner or waiting_peer else "finished",
                    "text": "",
                    "wait_for": ["owner_message" if waiting_owner else "inbox_received"]
                    if waiting_owner or waiting_peer
                    else [],
                },
                uuid4().hex,
            )
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_daily_plan_has_seven_distinct_released_contracts_and_no_startup_task():
    p = DailyConfirmationPlan()
    assert p.clock().horizon_seconds == 604800
    assert len(p.events()) == len(p.confirmations()) == 7
    assert len({t["task_key"] for t in p.confirmations()}) == 7
    assert [e.at_seconds for e in p.events()] == [d * 86400 + 43200 for d in range(7)]
    assert not p.scene().initial_mail
    for day, (event, contract) in enumerate(zip(p.events(), p.confirmations(), strict=True)):
        assert event.event_key == contract["activation_event_key"]
        assert event.payload["owner_response_required"] is True
        assert contract["terms"] in event.payload["text"]
        assert contract["condition"] in event.payload["text"]
        assert ("16:00" if day % 2 == 0 else "14:00") in contract["terms"]
    with pytest.raises(ValidationError):
        DailyConfirmationPlan(pairs=3)


async def test_daily_start_requires_free_control_before_provider_access(tmp_path):
    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/run_clock_actors.py"))
    ready = tmp_path / "readiness.json"
    ready.write_text(
        json.dumps({"eligible": True, "plan_hash": digest(DailyConfirmationPlan().manifest())})
    )
    args = Namespace(
        confirmation_short=False,
        event_profile="daily-confirmation",
        days=7,
        pairs=2,
        scenario=None,
        no_chaos=False,
        events_per_day=1,
        seed=20261109,
        prepare=None,
        resume=None,
        preflight=None,
        readiness=ready,
        baseline_manifest=None,
        max_actions=1,
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("Provider access before the free control is forbidden.")

    module["main"].__globals__["fetch_model_prices"] = forbidden
    with pytest.raises(SystemExit, match="free seven-day control"):
        await module["main"](args)


async def test_budget_checkpoint_retains_generated_actor_candidate_before_review():
    view = contract_view()
    candidate = action_output(view).model_dump(mode="json")
    actor = ModelActor(
        Responses([candidate]),
        reviewer=Responses([BudgetExceeded("Cannot reserve review.")]),
        max_repairs=0,
    )
    with pytest.raises(ModelGenerationError) as raised:
        await actor.respond(view)
    assert raised.value.kind == "budget"
    assert actor.last_attempts[-1]["failure_kind"] == "budget_checkpoint"
    assert actor.last_attempts[-1]["candidate"]["confirmation_actions"]
    assert actor.last_attempts[-1]["generated_candidate"]


def test_current_confirmation_schema_requires_exact_task_field_without_choosing_peer_consent():
    view = contract_view()
    schema = _candidate_schema(view)
    candidate = action_output(view).model_dump(mode="json", exclude={"source_event_id"})
    assert "confirmation_actions" in schema.model_json_schema()["required"]
    schema.model_validate_json(json.dumps(candidate))
    for actions in [[], [{"task_id": str(uuid4()), "kind": "request"}]]:
        with pytest.raises(ValidationError):
            schema.model_validate_json(json.dumps(dict(candidate, confirmation_actions=actions)))
    task = dict(
        view.confirmations["tasks"][0],
        source_message_id=str(uuid4()),
        request_mail_id=str(uuid4()),
        question_message_id=str(uuid4()),
    )
    peer = view.model_copy(
        update={"owner_id": "bob", "confirmations": {"enabled": True, "tasks": [task]}}
    )
    peer_schema = _candidate_schema(peer)
    for choice in ["confirm", "decline"]:
        peer_schema.model_validate_json(
            json.dumps(
                dict(candidate, confirmation_actions=[{"task_id": task["id"], "kind": choice}])
            )
        )


@pytest.mark.parametrize("budget_stop", [False, True, "schema_recovery"])
async def test_free_daily_seven_day_graph_or_budget_checkpoint(
    pool, app, test_database_url, budget_stop
):
    schema_recovery = budget_stop == "schema_recovery"
    budget_stop = budget_stop is True
    p = DailyConfirmationPlan()
    manifest = await Director(pool).create_run(p.scene())
    run = UUID(manifest["run_id"])
    await DirectorClock(pool).bind(run, p.clock(), max_jobs=10000)
    await TimedTransfers(pool).enable(run)
    await install_community_events(pool, run, p)
    agents = {}
    async with AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url))
        dropped = DroppedReceipt(app)
        for member in manifest["participants"]:
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=dropped
                    if member["owner_id"] == "alice"
                    else httpx.ASGITransport(app=app),
                    base_url="http://test/api/v1/",
                    headers={"Authorization": "Bearer " + member["token"]},
                )
            )
            agents[member["owner_id"]] = runtime.agent(
                model=FreeAgent(
                    pool=pool, run=run, owner=member["owner_id"], stop_after=5 if budget_stop else 0
                ),
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, timed_transfers=True, confirmations=True),
                agent_id=member["agent_id"],
                session_id=member["session_id"],
                instructions=CONFIRMATION_INSTRUCTIONS + DAILY_CONFIRMATION_INSTRUCTIONS,
                require_explicit_finish=True,
                include_previous_activation_context=True,
                max_model_calls=16,
            )
        actors = {o: FreeOwner() for o in agents}
        if schema_recovery:
            actors["alice"] = RejectOnceOwner()
        conductor = ClockRuntime(pool, actors=actors, agents=agents)
        config = {"confirmations": p.confirmations(), "source_hash": "a" * 64}
        await conductor.enable(run, config)
        async with pool.connection() as conn:
            assert not await visible(conn, run, "alice")
            assert not await visible(conn, run, "bob")
        if budget_stop:
            with pytest.raises(AgentBudgetError):
                await conductor.advance(run, max_actions=1000)
            artifact = await keep_control(pool, run, "daily-confirmation-budget-checkpoint")
            assert artifact["simulated_seconds"] < 604800
            assert artifact["uncertain_jobs"] == 1
            async with pool.connection() as conn:
                jobs = await rows(
                    conn,
                    "SELECT * FROM director_clock_jobs WHERE run_id=%s AND status='uncertain'",
                    (run,),
                )
                assert jobs[0]["input"] and jobs[0]["output"] is None
                assert not await row(
                    conn, "SELECT 1 FROM model_spend_attempts WHERE run_id=%s", (run,)
                )
            return
        if schema_recovery:
            with pytest.raises(ActorContractError):
                await conductor.advance(run, max_actions=1000)
            async with pool.connection() as conn:
                failed = await row(
                    conn,
                    "SELECT * FROM director_clock_jobs WHERE run_id=%s AND status='uncertain'",
                    (run,),
                )
                before = await row(
                    conn,
                    "SELECT output FROM clock_runtime_actor_outputs WHERE job_id=%s",
                    (failed["id"],),
                )
                await conn.execute(
                    "UPDATE clock_runtime_actor_outputs SET "
                    "output=jsonb_build_object('failure_kind','ModelGenerationError') "
                    "WHERE job_id=%s",
                    (failed["id"],),
                )
            from agentrpg.director import DirectorError

            with pytest.raises(DirectorError, match="captured, rejected"):
                await recover_confirmation_schema(
                    pool,
                    run,
                    failed["id"],
                    source_after="b" * 64,
                    carried_output_tokens=10,
                    evidence_reference="free-known-rejection-control",
                )
            async with pool.connection() as conn:
                from psycopg.types.json import Jsonb

                await conn.execute(
                    "UPDATE clock_runtime_actor_outputs SET output=%s WHERE job_id=%s",
                    (Jsonb(before["output"]), failed["id"]),
                )
            receipt = await recover_confirmation_schema(
                pool,
                run,
                failed["id"],
                source_after="b" * 64,
                carried_output_tokens=10,
                evidence_reference="free-known-rejection-control",
            )
            assert receipt["input_hash"] == failed["input_hash"]
            assert receipt["diagnostics"][0]["generated_candidate"]["confirmation_actions"] == []
            assert (
                await recover_confirmation_schema(
                    pool,
                    run,
                    failed["id"],
                    source_after="b" * 64,
                    carried_output_tokens=10,
                    evidence_reference="free-known-rejection-control",
                )
                == receipt
            )
            from agentrpg.director import DirectorError

            with pytest.raises(DirectorError, match="different evidence"):
                await recover_confirmation_schema(
                    pool,
                    run,
                    failed["id"],
                    source_after="b" * 64,
                    carried_output_tokens=0,
                    evidence_reference="free-known-rejection-control",
                )
            config["source_hash"] = "b" * 64
            await conductor.enable(run, config)
        restarted = False
        for _ in range(100):
            snapshot = await conductor.advance(run, max_actions=20)
            async with pool.connection() as conn:
                done = await row(
                    conn,
                    "SELECT count(*) AS n FROM confirmation_tasks WHERE run_id=%s "
                    "AND owner_report_id IS NOT NULL",
                    (run,),
                )
            if done["n"] >= 3 and not restarted:
                conductor = ClockRuntime(pool, actors=actors, agents=agents)
                await conductor.enable(run, config)
                restarted = True
            if snapshot["status"] == "horizon":
                break
        assert restarted and dropped.dropped
        artifact = await keep_control(
            pool,
            run,
            "daily-confirmation-schema-recovery-seven-day"
            if schema_recovery
            else "daily-confirmation-seven-day",
        )
        assert artifact["simulated_seconds"] == 604800 and artifact["clock_status"] == "horizon"
        assert artifact["native_agent_mail"] == 14 and artifact["uncertain_jobs"] == 0
        assert artifact["native_payments"] == 0
        assert len(artifact["confirmation_tasks"]) == 7
        for t in artifact["confirmation_tasks"]:
            assert t["decision"] == "confirm" and t["reply_body_read"]
            assert t["owner_report_id"] and t["peer_report_id"]
        async with pool.connection() as conn:
            assert not await row(conn, "SELECT 1 FROM model_spend_attempts WHERE run_id=%s", (run,))
            assert (
                len(
                    await rows(
                        conn,
                        "SELECT id FROM owner_messages WHERE run_id=%s AND source='agent'",
                        (run,),
                    )
                )
                == 21
            )
            for owner in agents:
                assert len(await visible(conn, run, owner)) == 7
            if schema_recovery:
                saved = await row(
                    conn,
                    "SELECT timed_output FROM clock_runtime_actor_outputs WHERE job_id=%s",
                    (failed["id"],),
                )
                assert saved["timed_output"]["output_tokens"] == 20
                restored = await row(
                    conn, "SELECT * FROM director_clock_jobs WHERE id=%s", (failed["id"],)
                )
                assert (
                    restored["input_hash"] == failed["input_hash"]
                    and restored["status"] == "applied"
                )
                assert (
                    await recover_confirmation_schema(
                        pool,
                        run,
                        failed["id"],
                        source_after="b" * 64,
                        carried_output_tokens=10,
                        evidence_reference="free-known-rejection-control",
                    )
                    == receipt
                )
        if artifact:
            # Export already contains the complete native proof, not synthetic day labels.
            assert artifact["ordered_event_delivery"]
