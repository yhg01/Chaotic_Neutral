"""Native PostgreSQL/private Actor cognition; all model choices are supplied."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError
from test_actor import Responses

from agentrpg.actor import ActorCandidate, ActorOutput, ActorView, ModelActor, _candidate_schema
from agentrpg.actor_memory import ActorMemoryStore, RememberingActor, rank_memories
from agentrpg.actor_memory_schema import (
    CognitionUpdate,
    DailyAgenda,
    MemoryProfile,
    validate_cognition,
)
from agentrpg.community_experiment import CommunityPlan
from agentrpg.director import Director, DirectorError, row, rows
from agentrpg.director_clock import DirectorClock, TaskOutput
from agentrpg.simulation_clock import ClockConflict, Schedule, Sleep, digest


async def prepared(pool, profile=None):
    plan = CommunityPlan()
    manifest = await Director(pool).create_run(plan.scene())
    run = UUID(manifest["run_id"])
    director = DirectorClock(pool)
    await director.bind(run, plan.clock())
    store = ActorMemoryStore(pool, profile)
    await store.enable(run)
    for p in plan.clock().processes:
        if p.id != "actor-alice":
            await director.control(
                run,
                uuid4(),
                Sleep(process_id=p.id, until_seconds=604801, interrupt_on=("private_cue",)),
            )
    job = await director.prepare(run, "actor-alice", uuid4())
    return store, director, run, ActorView.model_validate(job["input"]["actor_view"])


def update_for(context, activity="Read and plan my morning"):
    return CognitionUpdate.model_validate(
        {
            "thoughts": [
                {
                    "text": "I should consider today's own goals before making a new request.",
                    "evidence": ["request"],
                    "valid_for_seconds": 60,
                }
            ],
            "agendas": [
                {
                    "day": context["planning_days"][0],
                    "slots": [{"start_minute": 540, "end_minute": 600, "activity": activity}],
                }
            ],
        }
    )


def test_legacy_schema_and_serialized_output_are_unchanged_when_disabled():
    output = ActorOutput(kind="wait", source_event_id="original")
    assert "cognition" not in output.model_dump(mode="json")
    assert "cognition" not in ActorCandidate.model_fields
    with pytest.raises(ValueError, match="enabled"):
        validate_cognition(None, CognitionUpdate())


@pytest.mark.parametrize(
    "slots",
    [
        [{"start_minute": 60, "end_minute": 60, "activity": "zero duration"}],
        [
            {"start_minute": 60, "end_minute": 90, "activity": "first"},
            {"start_minute": 80, "end_minute": 100, "activity": "overlap"},
        ],
        [{"start_minute": -1, "end_minute": 10, "activity": "negative"}],
    ],
)
def test_agenda_rejects_invalid_durations_and_overlaps(slots):
    with pytest.raises(ValidationError):
        DailyAgenda.model_validate({"day": "2026-11-10", "slots": slots})


def test_retrieval_handles_empty_equal_expired_future_and_recency():
    p = MemoryProfile()
    assert rank_memories([], "", 100, p) == []

    def memory(identity, last, created=0, expires=200):
        return dict(
            id=identity,
            text="same words",
            kind="observation",
            importance=5,
            created_seconds=created,
            expires_seconds=expires,
            last_accessed_seconds=last,
        )

    data = [
        memory("old", 1),
        memory("new", 99),
        memory("expired", 99, expires=100),
        memory("future", 110, created=110),
    ]
    selected = rank_memories(data, "", 100, p)
    assert [m["id"] for m in selected] == ["new", "old"]
    assert rank_memories([memory("b", 99), memory("a", 99)], "same words", 100, p)[0]["id"] == "a"


async def test_exact_context_snapshot_concurrent_replay_scope_and_immutability(pool):
    store, director, run, view = await prepared(pool)
    a, b = await asyncio.gather(
        store.context(run, "alice", view), store.context(run, "alice", view)
    )
    assert a == b and a["context_hash"] == digest(a["context"])
    assert a == await ActorMemoryStore(pool).context(run, "alice", view)
    assert (
        "cognition"
        in _candidate_schema(view.model_copy(update={"actor_memory": a["context"]})).model_fields
    )
    assert "actor_memory" not in view.model_dump(mode="json")
    async with pool.connection() as conn:
        assert (
            await row(
                conn, "SELECT count(*) AS n FROM actor_memory_snapshots WHERE run_id=%s", (run,)
            )
        )["n"] == 1
        with pytest.raises(psycopg.Error, match="immutable"):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE actor_memory_snapshots SET context='{}' WHERE job_id=%s",
                    (UUID(view.source_event_id),),
                )
    with pytest.raises(DirectorError, match="own"):
        await store.context(run, "bob", view)
    with pytest.raises(DirectorError, match="exact"):
        await store.context(run, "alice", view.model_copy(update={"cue": "spoofed"}))
    with pytest.raises(ClockConflict, match="frozen"):
        await ActorMemoryStore(pool, MemoryProfile(selection_limit=1)).enable(run)


async def test_model_actor_and_reviewer_see_same_memory_with_no_extra_call(pool):
    store, director, run, view = await prepared(pool)
    context = (await store.context(run, "alice", view))["context"]
    model = Responses(
        [
            {
                "kind": "reply",
                "text": view.request,
                "cognition": update_for(context).model_dump(mode="json"),
            }
        ]
    )
    reviewer = Responses([{"accepted": True, "issues": []}])
    delegate = ModelActor(model, reviewer=reviewer, max_repairs=0)
    actor = RememberingActor(store, run, "alice", delegate)
    original = await actor.respond(view)
    restored = RememberingActor(ActorMemoryStore(pool), run, "alice", delegate)
    assert await restored.respond(view) == original
    assert len(model.calls) == len(reviewer.calls) == 1
    assert actor.model is model and actor.reviewer is reviewer
    assert json.loads(model.calls[0][1].content)["view"]["actor_memory"] == context
    assert json.loads(reviewer.calls[0][1].content)["view"]["actor_memory"] == context
    await store.synchronize(run, "alice")
    async with pool.connection() as conn:
        assert not await row(conn, "SELECT 1 FROM actor_agendas WHERE run_id=%s", (run,))
        assert not await row(
            conn, "SELECT 1 FROM actor_memories WHERE run_id=%s AND kind='thought'", (run,)
        )
        assert not await row(conn, "SELECT 1 FROM transfer_grants WHERE run_id=%s", (run,))
        assert not await row(conn, "SELECT 1 FROM model_spend_attempts WHERE run_id=%s", (run,))
    assert restored.last_attempts == delegate.last_attempts


@pytest.mark.parametrize("change", ["foreign_evidence", "foreign_supersedes", "past_day", "ttl"])
async def test_private_cognition_rejects_unsupported_evidence_and_expiry(pool, change):
    store, director, run, view = await prepared(pool)
    context = (await store.context(run, "alice", view))["context"]
    data = update_for(context).model_dump(mode="json")
    if change == "foreign_evidence":
        data["thoughts"][0]["evidence"] = ["message:" + str(uuid4())]
    elif change == "foreign_supersedes":
        data["thoughts"][0]["supersedes"] = str(uuid4())
    elif change == "past_day":
        data["agendas"][0]["day"] = "2026-11-09"
    else:
        data["thoughts"][0]["valid_for_seconds"] = 604800
    with pytest.raises(ValueError):
        validate_cognition(context, CognitionUpdate.model_validate(data))


async def next_input(director, run, view, seconds):
    async def supplied(packet):
        return TaskOutput(
            text="", output_tokens=1, until_seconds=604801, interrupt_on=("private_cue",)
        )

    await director.dispatch(run, UUID(view.source_event_id), supplied)
    await director.control(
        run,
        uuid4(),
        Schedule(
            event_key="cue-" + str(seconds),
            at_seconds=seconds,
            kind="private_cue",
            recipients=("actor-alice",),
            payload={"text": "Consider today's current plan."},
        ),
    )
    for _ in range(20):
        result = await director.step(run, uuid4())
        if result["now_seconds"] >= seconds:
            break
    job = await director.prepare(run, "actor-alice", uuid4())
    return ActorView.model_validate(job["input"]["actor_view"])


async def test_last_access_survives_store_recreation_and_expired_items_are_filtered(pool):
    store, director, run, view = await prepared(
        pool, MemoryProfile(selection_limit=20, observation_ttl_seconds=100)
    )
    original = (await store.context(run, "alice", view))["context"]
    old_ids = {m["id"] for m in original["memories"]}
    assert old_ids
    view = await next_input(director, run, view, 10)
    current = (await store.context(run, "alice", view))["context"]
    touched = old_ids & {m["id"] for m in current["memories"]}
    assert touched
    new = ActorMemoryStore(pool, store.profile)
    assert current == (await new.context(run, "alice", view))["context"]
    async with pool.connection() as conn:
        restored = await rows(
            conn, "SELECT id,last_accessed_seconds FROM actor_memories WHERE run_id=%s", (run,)
        )
    assert all(m["last_accessed_seconds"] == 10 for m in restored if str(m["id"]) in touched)
    view = await next_input(director, run, view, 110)
    later = (await new.context(run, "alice", view))["context"]
    assert not old_ids & {m["id"] for m in later["memories"]}
    assert all(m["created_seconds"] <= 110 < m["expires_seconds"] for m in later["memories"])


async def test_memory_is_identical_in_a_new_python_process(pool, test_database_url):
    store, director, run, view = await prepared(pool)
    saved = await store.context(run, "alice", view)
    worker = """
import asyncio,json,os,sys
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from agentrpg.actor import ActorView
from agentrpg.actor_memory import ActorMemoryStore
async def main():
 data=json.loads(sys.stdin.read())
 async with AsyncConnectionPool(os.environ['ACTOR_MEMORY_TEST_DATABASE_URL'],
   min_size=1,max_size=2,kwargs={'row_factory':dict_row}) as pool:
  view=ActorView.model_validate(data['view'])
  saved=await ActorMemoryStore(pool).context(data['run'],'alice',view)
  print(json.dumps({'context_hash':saved['context_hash'],'context':saved['context']}))
asyncio.run(main())
"""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OPENAI_", "OPENROUTER_", "PERSONAL_AGENT_", "ACTOR_REVIEWER_"))
        and k not in {"DATABASE_URL", "ADMIN_TOKEN"}
    }
    env["ACTOR_MEMORY_TEST_DATABASE_URL"] = test_database_url
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        worker,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await child.communicate(
        json.dumps({"run": str(run), "view": view.model_dump(mode="json")}).encode()
    )
    assert child.returncode == 0, stderr.decode()
    assert json.loads(stdout) == {
        "context_hash": saved["context_hash"],
        "context": saved["context"],
    }


async def test_lost_accepted_response_replays_review_and_tokens_without_new_generation(
    pool, monkeypatch
):
    store, director, run, view = await prepared(pool)
    context = (await store.context(run, "alice", view))["context"]

    class MeteredResponses(Responses):
        async def ainvoke(self, messages, config=None):
            result = await super().ainvoke(messages, config)
            config["callbacks"][0].output_tokens += 11  # supplied, not billed
            return result

    model = MeteredResponses(
        [
            {
                "kind": "reply",
                "text": view.request,
                "cognition": update_for(context).model_dump(mode="json"),
            }
        ]
    )
    reviewer = MeteredResponses([{"accepted": True, "issues": []}])
    delegate = ModelActor(model, reviewer=reviewer, max_repairs=0)
    original = store.accept

    async def lose_after_commit(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("Supplied response loss after private journal commit")

    monkeypatch.setattr(store, "accept", lose_after_commit)
    with pytest.raises(RuntimeError, match="response loss"):
        await RememberingActor(store, run, "alice", delegate).respond(view)
    replacement = ModelActor(Responses([]), reviewer=Responses([]), max_repairs=0)
    restored = RememberingActor(ActorMemoryStore(pool), run, "alice", replacement)
    output = await restored.respond(view)
    assert output.cognition and replacement.usage.output_tokens == 22
    assert replacement.last_attempts == delegate.last_attempts
    assert len(model.calls) == len(reviewer.calls) == 1
    assert not replacement.model.calls and not replacement.reviewer.calls


async def test_unknown_generation_is_not_retried_and_does_not_activate_plans(pool):
    from agentrpg.actor import ModelGenerationError

    store, director, run, view = await prepared(pool)
    model = Responses([RuntimeError("Supplied unobserved provider result")])
    actor = RememberingActor(store, run, "alice", ModelActor(model, max_repairs=0))

    async def execute(packet):
        await actor.respond(ActorView.model_validate(packet["actor_view"]))

    with pytest.raises(ModelGenerationError):
        await director.dispatch(run, UUID(view.source_event_id), execute)
    with pytest.raises(DirectorError, match="Unknown"):
        await actor.respond(view)
    assert len(model.calls) == 1
    await store.synchronize(run, "alice")
    async with pool.connection() as conn:
        assert not await row(conn, "SELECT 1 FROM actor_agendas WHERE run_id=%s", (run,))


async def test_seven_day_mixed_native_control_with_memory_recreation(
    pool, app, test_database_url, tmp_path, monkeypatch
):
    import test_medium_chaos as mixed

    original_owner, original_clock = mixed.MixedOwner, mixed.ClockRuntime
    conductors = []

    class PlanningOwner(original_owner):
        async def respond(self, view):
            assert view.actor_memory and view.actor_memory["as_of_seconds"] >= 0
            output = await super().respond(view)
            if view.actor_memory["planning_needed"] or any(
                o.kind == "private_cue" for o in view.observations
            ):
                return output.model_copy(
                    update={
                        "cognition": update_for(
                            view.actor_memory,
                            "Consider my own day's activities; no booking or payment is completed.",
                        )
                    }
                )
            return output

    class MemoryConductor(original_clock):
        def __init__(self, pool, *, actors, agents):
            if all(isinstance(a, RememberingActor) for a in actors.values()):
                actors = {
                    o: RememberingActor(
                        ActorMemoryStore(pool, a.store.profile), a.run_id, o, a.delegate
                    )
                    for o, a in actors.items()
                }
            self.actor_inputs = actors
            super().__init__(pool, actors=actors, agents=agents)
            conductors.append(self)

        async def enable(self, run, configuration):
            store = ActorMemoryStore(self.pool)
            await store.enable(run)
            wrapped = {o: RememberingActor(store, run, o, a) for o, a in self.actors.items()}
            self.actor_inputs.clear()
            self.actor_inputs.update(wrapped)
            self.actors = self.actor_inputs
            await super().enable(run, configuration)

    monkeypatch.setattr(mixed, "MixedOwner", PlanningOwner)
    monkeypatch.setattr(mixed, "ClockRuntime", MemoryConductor)
    await mixed.test_native_mixed_seven_day_free_control(
        pool, app, test_database_url, tmp_path, monkeypatch, "mixed-restart"
    )
    async with pool.connection() as conn:
        run = (await row(conn, "SELECT run_id FROM actor_memory_profiles"))["run_id"]
    store = ActorMemoryStore(pool)
    assert len(conductors) >= 2
    last = conductors[-1]
    disabled = original_clock(
        pool, actors={o: a.delegate for o, a in last.actors.items()}, agents=last.agents
    )
    with pytest.raises(ClockConflict, match="differ"):
        await disabled.advance(run, max_actions=1)
    directory = (
        Path(os.environ.get("CLOCK_CONTROL_OUTPUT", tmp_path)) / "actor-memory-seven-day" / str(run)
    )
    memory = await store.export(run, directory)
    from agentrpg.clock_evidence import export_clock_evidence

    await export_clock_evidence(pool, run, directory)
    async with pool.connection() as conn:
        contexts = await rows(conn, "SELECT * FROM actor_memory_snapshots WHERE run_id=%s", (run,))
        plans = await rows(conn, "SELECT * FROM actor_agendas WHERE run_id=%s", (run,))
        assert plans and {p["owner_id"] for p in plans} == {"alice", "bob", "charlie"}
        assert all(s["applied_seconds"] is not None for s in contexts)
        revisions = {}
        for p in plans:
            revisions[(p["owner_id"], p["day"])] = revisions.get((p["owner_id"], p["day"]), 0) + 1
        assert max(revisions.values()) > 1
        assert all(
            p["day"] in s["context"]["planning_days"]
            for s in contexts
            for p in s["context"]["agendas"]
        )
        assert all(
            m["created_seconds"] <= s["context"]["as_of_seconds"] < m["expires_seconds"]
            for s in contexts
            for m in s["context"]["memories"]
        )
        assert not await row(conn, "SELECT 1 FROM model_spend_attempts WHERE run_id=%s", (run,))
    assert memory["snapshots"] == len(contexts)
    (directory / "acceptance.json").write_text(
        json.dumps(
            {
                "passed": True,
                "simulated_seconds": 604800,
                "mixed_requests_checked": 14,
                "private_memory_snapshots": len(contexts),
                "agenda_versions": len(plans),
                "no_expired_or_future_memory_in_snapshots": True,
                "profile_persisted": True,
                "native_mixed_receipt_audit_passed": True,
                "provider_calls": 0,
            },
            indent=2,
        )
        + "\n"
    )
