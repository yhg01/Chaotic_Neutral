"""Durable recovery, pause races, scoped progress and real graph replay; no provider calls."""

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from test_deep_agent import ScriptedModel, board_client, make_agent
from test_shop import add, ready_shop

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput, ModelGenerationError
from agentrpg.agent_model import ProviderCallError
from agentrpg.budget import BudgetExceeded
from agentrpg.deep_agent import AgentBudgetError, AgentLimitError, AgentRuntime, AgentTransientError
from agentrpg.director import Director, DirectorError, row
from agentrpg.run_engine import RunEngine
from agentrpg.runtime_failures import classify_failure
from agentrpg.scenario_schema import vehicle_scenario
from agentrpg.shop import ShopService
from agentrpg.supervision import RuntimeSupervisor, SupervisionPolicy

POLICY = SupervisionPolicy(base_delay_seconds=0.02, max_delay_seconds=0.04)


@pytest.mark.parametrize(
    "error,kind,retryable",
    [
        (ProviderCallError("private", retryable=True), "provider_transient", True),
        (ProviderCallError("private"), "provider_rejected", False),
        (ModelGenerationError("private", kind="budget"), "budget", False),
        (BudgetExceeded("private"), "budget", False),
        (AgentBudgetError("private"), "budget", False),
        (AgentLimitError("private"), "task_limit", False),
        (AgentTransientError(tool=True), "tool_transient", True),
        (RuntimeError("private"), "unknown", False),
    ],
)
def test_failure_classification_is_explicit_and_contains_no_exception_body(error, kind, retryable):
    result = classify_failure(error)
    assert result.kind == kind and result.retryable is retryable
    assert "private" not in repr(result)


class QuietActor:
    async def respond(self, view):
        return ActorOutput(kind="wait", source_event_id=view.source_event_id)


class FlakyAgent:
    def __init__(self, failures=1, error=None):
        self.failures = failures
        self.inputs = []
        self.error = error or AgentTransientError()

    async def run_activation(self, envelope):
        self.inputs.append(envelope.model_dump(mode="json"))
        if len(self.inputs) <= self.failures:
            raise self.error
        return ActivationResult(status="finished")


async def failed_run(pool, *, error=None, failures=1):
    director = Director(pool)
    manifest = await director.create_run(vehicle_scenario())
    run = UUID(manifest["run_id"])
    supervisor = RuntimeSupervisor(pool)
    await supervisor.bind(run, POLICY)
    await director.control(run, "start")
    first, peer = FlakyAgent(failures, error), FlakyAgent(0)
    engine = RunEngine(
        pool,
        actors={p["owner_id"]: QuietActor() for p in manifest["participants"]},
        agents={"owner-1": first, "owner-2": peer},
    )
    assert (await engine.advance(run))["status"] == "paused"
    return run, supervisor, first, peer


async def make_due(pool, run):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE run_supervision SET retry_due_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s",
            (run,),
        )


async def test_deadline_and_input_survive_new_supervisor_without_replaying_successful_peer(pool):
    run, supervisor, first, peer = await failed_run(pool)
    assert await supervisor.recover(run) == "waiting"
    async with pool.connection() as conn:
        due = (await row(conn, "SELECT retry_due_at FROM run_supervision WHERE run_id=%s", (run,)))[
            "retry_due_at"
        ]
    restored = RuntimeSupervisor(pool)
    assert await restored.recover(run) == "waiting"
    async with pool.connection() as conn:
        assert (
            await row(conn, "SELECT retry_due_at FROM run_supervision WHERE run_id=%s", (run,))
        )["retry_due_at"] == due
    await make_due(pool, run)
    decisions = await asyncio.gather(restored.recover(run), supervisor.recover(run))
    assert sorted(decisions) == ["resumed", "stopped"]
    engine = RunEngine(
        pool,
        actors={name: QuietActor() for name in ["owner-1", "owner-2"]},
        agents={"owner-1": first, "owner-2": peer},
    )
    final = await engine.advance(run)
    assert final["current_window"] == 1
    assert len(first.inputs) == 2 and first.inputs[0] == first.inputs[1]
    assert len(peer.inputs) == 1
    async with pool.connection() as conn:
        recovered = await row(
            conn,
            "SELECT attempts,failure_kind FROM run_jobs WHERE run_id=%s "
            "AND kind='agent' AND owner_id='owner-1'",
            (run,),
        )
    assert recovered == {"attempts": 2, "failure_kind": None}
    history = await Director(pool).trace(run)
    assert len([e for e in history if e["kind"] == "job_failure"]) == 1
    assert len([e for e in history if e["kind"] == "recovery_resumed"]) == 1


async def test_operator_pause_during_backoff_is_never_automatically_resumed(pool):
    run, supervisor, _, _ = await failed_run(pool)
    assert await supervisor.recover(run) == "waiting"
    await Director(pool).control(run, "pause")
    await make_due(pool, run)
    assert await RuntimeSupervisor(pool).recover(run) == "stopped"
    current = await Director(pool).get_run(run)
    assert current["status"] == "paused" and current["result"]["reason"] == "operator_pause"


@pytest.mark.parametrize(
    "error", [AgentBudgetError("secret"), AgentLimitError("secret"), RuntimeError("secret")]
)
async def test_budget_limit_and_unknown_errors_do_not_schedule_a_retry(pool, error):
    run, supervisor, first, _ = await failed_run(pool, error=error)
    assert await supervisor.recover(run) == "stopped"
    assert len(first.inputs) == 1
    trace = await Director(pool).trace(run)
    assert not any(e["kind"] == "recovery_scheduled" for e in trace)
    assert "secret" not in json.dumps(trace, default=str)


async def test_retry_exhaustion_retains_attempts_across_supervisor_recreation(pool):
    run, _, first, peer = await failed_run(pool, failures=10)
    engine = RunEngine(
        pool,
        actors={name: QuietActor() for name in ["owner-1", "owner-2"]},
        agents={"owner-1": first, "owner-2": peer},
    )
    for _ in range(2):
        supervisor = RuntimeSupervisor(pool)
        assert await supervisor.recover(run) == "waiting"
        await make_due(pool, run)
        assert await supervisor.recover(run) == "resumed"
        await engine.advance(run)
    assert await RuntimeSupervisor(pool).recover(run) == "stopped"
    assert (await Director(pool).get_run(run))["status"] == "incomplete"
    assert len(first.inputs) == 3 and len(peer.inputs) == 1


async def test_policy_cannot_change_or_be_enabled_on_a_historical_failed_run(pool):
    run, supervisor, _, _ = await failed_run(pool)
    await supervisor.bind(run, POLICY)
    with pytest.raises(DirectorError, match="original supervision"):
        await supervisor.bind(run, POLICY.model_copy(update={"max_retries": 1}))
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM run_supervision WHERE run_id=%s", (run,))
    with pytest.raises(DirectorError, match="before the first"):
        await supervisor.bind(run, POLICY)


async def tick_health(pool, run, window):
    # Represent the committed next Actor boundary in this isolated state test.
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE environment_runs SET current_window=%s WHERE id=%s", (window + 1, run)
        )
    return await RuntimeSupervisor(pool).check_progress(run, window)


async def test_unchanged_business_task_pauses_even_with_new_dialogue_and_cart_versions(pool):
    _, run, task, _ = await ready_shop(pool, supervision=POLICY)
    assert not await tick_health(pool, run, 1)
    for window in [2, 3]:
        assert not await tick_health(pool, run, window)
        assert not await RuntimeSupervisor(pool).check_progress(run, window)
    async with pool.connection() as conn:
        await conn.execute("UPDATE shop_carts SET version=version+1 WHERE run_id=%s", (run,))
    assert await tick_health(pool, run, 4)
    saved = await Director(pool).get_run(run)
    assert saved["result"] == {"reason": "no_progress", "task_ids": [task["id"]]}
    assert await RuntimeSupervisor(pool).recover(run) == "stopped"
    assert not (await ShopService(pool).state(run, "alice"))["orders"]


@pytest.mark.parametrize(
    "consent,quantity,reason",
    [
        (False, 1, "awaiting_authority"),
        (True, 0, "awaiting_stock_funds_or_terms"),
    ],
)
async def test_missing_authority_or_stock_is_a_saved_wait_not_a_false_stall(
    pool, consent, quantity, reason
):
    _, run, _, _ = await ready_shop(pool, supervision=POLICY, consent=consent)
    if not quantity:
        async with pool.connection() as conn:
            await conn.execute("UPDATE market_listings SET quantity=0 WHERE run_id=%s", (run,))
    for window in range(1, 6):
        assert not await tick_health(pool, run, window)
    async with pool.connection() as conn:
        health = await row(
            conn,
            "SELECT stagnant_windows,blocked_reason FROM run_task_health WHERE run_id=%s",
            (run,),
        )
    assert health == {"stagnant_windows": 0, "blocked_reason": reason}


async def test_actual_cart_change_resets_watchdog_and_pending_settlement_is_a_wait(pool):
    _, run, task, listing = await ready_shop(pool, supervision=POLICY)
    for window in [1, 2, 3]:
        assert not await tick_health(pool, run, window)
    cart = await add(pool, run, task, listing)
    assert not await tick_health(pool, run, 4)
    async with pool.connection() as conn:
        assert (
            await row(conn, "SELECT stagnant_windows FROM run_task_health WHERE run_id=%s", (run,))
        )["stagnant_windows"] == 0
    await ShopService(pool).checkout(run, "alice", uuid4(), cart["cart"]["version"])
    for window in range(5, 9):
        assert not await tick_health(pool, run, window)
    async with pool.connection() as conn:
        assert (
            await row(conn, "SELECT blocked_reason FROM run_task_health WHERE run_id=%s", (run,))
        )["blocked_reason"] == "waiting_settlement"


async def test_cart_state_cycles_do_not_keep_resetting_progress(pool):
    _, run, task, listing = await ready_shop(pool, supervision=POLICY)
    assert not await tick_health(pool, run, 1)
    await add(pool, run, task, listing)
    assert not await tick_health(pool, run, 2)
    for window in [3, 4, 5]:
        await add(pool, run, task, listing, quantity=0 if window % 2 else 1)
        stalled = await tick_health(pool, run, window)
    assert stalled
    assert (await Director(pool).get_run(run))["result"]["reason"] == "no_progress"


async def test_actual_model_node_transient_failure_preserves_task_counter(
    app, provision, test_database_url
):
    cluster, identities = await provision(count=1)
    task_id, session = uuid4(), uuid4()
    async with board_client(app, identities[0]["token"]) as client:
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(
                responses=[ProviderCallError("secret", retryable=True), AIMessage(content="Done.")]
            )
            agent = make_agent(runtime, client, cluster, identities[0], model, session)
            with pytest.raises(AgentTransientError) as error:
                await agent.run("Read the board.", task_id=task_id)
            assert "secret" not in str(error.value)
            assert await agent.run("Read the board.", task_id=task_id) == "Done."
            assert model.calls == 2


async def test_real_agent_transient_failure_after_board_commit_replays_one_operation(
    app, provision, test_database_url
):
    from test_deep_agent import call

    cluster, identities = await provision(count=1)
    task_id, session = uuid4(), uuid4()
    async with board_client(app, identities[0]["token"]) as client:
        async with AgentRuntime(test_database_url) as runtime:
            original = make_agent(
                runtime, client, cluster, identities[0], ScriptedModel(responses=[call()]), session
            )
            execute = original.board.execute

            async def lose_response(*args, **kwargs):
                await execute(*args, **kwargs)
                raise httpx.ReadTimeout("private transport details")

            original.board.execute = lose_response
            with pytest.raises(AgentTransientError):
                await original.run("Post one note.", task_id=task_id)
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(responses=[AIMessage(content="Recovered.")])
            restored = make_agent(runtime, client, cluster, identities[0], model, session)
            assert await restored.run("Post one note.", task_id=task_id) == "Recovered."
            assert model.calls == 1
            response = await client.get(f"clusters/{cluster['id']}/threads")
            assert len(response.json()["items"]) == 1
