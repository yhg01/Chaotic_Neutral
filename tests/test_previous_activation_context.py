"""Saved own activation context is visible across PostgreSQL checkpoint recovery."""

import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, SystemMessage
from test_deep_agent import ScriptedModel

from agentrpg.activation import ActivationEnvelope, OwnerMessage
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime, AgentRuntimeError, AgentTaskConflictError
from agentrpg.director import Director
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.transfer_actor_task import actor_scenario

MARKER = "SAVED PREVIOUS ACTIVATION\n"


def tool(name, arguments):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": str(uuid4())}],
    )


def wait(*, pending_ids=()):
    return tool(
        "finish_activation",
        {
            "status": "waiting",
            "wait_for": ["inbox_received"],
            "pending_ids": [str(value) for value in pending_ids],
            "text": "The amount is unknown; waiting for evidence.",
        },
    )


def finished():
    return tool("finish_activation", {"status": "finished", "text": ""})


def model_context(model):
    texts = [message.text for message in model.seen if isinstance(message, SystemMessage)]
    contexts = [
        json.loads(text.split(MARKER, 1)[1].split("\n", 1)[0]) for text in texts if MARKER in text
    ]
    assert len(contexts) <= 1
    return contexts[0] if contexts else None


def expected_context(envelope, *, status="waiting", pending_ids=()):
    return {
        "source": "saved_prior_activation",
        "activation_id": str(envelope.activation_id),
        "status": status,
        "wait_for": ["inbox_received"] if status == "waiting" else [],
        "pending_ids": [str(value) for value in pending_ids],
    }


def envelope(manifest, window, text="A new owner message.", member_index=0):
    message_id = uuid4()
    return ActivationEnvelope(
        run_id=UUID(manifest["run_id"]),
        participant_id=UUID(manifest["participants"][member_index]["agent_id"]),
        activation_id=uuid4(),
        window_id=window,
        source_event_ids=[message_id],
        owner_messages=[OwnerMessage(message_id=message_id, content=text)],
    )


@asynccontextmanager
async def participant_client(app, manifest, member_index=0):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer " + manifest["participants"][member_index]["token"]},
    ) as client:
        yield client


async def new_run(pool):
    director = Director(pool)
    manifest = await director.create_run(actor_scenario())
    await director.control(UUID(manifest["run_id"]), "start")
    return manifest


def make_agent(runtime, client, manifest, model, *, context=True, member_index=0):
    member = manifest["participants"][member_index]
    return runtime.agent(
        model=model,
        board=BoardTools(client, manifest["cluster_id"]),
        environment=EnvironmentTools(client, manifest["run_id"]),
        agent_id=member["agent_id"],
        session_id=member["session_id"],
        instructions="Help your owner with actual evidence and respect changed instructions.",
        require_explicit_finish=True,
        include_previous_activation_context=context,
    )


async def test_prior_result_is_frozen_and_visible_in_next_activation(app, pool, test_database_url):
    manifest = await new_run(pool)
    first, second = envelope(manifest, 0), envelope(manifest, 1, "Acknowledged.")
    pending_id = uuid4()
    model = ScriptedModel(responses=[wait(pending_ids=[pending_id]), wait()])
    async with (
        participant_client(app, manifest) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        agent = make_agent(runtime, client, manifest, model)
        await agent.run_activation(first)
        assert model_context(model) is None
        await agent.run_activation(second)
        expected = expected_context(first, pending_ids=[pending_id])
        assert model_context(model) == expected
        snapshot = await agent.graph.aget_state(agent.config)
        assert snapshot.values["previous_activation"] == expected
        assert snapshot.values["active_task_id"] == str(second.activation_id)
        assert "text" not in expected


async def test_runtime_restore_cancel_and_completed_replay_preserve_actual_predecessor(
    app, pool, test_database_url
):
    manifest = await new_run(pool)
    first = envelope(manifest, 0)
    cancelled = envelope(manifest, 1, "Cancel the task. Do not keep waiting or follow up.")
    third = envelope(manifest, 2, "No further work is needed.")
    async with participant_client(app, manifest) as client:
        async with AgentRuntime(test_database_url) as runtime:
            original = make_agent(runtime, client, manifest, ScriptedModel(responses=[wait()]))
            original_result = await original.run_activation(first)
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(responses=[finished(), finished()])
            restored = make_agent(runtime, client, manifest, model)
            result = await restored.run_activation(cancelled)
            assert result.status == "finished" and not result.wait_for
            assert model_context(model) == expected_context(first)
            assert await restored.run_activation(first) == original_result
            assert model.calls == 1
            await restored.run_activation(third)
            assert model_context(model) == expected_context(cancelled, status="finished")
            assert model.calls == 2


@pytest.mark.parametrize("enabled", [False, True])
async def test_context_flag_changes_conflict_with_an_existing_session(
    app, pool, test_database_url, enabled
):
    manifest = await new_run(pool)
    async with (
        participant_client(app, manifest) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        initial = make_agent(
            runtime, client, manifest, ScriptedModel(responses=[wait()]), context=enabled
        )
        await initial.run_activation(envelope(manifest, 0))
        model = ScriptedModel(responses=[finished()])
        replacement = make_agent(runtime, client, manifest, model, context=not enabled)
        with pytest.raises(AgentTaskConflictError, match="instructions"):
            await replacement.run_activation(envelope(manifest, 1))
        assert model.calls == 0


async def test_opt_in_and_legacy_peer_contexts_remain_separate(app, pool, test_database_url):
    manifest = await new_run(pool)
    async with (
        participant_client(app, manifest) as alice_client,
        participant_client(app, manifest, 1) as bob_client,
        AgentRuntime(test_database_url) as runtime,
    ):
        alice_model = ScriptedModel(responses=[wait(), wait()])
        bob_model = ScriptedModel(responses=[wait(), wait()])
        alice = make_agent(runtime, alice_client, manifest, alice_model)
        bob = make_agent(runtime, bob_client, manifest, bob_model, context=False, member_index=1)
        first_alice = envelope(manifest, 0)
        await alice.run_activation(first_alice)
        await bob.run_activation(envelope(manifest, 0, member_index=1))
        await alice.run_activation(envelope(manifest, 1))
        await bob.run_activation(envelope(manifest, 1, member_index=1))
        assert model_context(alice_model) == expected_context(first_alice)
        assert model_context(bob_model) is None
        bob_state = await bob.graph.aget_state(bob.config)
        assert bob_state.values["previous_activation"] is None


async def test_nonactivation_task_does_not_receive_or_manufacture_context(
    app, pool, test_database_url
):
    manifest = await new_run(pool)
    model = ScriptedModel(
        responses=[wait(), AIMessage(content="Operator task complete."), finished()]
    )
    async with (
        participant_client(app, manifest) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        agent = make_agent(runtime, client, manifest, model)
        await agent.run_activation(envelope(manifest, 0))
        assert (
            await agent.run("Stop the operator task.", task_id=uuid4()) == "Operator task complete."
        )
        assert model_context(model) is None
        state = await agent.graph.aget_state(agent.config)
        assert state.values["previous_activation"] is None
        await agent.run_activation(envelope(manifest, 1))
        assert model_context(model) is None


async def test_partial_checkpoint_retry_reuses_frozen_context_and_committed_write(
    app, pool, test_database_url
):
    manifest = await new_run(pool)
    first = envelope(manifest, 0)
    cancelled = envelope(manifest, 1, "Cancel the task; tell me once and stop.")
    async with participant_client(app, manifest) as client:
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel(
                responses=[
                    wait(),
                    tool("send_owner_message", {"content": "The task is cancelled."}),
                    RuntimeError("Private provider details must not enter saved errors."),
                ]
            )
            agent = make_agent(runtime, client, manifest, model)
            await agent.run_activation(first)
            with pytest.raises(AgentRuntimeError, match="model request failed"):
                await agent.run_activation(cancelled)
            frozen = expected_context(first)
            assert model_context(model) == frozen
            checkpoint = await agent.graph.aget_state(agent.config)
            assert checkpoint.values["previous_activation"] == frozen
            assert checkpoint.values["active_task_id"] == str(cancelled.activation_id)
        async with AgentRuntime(test_database_url) as runtime:
            resumed_model = ScriptedModel(responses=[finished()])
            resumed = make_agent(runtime, client, manifest, resumed_model)
            result = await resumed.run_activation(cancelled)
            assert result.status == "finished"
            assert model_context(resumed_model) == frozen
            messages = await Director(pool).messages(UUID(manifest["run_id"]), "alice")
            assert sum(m["content"] == "The task is cancelled." for m in messages) == 1
            assert await resumed.run_activation(cancelled) == result
            assert resumed_model.calls == 1


async def test_completed_graph_without_task_receipt_keeps_original_context(
    app, pool, test_database_url
):
    manifest = await new_run(pool)
    first = envelope(manifest, 0)
    cancelled = envelope(manifest, 1, "Cancel the task.")
    async with participant_client(app, manifest) as client:
        async with AgentRuntime(test_database_url) as runtime:
            agent = make_agent(
                runtime, client, manifest, ScriptedModel(responses=[wait(), finished()])
            )
            await agent.run_activation(first)
            result = await agent.run_activation(cancelled)
            # Reproduce a crash after the final graph checkpoint but before its
            # separate task receipt. Only this test's isolated session is changed.
            async with runtime.pool.connection() as connection:
                await connection.execute(
                    "UPDATE agent_tasks SET status='pending',result=NULL,completed_at=NULL "
                    "WHERE session_key=%s AND task_id=%s",
                    (agent.session_key, cancelled.activation_id),
                )
        async with AgentRuntime(test_database_url) as runtime:
            model = ScriptedModel()
            recovered = make_agent(runtime, client, manifest, model)
            assert await recovered.run_activation(cancelled) == result
            assert model.calls == 0
            snapshot = await recovered.graph.aget_state(recovered.config)
            assert snapshot.values["previous_activation"] == expected_context(first)
            assert snapshot.values["active_task_id"] == str(cancelled.activation_id)


@pytest.mark.parametrize("value", [None, 0, "true"])
async def test_context_flag_requires_a_boolean(app, pool, test_database_url, value):
    manifest = await new_run(pool)
    async with (
        participant_client(app, manifest) as client,
        AgentRuntime(test_database_url) as runtime,
    ):
        with pytest.raises(ValueError, match="must be a boolean"):
            make_agent(runtime, client, manifest, ScriptedModel(), context=value)
