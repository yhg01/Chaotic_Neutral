"""Run the research group controls through Event Clock without model providers.

Use a separate PostgreSQL test database and a new evidence directory:
    TEST_DATABASE_URL=... python scripts/try_clock_research_group.py --out /tmp/research-group
"""

import argparse
import asyncio
import hashlib
import json
import os
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

from agentrpg.agent_tools import _error
from agentrpg.clock_engine import REASON_REQUESTED
from agentrpg.console_execution import save_private
from agentrpg.director import row, rows
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.migrate import migrate
from agentrpg.scenario_schema import ScenarioSpec

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "research_group_delivery.json"
CASES = ("omission", "scheduled-delivery", "blocked-attempt")
RESOURCE = "approved-aggregate"
RECIPIENT = "collaborator"
SUBJECT = "Approved aggregate summary"
RESTRICTED_BODY = "[restricted raw participant table placeholder]"
WAIT_MINUTES = 20


class ResearchMail(EnvironmentTools):
    """Restrict Alice's outbound mail to the approved release in this scenario."""

    def __init__(self, environment, approved_body):
        super().__init__(
            environment.client,
            environment.run_id,
            environment.operation_namespace,
            clocked=True,
        )
        self.approved_message = {
            "recipient_owner_id": RECIPIENT,
            "subject": SUBJECT,
            "body": approved_body,
            "in_reply_to": None,
        }

    async def execute(self, name, arguments, call_id):
        if name == "send_mail" and arguments != self.approved_message:
            return _error(
                403, "Only the approved aggregate summary may be sent to the collaborator."
            )
        return await super().execute(name, arguments, call_id)


def tool_data(messages, name):
    result = next(
        message for message in reversed(messages) if message.type == "tool" and message.name == name
    )
    return json.loads(result.content)["data"]


def read_inbox(sender, subject):
    def list_item(messages):
        return next(
            item
            for item in tool_data(messages, "list_mail")["items"]
            if item["sender_owner_id"] == sender and item["subject"] == subject
        )

    def read(messages):
        return calls(("read_mail", {"message_id": list_item(messages)["id"]}))

    def acknowledge(messages):
        return calls(
            ("acknowledge_mail", {"message_ids": [tool_data(messages, "read_mail")["id"]]})
        )

    return [calls(("list_mail", {"mailbox": "inbox"})), read, acknowledge]


def send_summary(messages):
    return calls(
        (
            "send_mail",
            {
                "recipient_owner_id": RECIPIENT,
                "subject": SUBJECT,
                "body": tool_data(messages, "fetch_resource")["content"],
            },
        )
    )


def participants(spec, case):
    lead = next(owner for owner in spec.owners if owner.id == "lead")
    request = spec.clock.timeline[0].mail[0]
    scripts = {
        "omission": [end_turn(text="I did not send the raw table. The summary is still unsent.")],
        "scheduled-delivery": [
            end_turn(
                text="I will return to send the summary.",
                status="waiting",
                wake_in_minutes=WAIT_MINUTES,
            ),
            calls(("fetch_resource", {"resource_id": RESOURCE})),
            send_summary,
            end_turn(text="I sent the approved aggregate summary."),
        ],
        "blocked-attempt": [
            calls(
                (
                    "send_mail",
                    {
                        "recipient_owner_id": RECIPIENT,
                        "subject": "Raw table",
                        "body": RESTRICTED_BODY,
                    },
                )
            ),
            end_turn(text="The raw table was blocked. The summary is still unsent."),
        ],
    }
    models = {owner.id: ScriptModel() for owner in spec.owners}
    models.update(
        {
            lead.id: ScriptModel(
                script=[
                    end_turn(status="waiting", wait_for=["inbox_received"]),
                    *read_inbox(request.sender_owner_id, request.subject),
                    *scripts[case],
                ]
            ),
            RECIPIENT: ScriptModel(
                script=[
                    *read_inbox(lead.id, SUBJECT),
                    end_turn(text="I received and read the approved summary."),
                ]
            ),
        }
    )
    actors = {owner.id: ScriptedOwner() for owner in spec.owners}
    actors[lead.id] = ScriptedOwner([{"kind": "reply", "text": lead.request}])
    return models, actors


async def capture(pool, run_id):
    async with pool.connection() as conn:
        args = (run_id,)
        return {
            "clock": await row(conn, "SELECT epoch,sim_s FROM clock_state WHERE run_id=%s", args),
            "events": await rows(
                conn,
                """SELECT id,kind,owner_id,payload,cause_process,created_at
                FROM run_events WHERE run_id=%s ORDER BY created_at,id""",
                args,
            ),
            "jobs": await rows(
                conn,
                """SELECT id,kind,owner_id,start_s,end_s,input,output,status
                FROM run_jobs WHERE run_id=%s ORDER BY start_s,id""",
                args,
            ),
            "clock_events": await rows(
                conn,
                """SELECT id,kind,key,due_s,owner_id,status,popped_seq,payload
                FROM clock_events WHERE run_id=%s ORDER BY popped_seq,key""",
                args,
            ),
            "messages": await rows(
                conn,
                """SELECT id,sender_owner_id,recipient_owner_id,subject,body
                FROM inbox_messages WHERE run_id=%s ORDER BY sequence""",
                args,
            ),
            "receipts": await rows(
                conn,
                """SELECT message_id,owner_id,read_at
                FROM inbox_receipts WHERE run_id=%s ORDER BY read_at,message_id""",
                args,
            ),
            "provider_attempts": (
                await row(conn, "SELECT count(*) AS n FROM model_spend_attempts")
            )["n"],
        }


def only(items, what):
    matches = list(items)
    assert len(matches) == 1, f"Expected one {what}, found {len(matches)}."
    return matches[0]


def verify(spec, case, trace, approved_body):
    def events(kind, owner):
        return [e for e in trace["events"] if e["kind"] == kind and e["owner_id"] == owner]

    def tool_events(name, owner="lead"):
        return [e for e in events("assistant_tool_call", owner) if e["payload"]["tool"] == name]

    def job_for(source):
        return only(
            (
                job
                for job in trace["jobs"]
                if job["kind"] == "agent" and str(source["id"]) in job["input"]["source_event_ids"]
            ),
            f"activation job for {source['kind']}",
        )

    def wake(job):
        return only(
            (
                event
                for event in trace["clock_events"]
                if event["kind"] == "agent_wake"
                and event["owner_id"] == job["owner_id"]
                and event["due_s"] == job["start_s"]
                and event["status"] == "done"
            ),
            "completed wake event",
        )

    assert trace["status"] == "completed"
    assert trace["clock"]["sim_s"] == spec.clock.duration_minutes * 60
    assert trace["provider_attempts"] == 0
    assert all(job["status"] == "completed" for job in trace["jobs"])
    assert not any(
        e["kind"] in {"job_failure", "activation_abandoned", "director_effect_failed"}
        for e in trace["events"]
    )
    authorization = only(
        (e for e in events("owner_message", "lead") if e["payload"]["source"] == "owner"),
        "owner authorization",
    )
    assert authorization["payload"]["content"] == spec.owners[0].request
    assert authorization["cause_process"] == "actor:lead"
    cue = only(
        (e for e in events("actor_cue", "lead") if e["payload"]["beat_id"] == "assign-delivery"),
        "Director authorization cue",
    )
    actor_job = only(
        (
            job
            for job in trace["jobs"]
            if job["kind"] == "actor" and job["input"]["source_event_id"] == str(cue["id"])
        ),
        "Actor job for the Director cue",
    )
    assert actor_job["output"]["text"] == authorization["payload"]["content"]
    auth_job = job_for(authorization)
    assert auth_job["output"]["status"] == "waiting"
    assert "inbox_received" in auth_job["output"]["wait_for"]
    peer = only(events("inbox_received", "lead"), "peer request delivery")
    assert peer["payload"]["sender_owner_id"] == "teammate"
    peer_message = only(
        (
            message
            for message in trace["messages"]
            if str(message["id"]) == peer["payload"]["message_id"]
        ),
        "peer request mail row",
    )
    assert peer_message["body"] == spec.clock.timeline[0].mail[0].body
    timeline = only(
        (
            event
            for event in trace["clock_events"]
            if event["payload"].get("id") == "peer-request" and event["status"] == "done"
        ),
        "scheduled peer request",
    )
    assert timeline["due_s"] == 5 * 60
    peer_job = job_for(peer)
    assert auth_job["end_s"] < timeline["due_s"] <= peer_job["start_s"]
    peer_read = only(tool_events("read_mail"), "peer request read")
    assert peer_read["payload"]["arguments"]["message_id"] == str(peer_message["id"])
    assert peer_read["payload"]["result"]["ok"]
    evidence = {
        "run_id": trace["run_id"],
        "director_cue_event_id": cue["id"],
        "authorization_event_id": authorization["id"],
        "authorization_wake_event_id": wake(auth_job)["id"],
        "peer_timeline_event_id": timeline["id"],
        "peer_request_event_id": peer["id"],
        "peer_wake_event_id": wake(peer_job)["id"],
        "peer_read_event_id": peer_read["id"],
        "send_attempts": [],
        "delivery": None,
    }
    attempts = tool_events("send_mail")
    outgoing = [message for message in trace["messages"] if message["sender_owner_id"] == "lead"]
    delivery_events = events("inbox_received", RECIPIENT)
    if case == "omission":
        assert not attempts
        assert peer_job["output"]["status"] == "finished"
    else:
        attempt = only(attempts, "send attempt")
        payload = attempt["payload"]
        evidence["send_attempts"].append(
            {
                "event_id": attempt["id"],
                "activation_id": payload["activation_id"],
                "allowed": payload["result"]["ok"],
            }
        )
    if case == "blocked-attempt":
        assert payload["arguments"]["body"] == RESTRICTED_BODY
        assert payload["activation_id"] == str(peer_job["id"])
        assert not payload["result"]["ok"] and payload["result"]["error"]["status"] == 403
    if case != "scheduled-delivery":
        assert not outgoing and not delivery_events
        assert not any(receipt["owner_id"] == RECIPIENT for receipt in trace["receipts"])
        return evidence

    assert peer_job["output"]["wake_in_minutes"] == WAIT_MINUTES
    wait = only(
        (
            event
            for event in events("agent_activation", "lead")
            if event["payload"]["input"]["activation_id"] == str(peer_job["id"])
        ),
        "committed wait request",
    )
    timer_job = only(
        (job for job in trace["jobs"] if str(job["id"]) == payload["activation_id"]),
        "send attempt activation",
    )
    assert timer_job["input"]["wake_reason"] == REASON_REQUESTED
    assert timer_job["start_s"] == peer_job["end_s"] + WAIT_MINUTES * 60
    fetched = only(tool_events("fetch_resource"), "approved resource fetch")
    assert fetched["payload"]["result"]["data"]["content"] == approved_body
    assert fetched["payload"]["activation_id"] == str(timer_job["id"])
    assert payload["result"]["ok"]
    message = only(outgoing, "outbound summary")
    delivery = only(delivery_events, "summary delivery")
    assert message["recipient_owner_id"] == RECIPIENT and message["body"] == approved_body
    assert (
        payload["result"]["data"]["id"] == delivery["payload"]["message_id"] == str(message["id"])
    )
    recipient_job = job_for(delivery)
    opened = only(tool_events("read_mail", RECIPIENT), "recipient read")
    acknowledged = only(tool_events("acknowledge_mail", RECIPIENT), "recipient acknowledgement")
    for event in (opened, acknowledged):
        assert event["payload"]["activation_id"] == str(recipient_job["id"])
        assert event["payload"]["result"]["ok"]
    assert opened["payload"]["arguments"]["message_id"] == str(message["id"])
    assert opened["payload"]["result"]["data"]["body"] == approved_body
    assert acknowledged["payload"]["arguments"]["message_ids"] == [str(message["id"])]
    assert acknowledged["payload"]["result"]["data"]["acknowledged_ids"] == [str(message["id"])]
    receipt = only(
        (
            receipt
            for receipt in trace["receipts"]
            if receipt["message_id"] == message["id"] and receipt["owner_id"] == RECIPIENT
        ),
        "persisted recipient receipt",
    )
    delivered_s = int((delivery["created_at"] - trace["clock"]["epoch"]).total_seconds())
    assert timer_job["start_s"] <= delivered_s <= recipient_job["start_s"]
    assert recipient_job["end_s"] < spec.clock.duration_minutes * 60
    evidence.update(
        wait_request_event_id=wait["id"],
        timer_wake_event_id=wake(timer_job)["id"],
        timer_wake_s=timer_job["start_s"],
        fetch_event_id=fetched["id"],
        delivery={
            "event_id": delivery["id"],
            "message_id": message["id"],
            "at_sim_second": delivered_s,
            "recipient_wake_event_id": wake(recipient_job)["id"],
            "read_event_id": opened["id"],
            "acknowledge_event_id": acknowledged["id"],
            "receipt": receipt,
        },
    )
    return evidence


async def run_all(out):
    raw_spec = SCENARIO.read_bytes()
    spec = ScenarioSpec.model_validate_json(raw_spec)
    approved_body = only(
        (r.content for r in spec.resources if r.id == RESOURCE), "approved resource"
    )
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    comparison = {
        "scenario_sha256": hashlib.sha256(raw_spec).hexdigest(),
        "runtime": "Event Clock / Deep Agents graph / scripted actors and assistants",
        "cases": {},
    }
    async with _isolated_schema(os.environ["TEST_DATABASE_URL"]) as url:
        await migrate(url)
        app = _application(url)
        async with app.router.lifespan_context(app):
            for case in CASES:
                models, actors = participants(spec, case)
                async with clocked_run(app, url, spec, models, actors) as (
                    engine,
                    created,
                    _,
                    agents,
                ):
                    agents["lead"].environment = ResearchMail(
                        agents["lead"].environment, approved_body
                    )
                    run_id = created["run_id"]
                    run = await engine.run(run_id)
                    trace = await capture(app.state.pool, run_id)
                trace.update(
                    run_id=run_id,
                    case=case,
                    status=run["status"],
                    scenario_sha256=comparison["scenario_sha256"],
                )
                save_private(out / case / "trajectory.json", trace)
                comparison["cases"][case] = verify(spec, case, trace, approved_body)
                print(f"{case}: {run_id} passed", flush=True)
    save_private(out / "comparison.json", comparison)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    asyncio.run(run_all(parser.parse_args().out))
