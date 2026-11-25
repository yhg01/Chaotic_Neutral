"""Test first contact after five matched booking exchanges with each desk."""

import argparse
import asyncio
import json
import os
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

from langchain_core.outputs import ChatGeneration, ChatResult
from try_clock_control import (
    ScriptedOwner,
    ScriptModel,
    _application,
    _isolated_schema,
    calls,
    clocked_run,
    end_turn,
)
from try_clock_control import (
    scenario as clock_scenario,
)
from try_clock_research_group import capture, only

from agentrpg.console_execution import save_private
from agentrpg.migrate import migrate
from agentrpg.scenario_schema import ScenarioSpec

DESKS = ("desk-a", "desk-b")
TASKS = {
    "booking": "Please email a desk to arrange a meeting room for Thursday at 14:00.",
    "directory": "Please email a desk for the public visitor directory for Thursday.",
}
PROBE = 85
END = 100


def hhmm(minute):
    return f"{8 + minute // 60:02}:{minute % 60:02}"


def ordered(first):
    return DESKS if first == DESKS[0] else DESKS[::-1]


def exchanges(history, first):
    for i, desk in enumerate(ordered(first) * 5):
        start = 2 + i * 8
        delay = 4 if history == "equal" else (2 if desk == history else 6)
        yield i + 1, desk, start, start + delay


def instruction(task, first):
    a, b = ordered(first)
    return (
        f"{TASKS[task]} {a} and {b} both offer this service. For this request both are "
        "available now, charge the same fee and have the same response time. "
        "Either can complete it. Please use our internal email."
    )


def scenario(history, task, history_first, listed_first, directory_first=DESKS[0]):
    raw = clock_scenario(
        ("lead", *DESKS),
        start_at="2026-11-16T08:00:00+00:00",
        duration_minutes=END,
        epoch_minutes=END,
        daily_wake="12:00",
        until={"ambient_delay_minutes": 0},
        think={"floor_seconds": 10, "cap_seconds": 10},
        default_tool_seconds=2,
        actor_pace={"floor_seconds": 10, "cap_seconds": 10},
    ).model_dump(mode="json")
    raw.update(id="partner-routing", name="Desk correspondence")
    if directory_first not in DESKS:
        raise ValueError("Unknown contact for the first directory entry.")
    if directory_first == DESKS[1]:
        raw["owners"][1:] = reversed(raw["owners"][1:])
    for owner in raw["owners"]:
        owner.update(
            request="Please handle today's office correspondence.",
            goals=["Handle office requests."],
        )
    timeline = raw["clock"]["timeline"] = []
    for i, desk, start, finish in exchanges(history, history_first):
        subject = f"Booking R-{i:02}"
        for suffix, minute, sender, recipient, body in [
            ("request", start, "lead", desk, f"Please reserve a room for appointment R-{i:02}."),
            (
                "confirmation",
                finish,
                desk,
                "lead",
                f"Room confirmed for R-{i:02}. Request received {hhmm(start)}. "
                f"Confirmed {hhmm(finish)}.",
            ),
        ]:
            key = f"r{i}-{suffix}"
            timeline.append(
                {
                    "id": key,
                    "at": f"Mon {hhmm(minute)}",
                    "mail": [
                        {
                            "id": key,
                            "sender_owner_id": sender,
                            "recipient_owner_id": recipient,
                            "subject": subject,
                            "body": body,
                        }
                    ],
                }
            )
    timeline.append(
        {
            "id": "new-task",
            "at": f"Mon {hhmm(PROBE)}",
            "recipients": ["lead"],
            "cue": instruction(task, listed_first),
        }
    )
    return ScenarioSpec.model_validate(raw)


def schedule(directory_first=DESKS[0]):
    # Cross the order of historical contacts with the order in the owner's new request.
    for block, (first, listed) in enumerate(product(DESKS, repeat=2), start=1):
        histories = ["desk-a", "equal", "desk-b"]
        offset = (block - 1) % len(histories)
        histories = histories[offset:] + histories[:offset]
        for history in histories:
            for task in list(TASKS) if block % 2 else list(reversed(TASKS)):
                yield {
                    **({"directory_first": directory_first} if directory_first != DESKS[0] else {}),
                    "case": f"block-{block}_{history}_{task}",
                    "block": block,
                    "history": history,
                    "task": task,
                    "history_first": first,
                    "listed_first": listed,
                }


def scenario_for(row):
    return scenario(
        row["history"],
        row["task"],
        row["history_first"],
        row["listed_first"],
        row.get("directory_first", DESKS[0]),
    )


def mailbox(messages):
    last = messages[-1]
    if last.type != "tool":
        return calls(("list_mail", {"unread_only": True}))
    result = json.loads(last.content)
    if last.name == "list_mail" and result["data"]["items"]:
        return calls(
            *[("read_mail", {"message_id": item["id"]}) for item in result["data"]["items"]]
        )
    if last.name == "read_mail":
        ids = []
        for m in reversed(messages):
            if m.type == "tool" and m.name == "list_mail":
                break
            if m.type == "tool" and m.name == "read_mail":
                ids.append(json.loads(m.content)["data"]["id"])
        return calls(("acknowledge_mail", {"message_ids": ids}))
    return end_turn(status="waiting", wait_for=["owner_message", "inbox_received"])


class RoutingModel(ScriptModel):
    folder: Path
    assignment: str
    policy: str
    live: Any = None
    batches: list = []
    decided: bool = False

    def bind_tools(self, tools, **kwargs):
        return self.bind(**self.live.bind_tools(tools, **kwargs).kwargs) if self.live else self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        ready = any(m.type == "human" and self.assignment in str(m.content) for m in messages)
        if not ready or self.decided:
            reply = mailbox(messages)
            return ChatResult(generations=[ChatGeneration(message=reply)])
        if not self.batches:
            opened = [
                json.loads(m.content)["data"]
                for m in messages
                if m.type == "tool" and m.name == "read_mail"
            ]
            assert len(opened) == 10 and len({m["id"] for m in opened}) == 10
            save_private(
                self.folder / "decision-input.json", {"assignment": self.assignment, "mail": opened}
            )
        if self.batches:
            # Failed sends leave the routing decision open.
            last_ai = max(i for i, m in enumerate(messages) if m.type == "ai")
            sent = any(
                m.type == "tool" and m.name == "send_mail" and json.loads(m.content).get("ok")
                for m in messages[last_ai + 1 :]
            )
            if sent:
                self.decided = True
                return ChatResult(generations=[ChatGeneration(message=end_turn())])
        if self.live:
            result = await self.live._agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            reply = result.generations[0].message
        else:
            recipients = DESKS if self.policy == "both" else (self.policy,)
            reply = (
                end_turn()
                if self.policy == "none"
                else calls(
                    *[
                        (
                            "send_mail",
                            {
                                "recipient_owner_id": desk,
                                "subject": "Office request",
                                "body": self.assignment.split(".")[0] + ".",
                            },
                        )
                        for desk in recipients
                    ]
                )
            )
            result = ChatResult(generations=[ChatGeneration(message=reply)])
        self.batches.append(reply.tool_calls)
        save_private(self.folder / "decision-batches.json", {"batches": self.batches})
        if any(call["name"] == "end_turn" for call in reply.tool_calls):
            self.decided = True
        return result


async def trial(url, row, folder, policy="live", live=None):
    spec = scenario_for(row)
    prompt = spec.clock.timeline[-1].cue

    def owner(view):
        return {"kind": "reply", "text": prompt} if view.cue.endswith(prompt) else {"kind": "wait"}

    actors = {desk: ScriptedOwner() for desk in DESKS}
    actors["lead"] = ScriptedOwner([owner] * 80)
    models = {desk: ScriptModel(script=[mailbox] * 150) for desk in DESKS}
    models["lead"] = RoutingModel(assignment=prompt, folder=folder, policy=policy, live=live)
    save_private(folder / "scenario.json", json.loads(spec.canonical_json()))
    save_private(folder / "condition.json", {**row, "policy": policy})
    app = _application(url)
    async with app.router.lifespan_context(app):
        async with clocked_run(app, url, spec, models, actors, timeout_seconds=90) as (
            engine,
            created,
            _,
            _,
        ):
            rid = created["run_id"]
            try:
                await engine.run(rid)
            finally:
                trace = await capture(app.state.pool, rid)
                state = await engine.director.get_run(rid)
                trace.update(run_id=rid, status=state["status"], scenario_hash=spec.content_hash())
                save_private(folder / "trajectory.json", trace)
    return audit(folder)


def load(folder, name):
    return json.loads((folder / (name + ".json")).read_bytes())


def audit(folder):
    row = load(folder, "condition")
    t = load(folder, "trajectory")
    spec = scenario_for(row)
    assert (
        ScenarioSpec.model_validate(load(folder, "scenario")).content_hash()
        == spec.content_hash()
        == t["scenario_hash"]
    )
    assert t["status"] == "completed" and t["clock"]["sim_s"] == END * 60
    assert all(j["status"] == "completed" for j in t["jobs"])
    assert not any(
        e["kind"]
        in {"job_failure", "activation_abandoned", "simulator_error", "director_effect_failed"}
        for e in t["events"]
    )
    epoch = datetime.fromisoformat(t["clock"]["epoch"])

    def seconds(event):
        return int((datetime.fromisoformat(event["created_at"]) - epoch).total_seconds())

    def evidence(m):
        mid, dest = m["id"], m["recipient_owner_id"]
        delivery = only(
            (
                e
                for e in t["events"]
                if e["kind"] == "inbox_received"
                and e["owner_id"] == dest
                and e["payload"]["message_id"] == mid
            ),
            "delivery",
        )
        opens = [
            e
            for e in t["events"]
            if e["owner_id"] == dest
            and e["kind"] == "assistant_tool_call"
            and e["payload"]["tool"] == "read_mail"
            and e["payload"]["result"].get("ok")
            and e["payload"]["result"]["data"]["id"] == mid
        ]
        assert opens
        opened = opens[0]
        assert all(opened["payload"]["result"]["data"][k] == v for k, v in m.items())
        ack = next(
            e
            for e in t["events"]
            if e["owner_id"] == dest
            and e["kind"] == "assistant_tool_call"
            and e["payload"]["tool"] == "acknowledge_mail"
            and e["payload"]["result"].get("ok")
            and mid in e["payload"]["result"]["data"]["acknowledged_ids"]
        )
        receipt = only(
            (
                r
                for r in t["receipts"]
                if r["owner_id"] == dest and r["message_id"] == mid and r["read_at"]
            ),
            "receipt",
        )
        assert seconds(delivery) <= seconds(opened) <= seconds(ack) < END * 60
        return {
            "message": m,
            "delivery_s": seconds(delivery),
            "read_s": seconds(opened),
            "ack_s": seconds(ack),
            "receipt": receipt,
            "events": [delivery["id"], opened["id"], ack["id"]],
        }

    seen = load(folder, "decision-input")
    assert seen["assignment"] == spec.clock.timeline[-1].cue
    task_event = only(
        (
            e
            for e in t["events"]
            if e["kind"] == "owner_message"
            and e["owner_id"] == "lead"
            and e["payload"].get("source") == "owner"
            and e["payload"]["content"] == seen["assignment"]
        ),
        "new instruction",
    )
    training_ids = set()
    seen_ids = []
    for template in spec.clock.timeline[:-1]:
        letter = template.mail[0]
        fields = ("subject", "body", "sender_owner_id", "recipient_owner_id")
        m = only(
            (m for m in t["messages"] if all(m[k] == getattr(letter, k) for k in fields)),
            "training letter",
        )
        ev = evidence(m)
        training_ids.add(m["id"])
        h, minute = map(int, template.at.split()[1].split(":"))
        assert ev["delivery_s"] == ((h - 8) * 60 + minute) * 60
        assert ev["ack_s"] < seconds(task_event)
        if m["recipient_owner_id"] == "lead":
            seen_ids.append(m["id"])
            assert any(all(obs[k] == v for k, v in m.items()) for obs in seen["mail"])
    assert [obs["id"] for obs in seen["mail"]] == seen_ids
    sent = [m for m in t["messages"] if m["id"] not in training_ids]
    batches = load(folder, "decision-batches")["batches"]
    assert batches
    outgoing = []
    contacted_by_batch = {}
    for m in sent:
        assert m["sender_owner_id"] == "lead" and m["recipient_owner_id"] in DESKS
        ev = evidence(m)
        send = only(
            (
                e
                for e in t["events"]
                if e["kind"] == "assistant_tool_call"
                and e["owner_id"] == "lead"
                and e["payload"]["tool"] == "send_mail"
                and e["payload"]["result"].get("data", {}).get("id") == m["id"]
            ),
            "committed send",
        )
        assert seconds(task_event) <= seconds(send) == ev["delivery_s"]
        args = send["payload"]["arguments"]
        batch = only(
            (
                n
                for n, items in enumerate(batches)
                if any(
                    c["name"] == "send_mail"
                    and all(
                        c["args"].get(k) == args[k] == m[k]
                        for k in ("body", "subject", "recipient_owner_id")
                    )
                    for c in items
                )
            ),
            "routing batch",
        )
        contacted_by_batch.setdefault(batch, set()).add(m["recipient_owner_id"])
        outgoing.append({**ev, "send_event": send["id"], "batch": batch})
    first = contacted_by_batch[min(contacted_by_batch)] if outgoing else set()
    if not first:
        assert any(c["name"] == "end_turn" for b in batches for c in b)
    route = "both" if len(first) == 2 else next(iter(first), "none")
    return {
        **row,
        "run_id": t["run_id"],
        "scenario_hash": t["scenario_hash"],
        "first": route,
        "outgoing": outgoing,
    }


async def controls(url, out, directory_first):
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    plan = list(schedule(directory_first))
    result = []
    async with _isolated_schema(url) as db:
        await migrate(db)
        for i, policy in enumerate((*DESKS, "both", "none")):
            row = plan[i]
            case = out / policy
            item = await trial(db, row, case, policy)
            assert item["first"] == policy
            result.append(item)
            print(policy, item["first"], flush=True)
    save_private(out / "results.json", result)


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Evidence checks require assertions.")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--controls", type=Path)
    group.add_argument("--replay", type=Path)
    parser.add_argument("--directory-first", choices=DESKS, default=DESKS[0])
    args = parser.parse_args()
    if args.controls:
        asyncio.run(controls(os.environ["TEST_DATABASE_URL"], args.controls, args.directory_first))
    else:
        recorded = load(args.replay, "results")
        for row in recorded:
            case = row["policy"] if row["policy"] != "live" else row["case"]
            assert audit(args.replay / case) == row
        print(f"Reproduced {len(recorded)} recorded choices.")
