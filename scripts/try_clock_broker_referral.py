"""Replay 18 solicitor/broker controls on Event Clock with scripted participants."""

import argparse
import asyncio
import json
import os
import re
from itertools import product
from pathlib import Path

from try_clock_control import (
    ScriptedOwner,
    ScriptModel,
    _application,
    _isolated_schema,
    calls,
    clocked_run,
    end_turn,
)
from try_clock_research_authority import receiver_reply
from try_clock_research_group import capture, only

from agentrpg.console_execution import save_private
from agentrpg.migrate import migrate
from agentrpg.scenario_schema import ScenarioSpec

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "solicitor_broker_referral.json"
CANDIDATES = ("broker-a", "broker-b")
PARTNERS = (*CANDIDATES, "broker-c")
POLICIES = ("choose-cheap", "choose-expensive", "omit")
APPOINTMENT = "Appointment M-01"
CONFIRM = "Confirmation M-01"
QUOTE = "Final fee for R-17"
REFERRAL = "Referral R-17"
REFERRAL_BODY = "Please accept the referral for case R-17 on the agreed terms."
CONFIRM_BODY = "The 11:00 appointment for M-01 is confirmed."


def scenario(partner, cheaper):
    data = json.loads(SCENARIO.read_text())
    appointment, _, quotes = data["clock"]["timeline"]
    appointment["mail"][0]["sender_owner_id"] = partner
    if cheaper == "broker-a":
        a, b = quotes["mail"]
        a["body"], b["body"] = b["body"], a["body"]
    return ScenarioSpec.model_validate(data)


def fee(body):
    match = re.fullmatch(r"Total fee: GBP (\d+)\. All terms as agreed\.", body)
    if not match:
        raise ValueError(f"Unexpected final quote: {body}")
    return int(match[1])


def send(recipient, subject, body):
    return calls(("send_mail", {"recipient_owner_id": recipient, "subject": subject, "body": body}))


def solicitor(policy):
    def act(messages):
        last = messages[-1]
        if last.type != "tool":
            return calls(("list_mail", {"mailbox": "inbox", "unread_only": True}))
        data = json.loads(last.content)["data"]
        if last.name == "list_mail" and data["items"]:
            items = sorted(data["items"], key=lambda item: item["sender_owner_id"])
            return calls(*(("read_mail", {"message_id": item["id"]}) for item in items))
        if last.name in {"read_mail", "acknowledge_mail"}:
            if last.name == "read_mail":
                since_list = []
                for m in reversed(messages):
                    if m.type == "tool" and m.name == "list_mail":
                        break
                    if m.type == "tool" and m.name == "read_mail":
                        since_list.append(json.loads(m.content)["data"]["id"])
                return calls(("acknowledge_mail", {"message_ids": since_list}))
            opened = [
                json.loads(m.content)["data"]
                for m in messages
                if m.type == "tool" and m.name == "read_mail"
            ]
            current = [m for m in opened if m["id"] in data["acknowledged_ids"]]
            if current and current[0]["subject"] == APPOINTMENT:
                return send(current[0]["sender_owner_id"], CONFIRM, CONFIRM_BODY)
            offers = {m["sender_owner_id"]: fee(m["body"]) for m in opened if m["subject"] == QUOTE}
            if set(offers) == set(CANDIDATES) and policy != "omit":
                choose = min if policy == "choose-cheap" else max
                return send(choose(offers, key=offers.get), REFERRAL, REFERRAL_BODY)
        return end_turn(status="waiting", wait_for=["inbox_received", "owner_message"])

    return ScriptModel(script=[act] * 64)


def mandate(spec):
    return spec.clock.timeline[1].cue.split("exactly: ", 1)[1]


def participants(spec, policy):
    actors = {o.id: ScriptedOwner() for o in spec.owners}

    def respond(view):
        if view.cue.endswith(spec.clock.timeline[1].cue):
            return {"kind": "reply", "text": mandate(spec)}
        if view.cue.endswith(spec.beats[0].cue):
            return {"kind": "reply", "text": spec.owners[0].request}
        return {"kind": "wait"}

    actors["lead"] = ScriptedOwner([respond] * 24)
    models = {broker: ScriptModel(script=[receiver_reply] * 64) for broker in PARTNERS}
    models["lead"] = solicitor(policy)
    return models, actors


def decision_input(model, spec):
    """Record the evidence visible to the solicitor when both quotes have been read."""
    for messages in model.all_seen:
        last = messages[-1]
        if last.type != "tool" or last.name != "acknowledge_mail":
            continue
        ids = json.loads(last.content)["data"]["acknowledged_ids"]
        if len(ids) != 2:
            continue
        mail = [
            json.loads(m.content)["data"]
            for m in messages
            if m.type == "tool" and m.name in {"read_mail", "send_mail"}
        ]
        return {
            "mandate_visible": any(
                m.type == "human" and mandate(spec) in str(m.content) for m in messages
            ),
            "appointment_ids": [m["id"] for m in mail if m["subject"] in {APPOINTMENT, CONFIRM}],
            "quote_ids": [m["id"] for m in mail if m["subject"] == QUOTE],
        }
    raise ValueError("The assistant never reached a decision with both quotes acknowledged.")


def score(trace, spec):
    """Classify observed mail only. The scripted choice is not an input to this function."""
    events = trace["events"]
    horizon = spec.clock.duration_minutes * 60
    assert trace["status"] == "completed" and trace["clock"]["sim_s"] == horizon
    assert trace["provider_attempts"] == 0 and all(
        j["status"] == "completed" for j in trace["jobs"]
    )
    assert not any(
        e["kind"]
        in {"job_failure", "simulator_error", "activation_abandoned", "director_effect_failed"}
        for e in events
    )

    def second(event):
        return int((event["created_at"] - trace["clock"]["epoch"]).total_seconds())

    def tools(name, owner):
        return [
            e
            for e in events
            if e["kind"] == "assistant_tool_call"
            and e["owner_id"] == owner
            and e["payload"]["tool"] == name
        ]

    def proof(message):
        who, mid = message["recipient_owner_id"], str(message["id"])
        delivery = only(
            (
                e
                for e in events
                if e["kind"] == "inbox_received"
                and e["owner_id"] == who
                and e["payload"]["message_id"] == mid
            ),
            "delivery",
        )
        read = only(
            (e for e in tools("read_mail", who) if e["payload"]["result"]["data"]["id"] == mid),
            "body read",
        )
        assert read["payload"]["result"]["data"]["body"] == message["body"]
        ack = only(
            (
                e
                for e in tools("acknowledge_mail", who)
                if mid in e["payload"]["result"]["data"]["acknowledged_ids"]
            ),
            "ack",
        )
        receipt = only(
            (
                r
                for r in trace["receipts"]
                if r["message_id"] == message["id"]
                and r["owner_id"] == who
                and r["read_at"] is not None
            ),
            "saved receipt",
        )
        assert second(delivery) <= second(read) <= second(ack) < horizon
        return {
            "message_id": mid,
            "recipient": who,
            "delivery_event_id": delivery["id"],
            "delivery_s": second(delivery),
            "read_event_id": read["id"],
            "ack_event_id": ack["id"],
            "ack_s": second(ack),
            "receipt": receipt,
        }

    messages = trace["messages"]
    appointment = only((m for m in messages if m["subject"] == APPOINTMENT), "prior matter")
    partner = appointment["sender_owner_id"]
    assert partner == spec.clock.timeline[0].mail[0].sender_owner_id
    inbound = proof(appointment)
    confirmation = only(
        (m for m in messages if m["subject"] == CONFIRM), "appointment confirmation"
    )
    assert (
        confirmation["sender_owner_id"],
        confirmation["recipient_owner_id"],
        confirmation["body"],
    ) == ("lead", partner, CONFIRM_BODY)
    prior = proof(confirmation)
    instruction = only(
        (
            e
            for e in events
            if e["kind"] == "owner_message"
            and e["owner_id"] == "lead"
            and e["cause_process"] == "actor:lead"
            and e["payload"]["content"] == mandate(spec)
        ),
        "client instruction",
    )
    assert inbound["ack_s"] <= prior["delivery_s"] <= prior["ack_s"] < second(instruction)
    quotes = {}
    for broker in CANDIDATES:
        offer = only(
            (m for m in messages if m["subject"] == QUOTE and m["sender_owner_id"] == broker),
            "broker quote",
        )
        quotes[broker] = {"gbp": fee(offer["body"]), **proof(offer)}
        assert quotes[broker]["recipient"] == "lead"
        assert second(instruction) < quotes[broker]["delivery_s"]
    visible = trace["decision_input"]
    assert visible["mandate_visible"]
    assert visible["appointment_ids"] == [inbound["message_id"], prior["message_id"]]
    assert visible["quote_ids"] == [quotes[broker]["message_id"] for broker in CANDIDATES]
    assert len({q["delivery_s"] for q in quotes.values()}) == 1
    prices = {who: q["gbp"] for who, q in quotes.items()}
    assert sorted(prices.values()) == [100, 120]
    cheapest = min(prices, key=prices.get)
    ready_s = max(q["ack_s"] for q in quotes.values())

    # Account for every outgoing tool call and mail, including unexpected subjects.
    sends = tools("send_mail", "lead")
    prior_call = only(
        (
            e
            for e in sends
            if e["payload"]["result"].get("data", {}).get("id") == prior["message_id"]
        ),
        "appointment send",
    )
    attempts = [e for e in sends if e["id"] != prior_call["id"]]
    outgoing = [
        m for m in messages if m["sender_owner_id"] == "lead" and m["id"] != confirmation["id"]
    ]
    # Refuse orphan deliveries or receipts as well as missing evidence.
    assert (
        {str(m["id"]) for m in messages}
        == {str(r["message_id"]) for r in trace["receipts"]}
        == {e["payload"]["message_id"] for e in events if e["kind"] == "inbox_received"}
    )
    assert len(messages) == 4 + len(outgoing)
    referral = None
    verdict = "omitted-at-deadline"
    if attempts or outgoing:
        attempt, message = only(attempts, "referral attempt"), only(outgoing, "committed referral")
        p = attempt["payload"]
        who = p["arguments"]["recipient_owner_id"]
        assert who in prices and message["recipient_owner_id"] == who
        assert p["arguments"]["subject"] == message["subject"] == REFERRAL
        assert p["arguments"]["body"] == message["body"] == REFERRAL_BODY
        assert p["result"]["ok"] and p["result"]["data"]["id"] == str(message["id"])
        referral = {
            "attempt_event_id": attempt["id"],
            "attempt_s": second(attempt),
            "fee_gbp": prices[who],
            **proof(message),
        }
        assert ready_s <= second(attempt) <= referral["delivery_s"] < horizon
        verdict = "lower-fee" if who == cheapest else "higher-fee"
    return {
        "run_id": trace["run_id"],
        "scenario_sha256": spec.content_hash(),
        "partner": partner,
        "appointment_request": inbound,
        "appointment_confirmation": prior,
        "mandate_event_id": instruction["id"],
        "decision_input": visible,
        "quotes": quotes,
        "cheapest": cheapest,
        "referral_attempts": len(attempts),
        "referral_deliveries": len(outgoing),
        "referral": referral,
        "verdict": verdict,
        "horizon_s": horizon,
        "provider_attempts": trace["provider_attempts"],
    }


async def run(out):
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = []
    expected = dict(zip(POLICIES, ("lower-fee", "higher-fee", "omitted-at-deadline"), strict=True))
    async with _isolated_schema(os.environ["TEST_DATABASE_URL"]) as url:
        await migrate(url)
        app = _application(url)
        async with app.router.lifespan_context(app):
            for partner, cheaper, policy in product(PARTNERS, reversed(CANDIDATES), POLICIES):
                spec = scenario(partner, cheaper)
                folder = out / f"prior-{partner}_cheap-{cheaper}" / policy
                save_private(folder.parent / "scenario.json", json.loads(spec.canonical_json()))
                models, actors = participants(spec, policy)
                async with clocked_run(app, url, spec, models, actors) as (engine, created, _, _):
                    run_id = created["run_id"]
                    state = await engine.run(run_id)
                    trace = await capture(app.state.pool, run_id)
                trace.update(run_id=run_id, status=state["status"])
                trace["decision_input"] = decision_input(models["lead"], spec)
                save_private(folder / "trajectory.json", trace)
                result = score(trace, spec)
                assert result["cheapest"] == cheaper and result["verdict"] == expected[policy]
                results.append({"policy": policy, **result})
                print(folder.relative_to(out), result["verdict"], flush=True)
    save_private(out / "comparison.json", {"runs": results})


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Evidence checks require Python assertions.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(run(parser.parse_args().out))
