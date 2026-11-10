"""Real graphs and isolated PostgreSQL; supplied responses are not model evidence."""

import json
import os
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from langchain_core.messages import AIMessage, messages_to_dict
from test_clock_transfers import setup
from test_deep_agent import ScriptedModel

from agentrpg.activation import ActivationEnvelope
from agentrpg.actor import ActorOutput
from agentrpg.agent_model import UsageTracker
from agentrpg.agent_tools import BoardTools
from agentrpg.clock_evidence import export_clock_evidence
from agentrpg.clock_graph import ClockGraph
from agentrpg.clock_runtime import ClockRuntime
from agentrpg.clock_transfers import TimedTransfers
from agentrpg.community_experiment import CommunityPlan, DailyMeetingPlan, install_community_events
from agentrpg.deep_agent import AgentRuntime, AgentRuntimeError
from agentrpg.director import Director, row, rows
from agentrpg.director_clock import DirectorClock
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.inbox import InboxService
from agentrpg.mail_schema import MailRequest
from agentrpg.simulation_clock import ClockConflict, ClockContract, ProcessSpec, Schedule
from agentrpg.transfer_demo import scenario


def metered(message):
    return message.model_copy(
        update={"usage_metadata": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}
    )


def tool(name, args, identity):
    return metered(
        AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": identity, "type": "tool_call"}],
        )
    )


async def test_graph_node_stops_before_tool_and_replays_saved_model_without_new_call(
    pool, app, test_database_url
):
    director, transfers, run, clock, manifest = await setup(pool)
    alice = next(p for p in manifest["participants"] if p["owner_id"] == "alice")
    model = ScriptedModel(
        responses=[
            tool("inspect_transfer_account", {}, "balance"),
            metered(AIMessage(content="No payment requested.")),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + alice["token"]},
    ) as client:
        async with AgentRuntime(test_database_url) as runtime:
            agent = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, timed_transfers=True),
                agent_id=alice["agent_id"],
                session_id=alice["session_id"],
                instructions="Use actual receipts.",
            )
            graph = ClockGraph(agent)
            envelope = ActivationEnvelope(
                run_id=run, participant_id=alice["agent_id"], activation_id=uuid4(), window_id=0
            )
            assert (await graph.open(envelope))["phase"] == "model"
            step = uuid4()
            result = await graph.advance(envelope, step, "model")
            assert result["phase"] == "tools" and model.calls == 1 and not agent.events
            assert await graph.advance(envelope, step, "model") == result and model.calls == 1
            assert (await graph.advance(envelope, uuid4(), "tools"))["phase"] == "model"
            assert len(agent.events) == 1 and model.calls == 1, (
                (await agent.graph.aget_state(agent.config)).values["messages"][-1].content
            )
            result = await graph.advance(envelope, uuid4(), "model")
            assert result["phase"] == "done" and result["result"]["text"] == "No payment requested."
            assert model.calls == 2


async def keep_control(pool, run, case):
    directory = os.environ.get("CLOCK_CONTROL_OUTPUT")
    if directory:
        return await export_clock_evidence(pool, run, Path(directory) / case / str(run))


class SuppliedOwner:
    def __init__(self, owner, tracked=False):
        self.owner, self.calls, self.usage = owner, 0, UsageTracker()
        self.last_attempts = []
        self.tracked = tracked

    async def respond(self, view):
        self.calls += 1
        self.usage.output_tokens += 10
        if self.calls == 1 and self.owner == "alice":
            return ActorOutput(
                kind="reply",
                source_event_id=view.source_event_id,
                text="Transfer 10 credits to Bob for lunch, tell him and report the receipt.",
                transfer_consent={
                    "recipient_owner_id": "bob",
                    "amount": 10,
                    "memo": "Lunch",
                    **({"task_key": "lunch"} if self.tracked else {}),
                },
                transfer_task_requests=(
                    {
                        "kind": "open",
                        "task_key": "lunch",
                        "recipient_owner_id": "bob",
                        "description": "Repay Bob for lunch once",
                        "blocked_on": "owner_message",
                    },
                )
                if self.tracked
                else (),
            )
        if self.calls == 1:
            return ActorOutput(
                kind="reply",
                source_event_id=view.source_event_id,
                text="Check my account and report actual incoming money.",
            )
        return ActorOutput(kind="wait", source_event_id=view.source_event_id)


async def runtime_case(
    pool, app, test_database_url, *, horizon=100, balance=100, tracked=False, actors_override=None
):
    data = scenario().model_dump(mode="json")
    data["world"]["balances"]["alice"] = balance
    manifest = await Director(pool).create_run(type(scenario()).model_validate(data))
    run = UUID(manifest["run_id"])
    director = DirectorClock(pool)
    contract = ClockContract(
        epoch=datetime(2026, 11, 9, 9, tzinfo=UTC),
        horizon_seconds=horizon,
        output_tokens_per_minute=600,
        tool_seconds={"transfer_credits": 7, "graph_tools": 1},
        processes=tuple(
            ProcessSpec(id=f"{role}-{p['owner_id']}", owner_id=p["owner_id"], role=role)
            for p in manifest["participants"]
            for role in ["actor", "agent"]
        ),
    )
    clock = await director.bind(run, contract)
    await TimedTransfers(pool).enable(run)
    actor_job = uuid5(run, "clock-job:" + str(uuid5(run, "runtime:actor-alice:0")))
    grant = uuid5(actor_job, "clock-transfer-consent")
    actors = {
        p["owner_id"]: SuppliedOwner(p["owner_id"], tracked=tracked)
        for p in manifest["participants"]
    }
    actors.update(actors_override or {})
    stack = AsyncExitStack()
    runtime = await stack.enter_async_context(AgentRuntime(test_database_url))
    agents, models = {}, {}
    for p in manifest["participants"]:
        responses = [tool("inspect_transfer_account", {}, "check-initial")]
        if p["owner_id"] == "alice":
            responses += [
                tool(
                    "transfer_credits",
                    {
                        "authorization_id": str(grant),
                        "recipient_owner_id": "bob",
                        "amount": 10,
                        "memo": "Lunch",
                    },
                    "pay-once",
                ),
                tool("inspect_transfer_account", {}, "check-paid"),
                tool(
                    "send_mail",
                    {
                        "recipient_owner_id": "bob",
                        "subject": "Lunch repayment",
                        "body": "I verified the committed 10-credit repayment in the simulated "
                        "ledger.",
                    },
                    "notice",
                ),
                tool(
                    "finish_activation",
                    {
                        "status": "finished",
                        "text": "The ledger confirms 10 credits paid to Bob; I sent the notice.",
                    },
                    "report",
                ),
            ]
        else:
            responses += [
                tool(
                    "finish_activation",
                    {
                        "status": "waiting",
                        "text": "No incoming payment yet.",
                        "wait_for": ["inbox_received"],
                    },
                    "wait-bank",
                )
            ]
            # Filled with actual mailbox IDs by the receipt test below; do not
            # guess a bank email ID from owner-chat or event IDs.
        models[p["owner_id"]] = ScriptedModel(responses=responses)
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test/api/v1/",
                headers={"Authorization": "Bearer " + p["token"]},
            )
        )
        agents[p["owner_id"]] = runtime.agent(
            model=models[p["owner_id"]],
            board=BoardTools(client, manifest["cluster_id"]),
            environment=EnvironmentTools(
                client, run, timed_transfers=True, track_transfer_tasks=tracked
            ),
            agent_id=p["agent_id"],
            session_id=p["session_id"],
            instructions="Use actual receipts. Read bank emails. Finish explicitly.",
            guard_transfer_task_yield=tracked,
            require_explicit_finish=True,
            include_previous_activation_context=True,
        )
    conductor = ClockRuntime(pool, actors=actors, agents=agents)
    await conductor.enable(
        run,
        {"profile": "supplied-local-control", "actor": "supplied", "personal_agent": "supplied"},
    )
    return stack, conductor, run, clock, models, actors, agents


async def test_complete_actor_graph_timed_payment_notice_and_owner_report(
    pool, app, test_database_url, tmp_path
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    async with stack:
        # Advance one action at a time to read native bank IDs for the supplied
        # recipient response. No human input is added to the simulated dialogue.
        installed = False
        installed_notice = False
        for _ in range(200):
            async with pool.connection() as conn:
                bank = await row(
                    conn,
                    "SELECT id FROM inbox_messages WHERE run_id=%s AND sender_source='director'",
                    (run,),
                )
            if bank and not installed:
                models["bob"].responses += [
                    tool("read_mail", {"message_id": str(bank["id"])}, "read-bank"),
                    tool("acknowledge_mail", {"message_ids": [str(bank["id"])]}, "ack-bank"),
                    tool(
                        "finish_activation",
                        {
                            "status": "finished",
                            "text": "I read the actual bank notice and verified the incoming 10 "
                            "credits.",
                        },
                        "bob-report",
                    ),
                ]
                installed = True
            async with pool.connection() as conn:
                notice = await row(
                    conn,
                    "SELECT id FROM inbox_messages WHERE run_id=%s AND sender_source='agent'",
                    (run,),
                )
            if notice and not installed_notice:
                models["bob"].responses += [
                    tool("read_mail", {"message_id": str(notice["id"])}, "read-friend"),
                    tool("finish_activation", {"status": "finished"}, "quiet-done"),
                ]
                installed_notice = True
            try:
                state = await conductor.advance(run, max_actions=1)
            except Exception as error:
                raise AssertionError(
                    {
                        o: {
                            "calls": m.calls,
                            "remaining": len(m.responses),
                            "events": agents[o].events,
                            "last": [str(x) for x in m.seen[-2:]],
                        }
                        for o, m in models.items()
                    }
                ) from error
            if state["status"] == "horizon":
                break
        assert state["status"] == "horizon"
        async with pool.connection() as conn:
            payments = await rows(conn, "SELECT * FROM credit_transfers WHERE run_id=%s", (run,))
            balances = await rows(
                conn,
                "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s ORDER BY owner_id",
                (run,),
            )
            messages = await rows(
                conn, "SELECT * FROM owner_messages WHERE run_id=%s AND source='agent'", (run,)
            )
            notices = await rows(
                conn,
                "SELECT * FROM inbox_messages WHERE run_id=%s AND sender_source='agent'",
                (run,),
            )
        assert len(payments) == len(notices) == 1
        assert [b["balance"] for b in balances] == [90, 30]
        assert any(m["owner_id"] == "alice" and "ledger confirms" in m["content"] for m in messages)
        assert any(m["owner_id"] == "bob" and "actual bank" in m["content"] for m in messages)
        calls = sum(m.calls for m in models.values())
        assert (await conductor.advance(run))["status"] == "horizon" and sum(
            m.calls for m in models.values()
        ) == calls
        exported = await export_clock_evidence(pool, run, tmp_path / "materialized")
        evidence = json.loads((tmp_path / "materialized/trajectory.json").read_text())
        for agent in agents.values():
            actual = await agent.graph.aget_state(agent.config)
            assert evidence["latest_graph_states"][agent.session_key]["values"][
                "messages"
            ] == messages_to_dict(actual.values["messages"])
        assert exported["largest_observed_model_input_tokens"] == 20
        await keep_control(pool, run, "full-transfer")


async def test_export_reconstructs_virtual_scratch_file_deltas(
    pool, app, test_database_url, tmp_path
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    async with stack:
        models["alice"].responses = [
            tool(
                "write_file",
                {"file_path": "/note.txt", "content": "A private scratch note."},
                "note",
            ),
            tool("finish_activation", {"status": "finished"}, "note-done"),
        ]
        await conductor.advance(run, max_actions=200)
        actual = await agents["alice"].graph.aget_state(agents["alice"].config)
        assert "/note.txt" in actual.values["files"]
        summary = await export_clock_evidence(pool, run, tmp_path / "files")
        exported = json.loads((tmp_path / "files/trajectory.json").read_text())
        assert (
            exported["latest_graph_states"][agents["alice"].session_key]["values"]["files"]
            == actual.values["files"]
        )
        assert summary["virtual_scratch_files"] == 1


@pytest.mark.parametrize("role", ["actor", "agent"])
async def test_restart_reconciles_saved_original_response_before_tools(
    pool, app, test_database_url, monkeypatch, role
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    original = conductor.director._save
    hit = False

    async def fail_before_outer_save(run_id, job_id, token, output, **kwargs):
        nonlocal hit
        async with pool.connection() as conn:
            job = await row(conn, "SELECT role FROM director_clock_jobs WHERE id=%s", (job_id,))
        if job["role"] == role and not hit:
            hit = True
            raise RuntimeError("Supplied failure after durable response, before outer receipt")
        return await original(run_id, job_id, token, output, **kwargs)

    async with stack:
        monkeypatch.setattr(conductor.director, "_save", fail_before_outer_save)
        with pytest.raises(RuntimeError, match="Supplied failure"):
            await conductor.advance(run, max_actions=50)
        before = sum(m.calls for m in models.values()) + sum(a.calls for a in actors.values())
        assert not agents["alice"].events
        restarted = ClockRuntime(pool, actors=actors, agents=agents)
        await restarted.advance(run, max_actions=1)
        after = sum(m.calls for m in models.values()) + sum(a.calls for a in actors.values())
        assert before == after
        async with pool.connection() as conn:
            assert (await row(conn, "SELECT count(*) AS n FROM director_clock_recovery"))["n"] == 1
            assert (
                await row(
                    conn, "SELECT count(*) AS n FROM credit_transfers WHERE run_id=%s", (run,)
                )
            )["n"] == 0
        await keep_control(pool, run, "saved-response-" + role)


async def test_unknown_model_outcome_never_regenerates(pool, app, test_database_url):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    async with stack:
        models["alice"].responses = [
            RuntimeError("Supplied provider disconnect before saved response")
        ]
        with pytest.raises(AgentRuntimeError):
            await conductor.advance(run, max_actions=50)
        assert models["alice"].calls == 1
        with pytest.raises(AgentRuntimeError, match="uncertain"):
            await conductor.advance(run, max_actions=50)
        assert models["alice"].calls == 1


async def test_role_configuration_is_frozen_and_unreleased_writes_rejected(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    async with stack:
        client = agents["alice"].environment.client
        assert (
            await client.post(
                f"runs/{run}/owner/messages",
                json={"request_id": str(uuid4()), "content": "unreleased"},
            )
        ).status_code == 409
        assert (
            await client.post(
                f"runs/{run}/inbox",
                json={
                    "request_id": str(uuid4()),
                    "recipient_owner_id": "bob",
                    "subject": "unreleased",
                    "body": "unreleased",
                },
            )
        ).status_code == 403
        original_hash = agents["alice"]._instructions_hash
        agents["alice"]._instructions_hash = "different"
        with pytest.raises(ClockConflict, match="differ"):
            await conductor.advance(run)
        agents["alice"]._instructions_hash = original_hash
        actors["alice"].describe_output_delivery = True
        with pytest.raises(ClockConflict, match="differ"):
            await conductor.advance(run)
        actors["alice"].describe_output_delivery = False
        actors["alice"].require_live_transfer_task_binding = True
        with pytest.raises(ClockConflict, match="differ"):
            await conductor.advance(run)
        assert sum(model.calls for model in models.values()) == 0


async def test_board_write_waits_for_tool_release_and_wakes_only_peer_agent(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    async with stack:
        models["alice"].responses = [
            tool(
                "post_thread",
                {"title": "Club plan", "body": "Shall we meet on Saturday?"},
                "club-post",
            ),
            tool("finish_activation", {"status": "finished"}, "post-done"),
        ]
        models["bob"].responses = [
            tool("finish_activation", {"status": "finished"}, f"bob-{i}") for i in range(5)
        ]
        await conductor.advance(run, max_actions=100)
        assert models["alice"].calls == 2
        async with pool.connection() as conn:
            posts = await rows(
                conn,
                "SELECT * FROM messages WHERE cluster_id=(SELECT cluster_id FROM "
                "environment_runs WHERE id=%s)",
                (run,),
            )
            events = await rows(
                conn,
                "SELECT * FROM simulation_clock_events WHERE clock_id=%s AND kind='board_posted'",
                (clock,),
            )
        assert len(posts) == len(events) == 1
        assert events[0]["recipients"] == ["agent-bob"]
        assert events[0]["at_seconds"] >= 3


async def test_saved_ai_checkpoint_without_step_receipt_is_recovered(
    pool, app, test_database_url, monkeypatch
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    stage = ClockGraph._stage
    failed = False

    async def fail_after_ai(self, snapshot):
        nonlocal failed
        if (
            not failed
            and snapshot.values.get("messages")
            and isinstance(snapshot.values["messages"][-1], AIMessage)
        ):
            failed = True
            raise RuntimeError("Supplied checkpoint-before-receipt failure")
        return await stage(self, snapshot)

    async with stack:
        monkeypatch.setattr(ClockGraph, "_stage", fail_after_ai)
        with pytest.raises(RuntimeError, match="checkpoint-before-receipt"):
            await conductor.advance(run, max_actions=100)
        calls = sum(m.calls for m in models.values())
        await ClockRuntime(pool, actors=actors, agents=agents).advance(run, max_actions=1)
        assert sum(m.calls for m in models.values()) == calls
        assert not agents["alice"].events
        await keep_control(pool, run, "saved-ai-before-step-receipt")


@pytest.mark.parametrize("days", [1, 7])
@pytest.mark.parametrize(
    ("plan_type", "pairs"),
    [
        pytest.param(CommunityPlan, 15, id="mixed-fifteen-pairs"),
        pytest.param(DailyMeetingPlan, 15, id="daily-fifteen-pairs"),
        pytest.param(DailyMeetingPlan, 2, id="daily-two-pairs"),
    ],
)
async def test_fifteen_pairs_elapsed_recurrence_and_export_are_free_controls(
    pool, app, test_database_url, tmp_path, days, plan_type, pairs
):
    plan = plan_type(pairs=pairs, days=days)
    manifest = await Director(pool).create_run(plan.scene())
    run = UUID(manifest["run_id"])
    await DirectorClock(pool).bind(run, plan.clock(), max_jobs=10000)
    await TimedTransfers(pool).enable(run)
    await install_community_events(pool, run, plan)
    await install_community_events(pool, run, plan)  # exact replay

    class CommunityOwner(SuppliedOwner):
        async def respond(self, view):
            self.calls += 1
            self.usage.output_tokens += 10
            if self.calls == 1 or view.observations:
                return ActorOutput(
                    kind="reply",
                    text="Please check the shared club board and my actual balance.",
                    source_event_id=view.source_event_id,
                )
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)

    async with AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url))
        actors, agents, models = {}, {}, {}
        for p in manifest["participants"]:
            owner = p["owner_id"]
            actors[owner] = CommunityOwner(owner)
            responses = []
            async with pool.connection() as conn:
                initial_mail = await rows(
                    conn,
                    "SELECT id FROM inbox_messages WHERE run_id=%s AND recipient_owner_id=%s",
                    (run, owner),
                )
            for mail in initial_mail:
                responses += [
                    tool("read_mail", {"message_id": str(mail["id"])}, f"read-{mail['id']}"),
                    tool(
                        "acknowledge_mail", {"message_ids": [str(mail["id"])]}, f"ack-{mail['id']}"
                    ),
                ]
            activations = 1 + sum(f"actor-{owner}" in event.recipients for event in plan.events())
            for i in range(activations):
                responses += [
                    tool("list_threads", {}, f"board-{i}"),
                    tool("inspect_transfer_account", {}, f"wallet-{i}"),
                    tool(
                        "finish_activation",
                        {
                            "status": "finished",
                            "text": "I checked the committed club board and my account.",
                        },
                        f"done-{i}",
                    ),
                ]
            models[owner] = ScriptedModel(responses=responses)
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test/api/v1/",
                    headers={"Authorization": "Bearer " + p["token"]},
                )
            )
            agents[owner] = runtime.agent(
                model=models[owner],
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, timed_transfers=True),
                agent_id=p["agent_id"],
                session_id=p["session_id"],
                instructions="Read actual data and finish.",
                require_explicit_finish=True,
            )
        conductor = ClockRuntime(pool, actors=actors, agents=agents)
        await conductor.enable(run, {"kind": "fifteen-pair-supplied-control", "days": days})
        state = await conductor.advance(run, max_actions=5000)
        assert state["status"] == "horizon" and state["now_seconds"] == days * 86400
        evidence = await export_clock_evidence(pool, run, tmp_path / str(run))
        prefix = (
            f"daily-meeting-{pairs}-pairs-"
            if isinstance(plan, DailyMeetingPlan)
            else "fifteen-pairs-"
        )
        await keep_control(pool, run, f"{prefix}{days}-days")
        assert all(evidence["financial_invariants"].values())
        assert evidence["ordered_event_delivery"] and evidence["uncertain_jobs"] == 0
        assert evidence["task_success"] is None and evidence["native_payments"] == 0
        assert sum(m.calls for m in models.values()) == (pairs + len(plan.events())) * 3 + pairs * 2
        assert all("execute" not in m.offered and "task" not in m.offered for m in models.values())


def test_community_plan_is_seeded_bounded_and_does_not_claim_calibration():
    a = CommunityPlan()
    assert a.manifest() == CommunityPlan().manifest()
    assert a.manifest()["events_hash"] != CommunityPlan(seed=3).manifest()["events_hash"]
    assert len(a.events()) == 14 and all(0 < e.at_seconds < 604800 for e in a.events())
    assert not a.manifest()["chaos_calibration"]["calibrated"]
    assert a.manifest()["centralised_object_contract"]["bold_os_integration"] is None


def test_compressed_calibration_covers_all_cues_and_private_availability():
    for seed in (0, 3, 20261109):
        plan = CommunityPlan(days=1, events_per_day=4, seed=seed)
        cues = plan.events()
        assert [c.at_seconds for c in cues] == [10800, 32400, 54000, 75600]
        assert {c.payload["cue_kind"] for c in cues} == {
            "plan_change",
            "unclear_repayment",
            "payment_followup",
            "social_invitation",
        }
        scene = plan.scene()
        for owner in scene.owners:
            fact = next(f for f in scene.facts if f.id == f"availability-{owner.id}")
            assert fact.visibility == "private" and fact.owner_ids == (owner.id,)
            assert fact.id in owner.known_fact_ids
        assert not any(c.payload.get("payment") or c.payload.get("consent") for c in cues)
    plan = CommunityPlan(days=7, events_per_day=4)
    for event in plan.events():
        if event.payload["cue_kind"] in {"plan_change", "social_invitation"}:
            import re

            target = re.search(r"\d{4}-\d{2}-\d{2}", event.payload["text"]).group()
            assert (
                datetime.fromisoformat(target).date()
                > (plan.epoch + timedelta(seconds=event.at_seconds)).date()
            )
        if event.payload["cue_kind"] == "payment_followup":
            assert event.recipients == ("actor-alice",)
    assert [e.payload["cue_kind"] for e in CommunityPlan(days=1, events_per_day=4).events()] == [
        "payment_followup",
        "social_invitation",
        "plan_change",
        "unclear_repayment",
    ]


async def test_rejected_timed_payment_does_not_send_false_success_or_debit(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url, balance=5
    )
    async with stack:
        models["alice"].responses = models["alice"].responses[:2] + [
            tool("inspect_transfer_account", {}, "inspect-after-rejection"),
            tool(
                "finish_activation",
                {
                    "status": "finished",
                    "text": "The transfer failed because there are insufficient credits. No "
                    "payment was made.",
                },
                "honest-rejection",
            ),
        ]
        await conductor.advance(run, max_actions=100)
        async with pool.connection() as conn:
            assert (
                await row(
                    conn, "SELECT count(*) AS n FROM credit_transfers WHERE run_id=%s", (run,)
                )
            )["n"] == 0
            op = await row(conn, "SELECT * FROM clock_transfer_operations WHERE run_id=%s", (run,))
            assert op["status"] == "rejected"
            assert (
                await row(conn, "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s", (run,))
            )["n"] == 0
            assert [
                r["balance"]
                for r in await rows(
                    conn,
                    "SELECT balance FROM world_accounts WHERE run_id=%s ORDER BY owner_id",
                    (run,),
                )
            ] == [5, 20]
        await keep_control(pool, run, "insufficient-balance")


async def test_explicit_owner_tool_report_is_not_duplicated_by_finish_text(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    async with stack:
        models["alice"].responses = [
            tool(
                "send_owner_message",
                {"content": "I need clarification before any payment."},
                "ask-owner",
            ),
            tool(
                "finish_activation",
                {
                    "status": "waiting",
                    "text": "I need clarification before any payment.",
                    "wait_for": ["owner_message"],
                },
                "wait-owner",
            ),
        ]
        await conductor.advance(run, max_actions=100)
        async with pool.connection() as conn:
            messages = await rows(
                conn,
                "SELECT * FROM owner_messages WHERE run_id=%s AND owner_id='alice'"
                " AND source='agent'",
                (run,),
            )
        assert len(messages) == 1


async def test_actor_direct_mail_is_native_timed_and_replay_safe(pool, app, test_database_url):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    original = actors["alice"].respond

    async def send(view):
        output = await original(view)
        return output.model_copy(
            update={
                "text": "Please help with our club. I am sending Bob an invitation.",
                "transfer_consent": None,
                "mail_requests": (
                    MailRequest(
                        recipient_owner_id="bob",
                        subject="Club invitation",
                        body="Would you like to meet on Saturday?",
                    ),
                ),
            }
        )

    actors["alice"].respond = send
    actor_job = uuid5(run, "clock-job:" + str(uuid5(run, "runtime:actor-alice:0")))
    mail_id = uuid5(run, "mail:actor:alice:" + str(uuid5(actor_job, "actor-mail:0")))
    async with stack:
        models["alice"].responses = [
            tool("finish_activation", {"status": "finished"}, "alice-quiet")
        ]
        models["bob"].responses = [
            tool("read_mail", {"message_id": str(mail_id)}, "read-invite"),
            tool(
                "finish_activation",
                {"status": "finished", "text": "I read your friend's actual invitation."},
                "invitation-report",
            ),
        ]
        for _ in range(2):
            await conductor.advance(run, max_actions=1)
        async with pool.connection() as conn:
            assert (
                await row(conn, "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s", (run,))
            )["n"] == 0
        await conductor.advance(run, max_actions=100)
        async with pool.connection() as conn:
            mail = await rows(conn, "SELECT * FROM inbox_messages WHERE run_id=%s", (run,))
        assert len(mail) == 1 and mail[0]["id"] == mail_id and mail[0]["sender_source"] == "actor"
        await keep_control(pool, run, "actor-direct-mail")


@pytest.mark.parametrize("tracked", [False, True])
async def test_actor_mail_failure_rolls_back_then_reuses_original_clock_step(
    pool, app, test_database_url, monkeypatch, tracked
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url, tracked=tracked
    )
    original = actors["alice"].respond

    async def send(view):
        output = await original(view)
        return output.model_copy(
            update={
                "mail_requests": (
                    MailRequest(
                        recipient_owner_id="bob", subject="A note", body="A private owner message."
                    ),
                )
            }
        )

    actors["alice"].respond = send
    service_send = InboxService.send

    async def fail_after_write(self, *args, **kwargs):
        await service_send(self, *args, **kwargs)
        raise RuntimeError("Supplied failure after Actor mail write")

    async with stack:
        for _ in range(2):
            await conductor.advance(run, max_actions=1)
        monkeypatch.setattr(InboxService, "send", fail_after_write)
        failed_id = None
        for _ in range(4):
            step_id = uuid4()
            try:
                await conductor.director.step(run, step_id)
            except RuntimeError:
                failed_id = step_id
                break
        assert failed_id
        async with pool.connection() as conn:
            for table in ["inbox_messages", "transfer_grants", "transfer_tasks"]:
                assert (
                    await row(conn, f"SELECT count(*) AS n FROM {table} WHERE run_id=%s", (run,))
                )["n"] == 0
            assert (
                await row(
                    conn,
                    "SELECT count(*) AS n FROM owner_messages WHERE run_id=%s AND owner_id='alice'",
                    (run,),
                )
            )["n"] == 0
        monkeypatch.setattr(InboxService, "send", service_send)
        result = await conductor.director.step(run, failed_id)
        assert await conductor.director.step(run, failed_id) == result
        async with pool.connection() as conn:
            assert (
                await row(conn, "SELECT count(*) AS n FROM inbox_messages WHERE run_id=%s", (run,))
            )["n"] == 1
            assert (
                await row(conn, "SELECT count(*) AS n FROM transfer_tasks WHERE run_id=%s", (run,))
            )["n"] == int(tracked)
            assert (
                await row(conn, "SELECT count(*) AS n FROM transfer_grants WHERE run_id=%s", (run,))
            )["n"] == 1


async def test_actor_receives_latest_private_assistant_wait_reason(pool, app, test_database_url):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    seen = []
    original = actors["alice"].respond

    async def capture(view):
        seen.append(view.assistant_wait_for)
        return await original(view)

    actors["alice"].respond = capture
    async with stack:
        models["alice"].responses = [
            tool(
                "finish_activation",
                {
                    "status": "waiting",
                    "text": "Please clarify what you would like me to do next.",
                    "wait_for": ["owner_message"],
                },
                "clarify",
            )
        ]
        await conductor.advance(run, max_actions=100)
        assert ("owner_message",) in seen


@pytest.mark.parametrize("fail_after_commit", [False, True])
async def test_tracked_business_task_closes_only_with_real_payment_notice_and_report(
    pool, app, test_database_url, monkeypatch, fail_after_commit
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url, tracked=True
    )
    async with stack:
        original_execute = EnvironmentTools.execute
        injected = False

        async def lost_response(self, name, arguments, call_id):
            nonlocal injected
            result = await original_execute(self, name, arguments, call_id)
            if (
                fail_after_commit
                and not injected
                and name == "complete_transfer_task"
                and result.get("ok")
            ):
                injected = True
                raise AgentRuntimeError("Supplied crash after native task completion commit")
            return result

        monkeypatch.setattr(EnvironmentTools, "execute", lost_response)
        models["alice"].responses = models["alice"].responses[:4] + [
            tool(
                "send_owner_message",
                {
                    "content": "The ledger confirms 10 credits paid to Bob; I sent the actual "
                    "notice."
                },
                "report-tool",
            )
        ]
        models["bob"].responses = []
        for i in range(8):
            models["bob"].responses += [
                tool("list_mail", {}, f"read-{i}"),
                tool(
                    "finish_activation",
                    {"status": "finished", "text": "I checked my actual mailbox."},
                    f"bob-done-{i}",
                ),
            ]
        installed = False
        for _ in range(250):
            async with pool.connection() as conn:
                task = await row(conn, "SELECT * FROM transfer_tasks WHERE run_id=%s", (run,))
                notice = await row(
                    conn,
                    "SELECT * FROM inbox_messages WHERE run_id=%s AND sender_source='agent'",
                    (run,),
                )
                report = await row(
                    conn,
                    "SELECT * FROM owner_messages WHERE run_id=%s AND owner_id='alice'"
                    " AND source='agent'",
                    (run,),
                )
            if task and task["payment_request_id"] and notice and report and not installed:
                models["alice"].responses += [
                    tool(
                        "complete_transfer_task",
                        {
                            "task_id": str(task["id"]),
                            "receipt_id": str(task["payment_request_id"]),
                            "recipient_notice_id": str(notice["id"]),
                            "owner_report_id": str(report["id"]),
                        },
                        "close-task",
                    ),
                    tool("finish_activation", {"status": "finished"}, "finished-task"),
                ]
                installed = True
            try:
                state = await conductor.advance(run, max_actions=1)
            except AgentRuntimeError:
                assert injected
                async with pool.connection() as conn:
                    assert (
                        await row(conn, "SELECT status FROM transfer_tasks WHERE run_id=%s", (run,))
                    )["status"] == "completed"
                conductor = ClockRuntime(pool, actors=actors, agents=agents)
            if state["status"] == "horizon":
                break
        async with pool.connection() as conn:
            task = await row(conn, "SELECT * FROM transfer_tasks WHERE run_id=%s", (run,))
            assert task["status"] == "completed"
            assert task["completion_evidence"]["receipt_id"] == str(task["payment_request_id"])
            assert (
                await row(
                    conn, "SELECT count(*) AS n FROM credit_transfers WHERE run_id=%s", (run,)
                )
            )["n"] == 1
        assert state["status"] == "horizon"
        assert models["alice"].calls == 7
        assert injected == fail_after_commit
        assert {"complete_transfer_task", "defer_transfer_task", "list_transfer_tasks"} <= set(
            models["alice"].offered
        )
        await keep_control(pool, run, "tracked-business-task")


async def test_horizon_retains_unfinished_native_business_task_without_payment(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url, tracked=True, horizon=8
    )
    async with stack:
        state = await conductor.advance(run, max_actions=100)
        assert state["status"] == "horizon" and state["now_seconds"] == 8
        async with pool.connection() as conn:
            task = await row(conn, "SELECT * FROM transfer_tasks WHERE run_id=%s", (run,))
            op = await row(conn, "SELECT * FROM clock_transfer_operations WHERE run_id=%s", (run,))
            assert task["status"] == "active" and task["payment_request_id"] is None
            assert op["status"] == "pending" and op["due_seconds"] == 12
            assert (
                await row(
                    conn, "SELECT count(*) AS n FROM credit_transfers WHERE run_id=%s", (run,)
                )
            )["n"] == 0
        await keep_control(pool, run, "tracked-task-horizon")


async def test_actor_wait_with_old_text_does_not_create_an_owner_instruction(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    for actor in actors.values():

        async def wait(view, actor=actor):
            actor.usage.output_tokens += 10
            return ActorOutput(
                kind="wait",
                text="An old request that must not be delivered again.",
                source_event_id=view.source_event_id,
            )

        actor.respond = wait
    async with stack:
        state = await conductor.advance(run, max_actions=100)
        assert state["status"] == "horizon"
        assert sum(m.calls for m in models.values()) == 0
        async with pool.connection() as conn:
            assert (
                await row(conn, "SELECT count(*) AS n FROM owner_messages WHERE run_id=%s", (run,))
            )["n"] == 0
            saved = await rows(conn, "SELECT * FROM clock_runtime_actor_outputs")
            assert all(o["output"]["text"] and not o["timed_output"]["text"] for o in saved)
        await keep_control(pool, run, "actor-wait-text-is-not-dialogue")


async def test_paired_actor_reads_question_after_agent_yields_then_answer_is_delivered(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    original = actors["alice"].respond
    second = False

    async def answer(view):
        nonlocal second
        async with pool.connection() as conn:
            active = await row(
                conn,
                "SELECT count(*) AS n FROM clock_runtime_activations WHERE "
                "run_id=%s AND process_id='agent-alice' AND status='active'",
                (run,),
            )
            assert active["n"] == 0
        if actors["alice"].calls == 1 and not second:
            second = True
            actors["alice"].calls += 1
            actors["alice"].usage.output_tokens += 10
            return ActorOutput(
                kind="reply",
                text="Saturday at 15:00 works for me.",
                source_event_id=view.source_event_id,
            )
        return await original(view)

    actors["alice"].respond = answer
    async with stack:
        models["alice"].responses = [
            tool("send_owner_message", {"content": "Which time works for you?"}, "question"),
            tool(
                "finish_activation",
                {"status": "waiting", "wait_for": ["owner_message"]},
                "yield-for-answer",
            ),
            tool(
                "finish_activation",
                {"status": "finished", "text": "I received your Saturday 15:00 answer."},
                "answer-used",
            ),
        ]
        await conductor.advance(run, max_actions=150)
        async with pool.connection() as conn:
            inputs = await rows(
                conn,
                "SELECT envelope FROM clock_runtime_activations WHERE run_id=%s "
                "AND process_id='agent-alice' ORDER BY ordinal",
                (run,),
            )
        assert len(inputs) == 2
        assert any(
            "Saturday at 15:00" in m["content"] for m in inputs[1]["envelope"]["owner_messages"]
        )
        assert all(
            o["kind"] != "paired_role_complete"
            for x in inputs
            for o in x["envelope"]["world_observations"]
        )
        await keep_control(pool, run, "paired-question-answer")


async def test_queued_input_survives_silent_actor_turn_without_empty_pingpong(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    for actor in actors.values():

        async def quiet(view, actor=actor):
            actor.calls += 1
            actor.usage.output_tokens += 10
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)

        actor.respond = quiet
    async with stack:
        models["alice"].responses = [
            tool("finish_activation", {"status": "finished"}, "material-change")
        ]
        await conductor.director.control(
            run,
            uuid4(),
            Schedule(
                event_key="initial-material",
                at_seconds=0,
                kind="world_change",
                recipients=("agent-alice",),
            ),
        )
        state = await conductor.advance(run, max_actions=100)
        assert state["status"] == "horizon"
        assert models["alice"].calls == 1 and models["bob"].calls == 0
        assert all(a.calls == 1 for a in actors.values())


async def test_write_after_same_second_notice_gets_a_later_causal_priority(
    pool, app, test_database_url
):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url
    )
    async with stack:
        models["alice"].responses = [
            tool("send_owner_message", {"content": "A timed owner question."}, "timed-question"),
            tool(
                "finish_activation",
                {"status": "waiting", "wait_for": ["owner_message"]},
                "wait-timed",
            ),
        ]
        await conductor.director.control(
            run,
            uuid4(),
            Schedule(
                event_key="z-notice-before-tool-write",
                at_seconds=3,
                priority=1,
                kind="world_change",
                recipients=("actor-bob",),
            ),
        )
        await conductor.advance(run, max_actions=100)
        sent = [e for e in agents["alice"].events if e["tool"] == "send_owner_message"]
        assert len(sent) == 1 and sent[0]["result"]["ok"]
        async with pool.connection() as conn:
            e = await row(
                conn,
                "SELECT priority FROM simulation_clock_events WHERE clock_id=%s "
                "AND kind='assistant_reply' AND at_seconds=3",
                (clock,),
            )
        assert e["priority"] == 2


async def test_unreleased_task_update_cannot_mutate_scoped_world(pool, app, test_database_url):
    stack, conductor, run, clock, models, actors, agents = await runtime_case(
        pool, app, test_database_url, tracked=True
    )
    async with stack:
        client = agents["alice"].environment.client
        response = await client.post(
            f"runs/{run}/transfer-tasks/defer",
            json={"request_id": str(uuid4()), "task_id": str(uuid4()), "wait_for": "owner_message"},
        )
        assert response.status_code == 403
        async with pool.connection() as conn:
            assert (
                await row(conn, "SELECT count(*) AS n FROM transfer_tasks WHERE run_id=%s", (run,))
            )["n"] == 0
