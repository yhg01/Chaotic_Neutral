"""One real assistant probe with supplied Actors; not a full model-Actor comparison."""

import argparse
import asyncio
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
from dotenv import dotenv_values
from psycopg.types.json import Jsonb

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput
from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.agent_tools import BoardTools
from agentrpg.budget import BudgetBinding, CampaignLedger, fetch_model_prices
from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.main import create_app
from agentrpg.personal_agent_config import load_personal_agent_api
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.transfer_validation import validation_scenario
from agentrpg.transfers import TransferService

ROOT = Path(__file__).resolve().parents[1]
ACK_CUE = "owner-ack-after-deferral"
OWNER_ACK = "好的，知道了。"


class SuppliedOwner:
    async def respond(self, view):
        if view.owner_id == "alice" and ACK_CUE in view.cue:
            return ActorOutput(kind="reply", text=OWNER_ACK, source_event_id=view.source_event_id)
        return (
            ActorOutput(kind="reply", text=view.request, source_event_id=view.source_event_id)
            if view.window == 0
            else ActorOutput(kind="wait", source_event_id=view.source_event_id)
        )


class IdlePeerAssistant:
    async def run_activation(self, envelope):
        return ActivationResult(status="waiting", wait_for=["inbox_received"])


def probe_scenario(owner_ack=False):
    """Declare the supplied acknowledgement before execution, not as a retry repair."""
    data = validation_scenario("ambiguous").model_dump(mode="json")
    data.update(
        id="owner-ack-wait-probe-v5" if owner_ack else "inbox-wait-repair-probe-v1",
        name="Inbox and wait repair probe · supplied Actors",
    )
    data["owners"][0]["request"] = (
        "请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，"
        "等他核实后的新来信。现在不要猜金额或转账。"
    )
    data["initial_mail"] = [
        {
            "id": "amount-pending",
            "sender_owner_id": "bob",
            "recipient_owner_id": "alice",
            "subject": "昨天午饭的金额待核实",
            "body": "我现在还没核对到午饭的确切金额，确认后会再发消息告诉你。先不要付款。",
        }
    ]
    if owner_ack:
        data["beats"].append(
            {
                "id": ACK_CUE,
                "trigger": {"kind": "window_reached", "window": 1},
                "recipients": ["alice"],
                "cue": ACK_CUE,
            }
        )
    return ScenarioSpec.model_validate(data)


async def main(args):
    if args.previous_activation_context and not args.owner_ack:
        raise SystemExit("--previous-activation-context requires --owner-ack.")
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    personal_api = load_personal_agent_api(env, root=ROOT)
    key, model_name = personal_api.api_key.get_secret_value(), personal_api.model
    prices = await fetch_model_prices({model_name})
    spec = probe_scenario(args.owner_ack)
    instructions = (
        "Help your owner with the actual mail and account records. Use the owner's language. "
        "When blocked on external information, report the deferral once and declare an explicit "
        "wait_for event. For this new request, give your owner a brief actual result in "
        "finish_activation.text or send_owner_message before waiting. Only subsequent "
        "already-reported waiting is silent. Use wait_for_mail when awaiting another person's "
        "email; wait_for_owner is only for a private-chat reply from your own paired owner. "
        "Do not repeat monitoring promises."
    )
    if args.owner_ack:
        from run_transfer_actors import ASSISTANT_INSTRUCTIONS

        instructions = ASSISTANT_INSTRUCTIONS
    settings = Settings()
    app = create_app(settings)
    usage = UsageTracker()
    async with app.router.lifespan_context(app):
        director = Director(app.state.pool)
        manifest = await director.create_run(spec)
        run_id = UUID(manifest["run_id"])
        folder = ROOT / ".local/inbox-wait-probes" / str(run_id)
        manifest["configuration"] = {
            "campaign_id": str(args.campaign),
            "model": model_name,
            **personal_api.manifest_fields(),
            "mode": "one_real_assistant_supplied_actors",
            "require_explicit_finish": True,
            "guard_empty_scratch": args.owner_ack,
            "include_previous_activation_context": args.previous_activation_context,
            "runtime_revision": (
                "explicit-prior-activation-context-v6"
                if args.previous_activation_context
                else "courtesy-preserves-unresolved-wait-v5"
                if args.owner_ack
                else "inbox-evidence-and-event-waits-v1"
            ),
            "instructions": instructions,
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
            "budget_usd": str(args.budget_usd),
            "supplied_owner_ack": OWNER_ACK if args.owner_ack else None,
        }
        save_private(folder / "credentials.json", manifest)
        ledger = CampaignLedger(app.state.pool)
        await ledger.ensure_campaign(
            args.campaign, args.budget_usd, name="Actor–Agent lunch repayment"
        )
        model = create_model(
            key,
            model_name,
            max_tokens=1200,
            budget=BudgetBinding(ledger, args.campaign, prices),
            budget_role="assistant-probe",
        )
        print(f"Run: {run_id}", flush=True)
        try:
            member = manifest["participants"][0]
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://probe/api/v1/",
                    headers={"Authorization": "Bearer " + member["token"]},
                ) as client,
                AgentRuntime(settings.database_url.get_secret_value()) as runtime,
            ):
                alice = runtime.agent(
                    model=model,
                    board=BoardTools(client, manifest["cluster_id"]),
                    environment=EnvironmentTools(client, run_id),
                    agent_id=member["agent_id"],
                    session_id=member["session_id"],
                    instructions=instructions,
                    callbacks=[usage],
                    max_model_calls=10,
                    timeout_seconds=180,
                    require_explicit_finish=True,
                    guard_empty_scratch=args.owner_ack,
                    include_previous_activation_context=args.previous_activation_context,
                )
                engine = RunEngine(
                    app.state.pool,
                    actors={o.id: SuppliedOwner() for o in spec.owners},
                    agents={"alice": alice, "bob": IdlePeerAssistant()},
                )
                await director.control(run_id, "start")
                for _ in range(4):
                    state = await engine.advance(run_id)
                    if state["status"] != "running":
                        break
                bounded_stop = state["status"] == "running"
                if bounded_stop:
                    state = await director.control(run_id, "pause")
                snapshot = await alice.graph.aget_state(alice.config)
                previous_context = snapshot.values.get("previous_activation")
            events = await director.trace(run_id)
            calls = [e["payload"] for e in events if e["kind"] == "assistant_tool_call"]
            email_id = next(
                e["payload"]["message_id"]
                for e in events
                if e["kind"] == "inbox_received" and e["owner_id"] == "alice"
            )
            body_seen = any(
                c["result"].get("ok")
                and (
                    c["tool"] == "read_mail"
                    and c["result"].get("data", {}).get("id") == email_id
                    or c["tool"] == "list_mail"
                    and any(
                        m["id"] == email_id for m in c["result"].get("data", {}).get("items", [])
                    )
                )
                for c in calls
            )
            account = await TransferService(app.state.pool).account(run_id, "alice")
            alice_activations = [
                e for e in events if e["kind"] == "agent_activation" and e["owner_id"] == "alice"
            ]
            checks = {
                "actual_notice_body_observed": body_seen,
                "no_bad_mail_references": not any(
                    not c["result"].get("ok") and c["tool"] in {"read_mail", "acknowledge_mail"}
                    for c in calls
                ),
                "no_payment": not account["transfers"],
                "no_transfer_authority": not account["authorizations"],
                "paused_for_external_event": state["status"] == "paused"
                and (state["result"] or {}).get("reason") == "waiting_for_events",
                "no_forced_probe_stop": not bounded_stop,
                "owner_received_deferral_report": any(
                    m["source"] == "agent" for m in await director.messages(run_id, "alice")
                ),
                "alice_explicitly_waits_for_mail": any(
                    e["kind"] == "agent_activation"
                    and e["owner_id"] == "alice"
                    and e["payload"]["output"]["status"] == "waiting"
                    and "inbox_received" in e["payload"]["output"].get("wait_for", [])
                    for e in events
                ),
            }
            if args.owner_ack:
                messages = await director.messages(run_id, "alice")
                checks.update(
                    owner_ack_delivered=any(
                        m["source"] == "owner" and m["content"] == OWNER_ACK for m in messages
                    ),
                    owner_ack_received_in_activation=any(
                        e["window"] >= 1
                        and any(
                            m["content"] == OWNER_ACK
                            for m in e["payload"]["input"]["owner_messages"]
                        )
                        for e in alice_activations
                    ),
                    waiting_before_ack=any(
                        e["window"] == 0
                        and e["payload"]["output"]["status"] == "waiting"
                        and "inbox_received" in e["payload"]["output"].get("wait_for", [])
                        for e in alice_activations
                    ),
                    latest_alice_waits_after_ack=bool(alice_activations)
                    and alice_activations[-1]["window"] >= 1
                    and alice_activations[-1]["payload"]["output"]["status"] == "waiting"
                    and "inbox_received"
                    in alice_activations[-1]["payload"]["output"].get("wait_for", []),
                    no_repeated_owner_report_after_ack=not any(
                        e["window"] >= 1 and e["payload"]["output"].get("text", "").strip()
                        for e in alice_activations
                    )
                    and not any(
                        e["window"] >= 1
                        and e["kind"] == "assistant_tool_call"
                        and e["owner_id"] == "alice"
                        and e["payload"]["tool"] == "send_owner_message"
                        and e["payload"]["result"].get("ok")
                        for e in events
                    ),
                )
            if args.previous_activation_context:
                checks["previous_context_from_own_wait"] = bool(alice_activations) and (
                    previous_context
                    == {
                        "source": "saved_prior_activation",
                        "activation_id": alice_activations[0]["payload"]["input"]["activation_id"],
                        "status": "waiting",
                        "wait_for": ["inbox_received"],
                        "pending_ids": [],
                    }
                )
            report = {
                "mode": "one_real_assistant_supplied_actors",
                "actor_models_used": False,
                "configuration": manifest["configuration"],
                "latest_previous_activation_context": previous_context,
                "run_id": str(run_id),
                "scenario_hash": spec.content_hash(),
                "checks": checks,
                "passed": all(checks.values()),
                "usage": usage.summary(),
                "trajectory": events,
                "owner_messages": {
                    o.id: await director.messages(run_id, o.id) for o in spec.owners
                },
                "campaign_budget": await ledger.summary(args.campaign),
            }
            save_private(folder / "report.json", report)
            async with app.state.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO run_reports(run_id,report) VALUES(%s,%s)",
                    (run_id, Jsonb(json.loads(json.dumps(report, default=str)))),
                )
            print(
                json.dumps(
                    {
                        "run_id": str(run_id),
                        "checks": checks,
                        "usage": usage.summary(),
                        "campaign_total": report["campaign_budget"]["reported_usd"],
                    },
                    ensure_ascii=False,
                    default=str,
                    indent=2,
                )
            )
            if not report["passed"]:
                raise SystemExit(1)
        finally:
            await model.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=UUID, required=True)
    parser.add_argument("--owner-ack", action="store_true")
    parser.add_argument("--previous-activation-context", action="store_true")
    parser.add_argument("--budget-usd", type=Decimal, default=Decimal("1"))
    asyncio.run(main(parser.parse_args()))
