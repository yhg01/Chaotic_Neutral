"""Actor payment authority and the real Director/tool/receipt feedback loop."""

import json
from contextlib import AsyncExitStack
from uuid import UUID

import httpx
import pytest
from langchain_core.messages import AIMessage

from agentrpg.activation import ActivationResult
from agentrpg.actor import (
    ActorContractError,
    ActorOutput,
    ModelActor,
    build_actor_view,
    validate_actor_output,
)
from agentrpg.director import Director, row
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.run_engine import RunEngine
from agentrpg.transfer_actor_task import actor_report, actor_scenario
from agentrpg.transfers import TransferService

CONSENT = {"recipient_owner_id": "bob", "amount": 10, "memo": "昨天的午饭钱"}


def view(owner="alice", **kwargs):
    return build_actor_view(actor_scenario(), owner, "State your current need.", **kwargs)


@pytest.mark.parametrize(
    "changes",
    [
        {"recipient_owner_id": "alice"},
        {"recipient_owner_id": "unknown"},
        {"amount": 11},
    ],
)
def test_actor_transfer_limits(changes):
    v = view()
    output = ActorOutput(
        kind="reply",
        text="Please repay lunch.",
        source_event_id=v.source_event_id,
        transfer_consent={**CONSENT, **changes},
    )
    with pytest.raises(ActorContractError):
        validate_actor_output(v, output)


@pytest.mark.parametrize("used", [False, True])
def test_acknowledgement_cannot_renew_payment_even_with_new_memo(used):
    v = view(transfers={"authorizations": [{**CONSENT, "used": used}]})
    for memo in (CONSENT["memo"], "A different label for the same debt"):
        output = ActorOutput(
            kind="reply",
            text="As before, continue.",
            source_event_id=v.source_event_id,
            transfer_consent={**CONSENT, "memo": memo},
        )
        with pytest.raises(ActorContractError):
            validate_actor_output(v, output)
    assert validate_actor_output(v, ActorOutput(kind="wait", source_event_id=v.source_event_id))


async def test_model_actor_repairs_repeated_grant_to_null():
    class Responses:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, config=None):
            self.calls += 1
            return AIMessage(
                content=json.dumps(
                    {
                        "kind": "reply",
                        "text": "Thank you, my lunch repayment is complete.",
                        "transfer_consent": CONSENT if self.calls == 1 else None,
                    }
                )
            )

    model = Responses()
    actor = ModelActor(model)
    output = await actor.respond(view(transfers={"authorizations": [{**CONSENT, "used": True}]}))
    assert output.transfer_consent is None and model.calls == 2
    assert "already has an authorization" in actor.last_attempts[0]["reason"]


class TestOwner:
    __test__ = False

    def __init__(self, owner, alice_wait=False):
        self.owner = owner
        self.alice_wait = alice_wait
        self.views = []

    async def respond(self, v):
        self.views.append(v)
        if v.window == 0:
            return ActorOutput(
                kind="reply",
                source_event_id=v.source_event_id,
                text="Please repay Bob ten credits for lunch."
                if self.owner == "alice"
                else "Please check the lunch repayment when it arrives.",
                transfer_consent=CONSENT if self.owner == "alice" else None,
            )
        if v.window == 1:
            assert v.transfers["transfers"]
            if self.owner == "alice" and self.alice_wait:
                return ActorOutput(kind="wait", source_event_id=v.source_event_id)
            return ActorOutput(
                kind="reply",
                source_event_id=v.source_event_id,
                text="Thanks, the lunch repayment is recorded.",
            )
        return ActorOutput(kind="wait", source_event_id=v.source_event_id)


class TestAssistant:
    __test__ = False

    def __init__(self, owner, adapter):
        self.owner, self.adapter = owner, adapter

    async def run_activation(self, envelope):
        account = await self.adapter.execute(
            "inspect_transfer_account", {}, f"account-{envelope.window_id}"
        )
        assert account["ok"]
        if self.owner == "alice" and envelope.window_id == 0:
            grant = account["data"]["authorizations"][0]
            result = await self.adapter.execute(
                "transfer_credits",
                {
                    **CONSENT,
                    "authorization_id": grant["authorization_id"],
                },
                "payment",
            )
            assert result["ok"]
            notice = await self.adapter.execute(
                "send_mail",
                {
                    "recipient_owner_id": "bob",
                    "subject": "Lunch repaid",
                    "body": "I repaid the ten credits for yesterday's lunch.",
                },
                "notice",
            )
            assert notice["ok"]
        elif self.owner == "bob":
            result = await self.adapter.execute(
                "list_mail", {"mailbox": "inbox"}, f"read-{envelope.window_id}"
            )
            assert result["ok"]
        return ActivationResult(
            status="finished",
            text="The recorded account is checked." if envelope.window_id < 2 else "",
        )


@pytest.mark.parametrize("alice_wait", [False, True])
async def test_actor_agent_director_feedback_and_private_views(app, pool, alice_wait):
    director = Director(pool)
    manifest = await director.create_run(actor_scenario())
    run_id = UUID(manifest["run_id"])
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM transfer_grants"))["n"] == 0
        assert (await row(conn, "SELECT count(*) AS n FROM owner_messages"))["n"] == 0
    actors = {owner: TestOwner(owner, alice_wait) for owner in ("alice", "bob")}
    async with AsyncExitStack() as stack:
        agents = {}
        for member in manifest["participants"]:
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test/api/v1/",
                    headers={"Authorization": "Bearer " + member["token"]},
                )
            )
            agents[member["owner_id"]] = TestAssistant(
                member["owner_id"], EnvironmentTools(http, run_id)
            )
        engine = RunEngine(pool, actors=actors, agents=agents)
        await director.control(run_id, "start")
        for _ in range(6):
            state = await engine.advance(run_id)
            if state["status"] != "running":
                break
    assert state["status"] == "completed"
    account = await TransferService(pool).account(run_id, "alice")
    assert account["balance"] == 90 and len(account["transfers"]) == 1
    assert all(v.transfers["authorizations"] == [] for v in actors["bob"].views)
    assert any(v.transfers["transfers"] for v in actors["bob"].views)
    report = await actor_report(pool, run_id)
    assert report["checks"]["consent_came_from_actor"]
    assert report["checks"]["owner_messages_generated_by_actors"]
    assert report["checks"]["bob_actor_reacted_after_receipt"]
    assert report["checks"]["alice_actor_reacted_after_assistant"]
    # Stub assistants do not emit real graph tool events, so do not claim a model pass.
    assert not report["passed"]
