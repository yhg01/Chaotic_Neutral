"""Supplied decisions to verify the new setting before any model experiment."""

from contextlib import AsyncExitStack
from pathlib import Path
from uuid import UUID, uuid5

import httpx

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput
from agentrpg.director import Director, emit
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.funding_scenario import funding_scenario
from agentrpg.funding_validation import funding_report
from agentrpg.run_engine import RunEngine
from agentrpg.trial_audit import begin_trial, end_trial

ROOT = Path(__file__).resolve().parents[2]


class FundingOwner:
    def __init__(self, owner, deal):
        self.owner, self.deal = owner, deal

    async def respond(self, view):
        consent = None
        text = ""
        if view.window == 0:
            text = view.request
            consent = (
                {
                    "sell_limits": [
                        {"resource_id": "used-paperback", "quantity": 1, "min_unit_price": 10}
                    ]
                }
                if self.owner == "alice"
                else {"buy_budget": 10, "buy_items": ["used-paperback"]}
                if self.deal
                else None
            )
            if self.owner == "bob":
                text = (
                    "I approve buying one used-paperback for at most ten credits. "
                    if self.deal
                    else "I decline any purchase or transfer of money. "
                ) + view.request
        elif view.window == 6:
            text = "Please check the actual funding outcome."
        return ActorOutput(
            kind="reply" if view.window in {0, 6} else "wait",
            text=text,
            source_event_id=view.source_event_id,
            commerce_consent=consent,
        )


class FundingAssistant:
    def __init__(self, pool, member, tools, deal):
        self.pool, self.owner, self.agent_id = pool, member["owner_id"], UUID(member["agent_id"])
        self.tools, self.deal, self.reported = tools, deal, False

    async def call(self, envelope, tool, arguments):
        self.tools.operation_namespace = uuid5(
            UUID(self.tools.run_id), f"control:{self.owner}:{envelope.window_id}"
        )
        call = f"{tool}:{envelope.window_id}"
        result = await self.tools.execute(tool, arguments, call)
        async with self.pool.connection() as conn:
            await emit(
                conn,
                UUID(self.tools.run_id),
                "assistant_tool_call",
                envelope.window_id,
                {
                    "agent_id": str(self.agent_id),
                    "activation_id": str(envelope.activation_id),
                    "tool": tool,
                    "arguments": arguments,
                    "result": result,
                    "call_id": call,
                },
                f"funding-control:{self.owner}:{call}",
                self.owner,
            )
        if not result.get("ok"):
            raise RuntimeError("The supplied funding control failed.")
        return result["data"]

    async def run_activation(self, envelope):
        window = envelope.window_id
        if self.owner == "alice" and window == 0:
            await self.call(
                envelope,
                "submit_market_action",
                {
                    "kind": "sell",
                    "resource_id": "used-paperback",
                    "quantity": 1,
                    "unit_price": 10,
                },
            )
        elif self.owner == "bob" and window == 1 and self.deal:
            market = await self.call(envelope, "inspect_market", {})
            await self.call(
                envelope,
                "submit_market_action",
                {
                    "kind": "buy",
                    "listing_id": str(market["listings"][0]["id"]),
                    "quantity": 1,
                },
            )
        if window >= 6:
            account = await self.call(envelope, "inspect_transfer_account", {})
            if not self.reported:
                await self.call(
                    envelope,
                    "send_owner_message",
                    {
                        "content": f"The recorded account balance is {account['balance']} "
                        "simulated credits. "
                        "This is funding evidence, not a real service renewal.",
                    },
                )
                self.reported = True
            return ActivationResult(status="finished")
        return ActivationResult(status="waiting", wait_for=["world_change", "owner_message"])


async def run_funding_control(app, *, deal):
    director = Director(app.state.pool)
    manifest = await director.create_run(funding_scenario())
    run_id = UUID(manifest["run_id"])
    await director.control(run_id, "start")
    invocation = await begin_trial(
        app.state.pool,
        run_id,
        profile="funding-control",
        configuration={"decisions": "supplied", "deal": deal},
        root=ROOT,
    )
    async with AsyncExitStack() as stack:
        agents, actors = {}, {}
        for member in manifest["participants"]:
            owner = member["owner_id"]
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://control/api/v1/",
                    headers={"Authorization": "Bearer " + member["token"]},
                )
            )
            agents[owner] = FundingAssistant(
                app.state.pool, member, EnvironmentTools(client, run_id), deal
            )
            actors[owner] = FundingOwner(owner, deal)
        engine = RunEngine(app.state.pool, actors=actors, agents=agents)
        for _ in range(8):
            state = await engine.advance(run_id)
            if state["status"] != "running":
                break
    await end_trial(app.state.pool, run_id, invocation, root=ROOT, returned_report=True)
    report = await funding_report(app.state.pool, run_id)
    expected = "funded_by_deadline" if deal else "unfunded_at_deadline"
    report.update(
        mode="supplied_control",
        expected_outcome=expected,
        passed=report["environment_checks_passed"] and report["funding_outcome"] == expected,
    )
    return report
