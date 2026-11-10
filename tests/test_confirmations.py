"""Receipt closeout, owner decisions, replay, and graph guards on real PostgreSQL."""

from contextlib import AsyncExitStack
from typing import Any
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from pydantic import ValidationError
from test_actor import Responses
from test_clock_runtime import keep_control, tool
from test_deep_agent import ScriptedModel

from agentrpg.activation import ActivationResult
from agentrpg.actor import (
    ActorContractError,
    ActorOutput,
    ModelActor,
    build_actor_view,
    validate_actor_output,
)
from agentrpg.agent_model import UsageTracker
from agentrpg.agent_tools import BoardTools
from agentrpg.clock_runtime import ClockRuntime
from agentrpg.clock_transfers import TimedTransfers
from agentrpg.community_experiment import DailyMeetingPlan
from agentrpg.confirmation_control import CONFIRMATION_INSTRUCTIONS, control_spec
from agentrpg.confirmation_schema import ConfirmationSpec
from agentrpg.confirmations import (
    ConfirmationError,
    actor_view,
    guard_freeform,
    guard_mail,
    required_actions,
    yield_error,
)
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, row, rows
from agentrpg.director_clock import DirectorClock
from agentrpg.environment_tools import EnvironmentTools


def contract_view(owner="alice"):
    spec, definitions = control_spec()
    task = dict(
        definitions[0],
        id=str(uuid4()),
        source_message_id=None,
        request_mail_id=None,
        question_message_id=None,
        decision=None,
    )
    return build_actor_view(spec, owner, "Current task", source_event_id="current").model_copy(
        update={"confirmations": {"enabled": True, "tasks": [task]}}
    )


def action_output(
    view, kind="request", text="Please relay my terms and wait for Bob's actual acknowledgement."
):
    return ActorOutput(
        kind="reply",
        text=text,
        source_event_id=view.source_event_id,
        confirmation_actions=[{"kind": kind, "task_id": view.confirmations["tasks"][0]["id"]}],
    )


def test_outgoing_owner_task_cannot_silently_disappear_or_change_identity():
    view = contract_view()
    validate_actor_output(view, action_output(view))
    for output in [
        ActorOutput(kind="wait", source_event_id="current"),
        action_output(view, "confirm"),
        action_output(view).model_copy(update={"confirmation_actions": ()}),
    ]:
        with pytest.raises(ActorContractError):
            validate_actor_output(view, output)


def test_peer_cannot_approve_before_question_or_approve_a_foreign_task():
    view = contract_view("bob")
    with pytest.raises(ActorContractError):
        validate_actor_output(view, action_output(view, "confirm"))
    task = dict(
        view.confirmations["tasks"][0],
        source_message_id=str(uuid4()),
        request_mail_id=str(uuid4()),
        question_message_id=str(uuid4()),
    )
    view = view.model_copy(update={"confirmations": {"enabled": True, "tasks": [task]}})
    for decision in ["confirm", "decline"]:
        validate_actor_output(
            view, action_output(view, decision, "I explicitly " + decision + " this exact request.")
        )
    output = action_output(view, "confirm")
    with pytest.raises(ActorContractError):
        validate_actor_output(
            view,
            output.model_copy(update={"confirmation_actions": output.confirmation_actions * 2}),
        )


@pytest.mark.parametrize(
    "field", ["confirmation_actions_match_dialogue", "confirmation_conditions_preserved"]
)
async def test_review_cannot_accept_omitted_condition_or_fabricated_decision(field):
    view = contract_view()
    output = action_output(view).model_dump(mode="json")
    review = {
        "accepted": True,
        "issues": [],
        "confirmation_actions_match_dialogue": True,
        "confirmation_conditions_preserved": True,
        field: False,
    }
    actor = ModelActor(Responses([output]), reviewer=Responses([review]), max_repairs=0)
    with pytest.raises(ActorContractError, match="repair allowance"):
        await actor.respond(view)
    assert actor.last_attempts[-1]["accepted"] is False


def test_spec_rejects_self_confirmation_and_extra_model_permissions():
    _, definitions = control_spec()
    with pytest.raises(ValidationError):
        ConfirmationSpec.model_validate(dict(definitions[0], peer_owner_id="alice"))
    with pytest.raises(ValidationError):
        ConfirmationSpec.model_validate(dict(definitions[0], bypass_owner=True))


class OwnerPolicy:
    def __init__(self, decision):
        self.decision = decision
        self.usage = UsageTracker()
        self.last_attempts = []
        self.started = False

    async def respond(self, view):
        self.usage.output_tokens += 10
        required = required_actions(view)
        if not required:
            if not self.started and view.owner_id == "bob" and self.decision == "confirm":
                self.started = True
                return ActorOutput(
                    kind="reply",
                    source_event_id=view.source_event_id,
                    text="Wait for Alice's request, then ask me for my decision before replying.",
                )
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)
        task = view.confirmations["tasks"][0]
        kind = "request" if view.owner_id == task["owner_id"] else self.decision
        return ActorOutput(
            kind="reply",
            source_event_id=view.source_event_id,
            text=f"I explicitly {kind} this exact request. {task['terms']} {task['condition']}",
            confirmation_actions=[{"task_id": task["id"], "kind": kind}],
        )


class EvidenceModel(ScriptedModel):
    pool: Any = None
    run: UUID | None = None

    async def _agenerate(self, messages, **kwargs):
        response = self.responses[0]
        if response.tool_calls and response.tool_calls[0]["name"] in {
            "read_mail",
            "acknowledge_mail",
        }:
            call = response.tool_calls[0]
            args = call["args"]
            marker = args.get("message_id") or args.get("message_ids", [None])[0]
            if marker in {"request_mail_id", "reply_mail_id"}:
                async with self.pool.connection() as conn:
                    task = await row(
                        conn, "SELECT * FROM confirmation_tasks WHERE run_id=%s", (self.run,)
                    )
                identity = str(task[marker])
                updated = (
                    {"message_id": identity}
                    if call["name"] == "read_mail"
                    else {"message_ids": [identity]}
                )
                self.responses[0] = response.model_copy(
                    update={"tool_calls": [dict(call, args=updated)]}
                )
        return await super()._agenerate(messages, **kwargs)


class DroppedReceipt(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.dropped = False

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if (
            not self.dropped
            and request.url.path.endswith("/confirmations/request")
            and response.is_success
        ):
            self.dropped = True
            await response.aclose()
            raise httpx.ReadError("Lost response after the native commit.", request=request)
        return response


@pytest.mark.parametrize("decision", ["confirm", "decline"])
async def test_real_clock_graph_requires_actual_peer_reply_body_and_reports_once(
    pool, app, test_database_url, decision
):
    spec, definitions = control_spec()
    manifest = await Director(pool).create_run(spec)
    run = UUID(manifest["run_id"])
    clock = DailyMeetingPlan(pairs=2, days=1).clock().model_copy(update={"horizon_seconds": 3600})
    await DirectorClock(pool).bind(run, clock)
    await TimedTransfers(pool).enable(run)
    task_id = str(uuid5(run, "confirmation:alice:meeting-back-to-14"))
    agents, models = {}, {}
    async with AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url))
        dropped_receipt = DroppedReceipt(app)
        for p in manifest["participants"]:
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=dropped_receipt
                    if p["owner_id"] == "alice"
                    else httpx.ASGITransport(app=app),
                    base_url="http://test/api/v1/",
                    headers={"Authorization": "Bearer " + p["token"]},
                )
            )
            model = models[p["owner_id"]] = EvidenceModel(pool=pool, run=run)
            agents[p["owner_id"]] = runtime.agent(
                model=model,
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(client, run, timed_transfers=True, confirmations=True),
                agent_id=p["agent_id"],
                session_id=p["session_id"],
                instructions=CONFIRMATION_INSTRUCTIONS,
                require_explicit_finish=True,
                include_previous_activation_context=True,
                max_model_calls=20,
            )
        request_mail = "request_mail_id"
        reply_mail = "reply_mail_id"
        args = {"task_id": task_id}
        models["alice"].responses = [
            tool("request_confirmation", args, "request"),
            tool("request_confirmation", args, "request-retry"),
            tool("report_confirmation", args, "premature-report"),
            tool(
                "finish_activation",
                {"status": "finished", "text": "It is already final."},
                "premature-finish",
            ),
            tool(
                "finish_activation",
                {"status": "waiting", "text": "", "wait_for": ["inbox_received"]},
                "waiting",
            ),
            tool("acknowledge_mail", {"message_ids": [reply_mail]}, "ack-before-body"),
            tool("report_confirmation", args, "unread-report"),
            tool("read_mail", {"message_id": reply_mail}, "read-real-reply"),
            tool("report_confirmation", args, "actual-report"),
            tool("report_confirmation", args, "report-retry"),
            tool("finish_activation", {"status": "finished", "text": ""}, "done"),
        ]
        models["bob"].responses = [
            *(
                [
                    tool(
                        "send_owner_message",
                        {"content": "I will wait for Alice."},
                        "standby-report",
                    ),
                    tool(
                        "finish_activation",
                        {"status": "finished", "text": "I will wait for Alice."},
                        "standby-false-finish",
                    ),
                    tool(
                        "finish_activation",
                        {"status": "waiting", "text": "", "wait_for": ["inbox_received"]},
                        "standby-wait",
                    ),
                ]
                if decision == "confirm"
                else []
            ),
            tool("read_mail", {"message_id": request_mail}, "read-request"),
            tool("acknowledge_mail", {"message_ids": [request_mail]}, "ack-request"),
            tool("reply_confirmation", args, "fabricated-consent"),
            tool("ask_confirmation_owner", args, "question"),
            tool("ask_confirmation_owner", args, "question-retry"),
            tool(
                "finish_activation",
                {"status": "waiting", "text": "", "wait_for": ["owner_message"]},
                "await-owner",
            ),
            tool("reply_confirmation", args, "reply"),
            tool("reply_confirmation", args, "reply-retry"),
            tool("report_confirmation", args, "peer-report"),
            tool("finish_activation", {"status": "finished", "text": ""}, "done"),
        ]
        conductor = ClockRuntime(
            pool, actors={o: OwnerPolicy(decision) for o in agents}, agents=agents
        )
        await conductor.enable(run, {"confirmations": definitions})
        await conductor.advance(run, max_actions=15)
        conductor = ClockRuntime(pool, actors=conductor.actors, agents=agents)
        await conductor.enable(run, {"confirmations": definitions})
        await conductor.advance(run, max_actions=300)
        async with pool.connection() as conn:
            task = await row(conn, "SELECT * FROM confirmation_tasks WHERE run_id=%s", (run,))
            mails = await rows(
                conn, "SELECT * FROM inbox_messages WHERE run_id=%s ORDER BY sequence", (run,)
            )
            messages = await rows(conn, "SELECT * FROM owner_messages WHERE run_id=%s", (run,))
        assert task["decision"] == decision
        assert dropped_receipt.dropped
        assert task["reply_body_read"] and task["owner_report_id"] and task["peer_report_id"]
        assert len(mails) == 2
        assert mails[1]["in_reply_to"] == mails[0]["id"] == task["request_mail_id"]
        assert definitions[0]["condition"] in mails[0]["body"]
        assert definitions[0]["condition"] in mails[1]["body"]
        assert len([m for m in messages if m["id"] == task["question_message_id"]]) == 1
        reports = [m for m in messages if m["owner_id"] == "alice" and m["source"] == "agent"]
        assert len(reports) == 1 and reports[0]["id"] == task["owner_report_id"]
        assert "It is already final" not in " ".join(m["content"] for m in messages)
        if decision == "confirm":
            assert "condition is now satisfied" in reports[0]["content"]
        else:
            assert "change is not final" in reports[0]["content"]
        for owner, model in models.items():
            assert not model.responses, (owner, len(model.responses))
            assert agents[owner].environment.confirmations
        await keep_control(pool, run, "confirmation-" + decision)


async def test_unrelated_old_mail_or_read_receipt_cannot_complete_and_private_views_stay_scoped(
    pool,
):
    # Use real persisted profile/task rows, never a fake database.
    spec, definitions = control_spec()
    manifest = await Director(pool).create_run(spec)
    run = UUID(manifest["run_id"])
    await DirectorClock(pool).bind(run, DailyMeetingPlan(pairs=2, days=1).clock())
    async with pool.connection() as conn:
        from psycopg.types.json import Jsonb

        from agentrpg.confirmations import initialize

        await conn.execute(
            "INSERT INTO clock_runtime_profiles VALUES (%s,%s,%s)", (run, Jsonb({}), "test")
        )
        await initialize(conn, run, definitions)
        assert not await actor_view(conn, run, "bob")
        assert await yield_error(
            conn, run, "bob", ActivationResult(status="finished", text="Standing by.")
        )
        assert not await yield_error(
            conn,
            run,
            "bob",
            ActivationResult(status="waiting", text="", wait_for=("inbox_received",)),
        )
        assert not await actor_view(conn, run, "third-person")
        alice = (await actor_view(conn, run, "alice"))["tasks"][0]
        assert await yield_error(
            conn, run, "alice", ActivationResult(status="finished", text="done")
        )
        with pytest.raises(ConfirmationError):
            await guard_freeform(conn, run, "alice")
        with pytest.raises(ConfirmationError):
            await guard_mail(conn, run, "bob", "alice")
        assert alice["reply_mail_id"] is None
        assert await actor_view(conn, uuid4(), "alice") == {}
