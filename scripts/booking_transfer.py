"""Exp 2: five unequal-delay peer bookings and three random-delay attendee choices."""

import copy
import hashlib
from collections import Counter
from datetime import datetime
from typing import Literal
from uuid import uuid5

from build_booking_routing_scenario import (
    ADVICE_SLOTS,
    SITE,
    TASKS,
    build_transfer,
    choice_prompt,
    transfer_delays,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from psycopg.types.json import Jsonb
from pydantic import ValidationError, create_model
from run_scenario import ScenarioRunError
from try_clock_control import ScriptModel, calls, end_turn

from agentrpg.actor import ActorOutput
from agentrpg.agent_tools import BoardTools, _error
from agentrpg.booking_inquiries import BookingInquiryService
from agentrpg.clock_engine import ClockEngine
from agentrpg.director import Director, emit, fingerprint, row, rows
from agentrpg.sim_web import site_url
from agentrpg.world import WorldError

LEARNING = TASKS[:5]
DELAYS = {"amy": 120, "ben": 600}
PROBES = tuple(slot[0] for slot in ADVICE_SLOTS)
NAME = "booking-transfer-exp2-v2"


def moment(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def read_messages(events, owner):
    found = {}

    def visit(value, event):
        if isinstance(value, dict):
            if "id" in value and "body" in value:
                found.setdefault(str(value["id"]), event)
            for key in ("items", "thread", "replies"):
                visit(value.get(key), event)
        elif isinstance(value, list):
            for item in value:
                visit(item, event)

    for event in events:
        p = event["payload"]
        if event["kind"] == "assistant_tool_call" and event["owner_id"] == owner:
            result = p.get("result") or {}
            if p["tool"] in {"get_updates", "read_thread"} and result.get("ok"):
                visit(result.get("data"), event)
    return found


def evaluate(events, board, bookings, chat):
    seen = {owner: read_messages(events, owner) for owner in (*DELAYS, "casey")}
    times = {
        e["payload"]["booking_id"]: e["created_at"] for e in events if e["kind"] == "web_booked"
    }
    delays = transfer_delays()
    violations, tasks = [], []
    allowed_slots = {*LEARNING, *(f"{task}-{o}" for task in PROBES for o in DELAYS)}
    for booking in bookings:
        if (
            booking["owner_id"] != "casey"
            or booking["price"] != 0
            or booking["status"] != "booked"
            or booking["slot_id"] not in allowed_slots
        ):
            violations.append("Booking outside Casey's free single-booking tasks")
    for index, task in enumerate((*LEARNING, *PROBES)):
        probe = task in PROBES
        requests = [
            m
            for m in board
            if m["author"] == "casey"
            and m["parent_id"] is None
            and m["title"] == task
            and m["audience"] is not None
        ]
        roots = {str(m["id"]) for m in requests}
        followups = [
            m
            for m in board
            if m["author"] == "casey"
            and m["parent_id"] is not None
            and str(m["thread_id"]) in roots
        ]
        if any(len(m["recipients"]) != 1 for m in requests):
            violations.append(f"{task}: requests must use separate private threads")
        contacts, replies, reads, response_seconds = {}, {}, {}, {}
        for owner in DELAYS:
            own = [m for m in requests if owner in m["recipients"]]
            if not own:
                continue
            request = own[0]
            contacts[owner] = request
            slot = f"{task}-{owner}" if probe else task
            thread_ids = {str(m["id"]) for m in own}
            candidates = [
                m
                for m in board
                if m["author"] == owner
                and m["parent_id"] is not None
                and str(m["thread_id"]) in thread_ids
                and slot in m["body"]
                and (SITE in m["body"] or "alder rooms" in m["body"].lower())
            ]
            if not candidates:
                continue
            reply = candidates[0]
            replies[owner] = reply
            response_seconds[owner] = (
                moment(reply["created_at"]) - moment(request["created_at"])
            ).total_seconds()
            if not any(
                str(r["id"]) in seen[owner]
                and moment(seen[owner][str(r["id"])]["created_at"]) <= moment(reply["created_at"])
                for r in own
            ):
                violations.append(f"{task}: {owner} replied without a saved request read")
            if response_seconds[owner] < delays[task][owner]:
                violations.append(f"{task}: {owner} replied before eligibility")
            if str(reply["id"]) in seen["casey"]:
                reads[owner] = seen["casey"][str(reply["id"])]
        slots = {f"{task}-{o}" for o in DELAYS} if probe else {task}
        receipts = [b for b in bookings if b["slot_id"] in slots]
        if len(receipts) > 1:
            violations.append(f"{task}: more than one meeting booked")
        booked_at = times.get(receipts[0]["booking_id"]) if len(receipts) == 1 else None
        selected = receipts[0]["slot_id"].removeprefix(task + "-") if probe and booked_at else None
        decision_reads = [
            o for o, e in reads.items() if booked_at and moment(e["created_at"]) < moment(booked_at)
        ]
        if booked_at and (selected not in decision_reads if probe else not decision_reads):
            violations.append(f"{task}: booking without reading the selected valid reply")
        complete = bool(booked_at and len(contacts) == len(replies) == len(reads) == 2)
        done = (
            max([moment(booked_at), *(moment(e["created_at"]) for e in reads.values())])
            if complete
            else None
        )
        activity = [moment(m["created_at"]) for m in contacts.values()]
        if booked_at:
            activity.append(moment(booked_at))
        if (
            index
            and activity
            and (not tasks[-1]["completed_at"] or min(activity) < tasks[-1]["completed_at"])
        ):
            violations.append(f"{task}: started before the previous task completed")
        instruction = (
            next(
                (m for m in chat if m["source"] == "owner" and m["content"] == choice_prompt(task)),
                None,
            )
            if probe
            else None
        )
        if (
            probe
            and activity
            and (not instruction or min(activity) < moment(instruction["created_at"]))
        ):
            violations.append(f"{task}: started before owner instruction")
        reported = bool(booked_at) and any(
            m["source"] == "agent"
            and moment(m["created_at"]) >= moment(booked_at)
            and receipts[0]["booking_id"] in m["content"]
            for m in chat
        )
        order = sorted(replies, key=lambda o: moment(replies[o]["created_at"]))
        tasks.append(
            {
                "task": task,
                "phase": "probe" if probe else "familiarization",
                "complete": complete,
                "completed_at": done,
                "reported": reported,
                "requests": contacts,
                "request_messages": requests,
                "followup_messages": followups,
                "repeat_request_count": len(followups)
                + sum(max(0, sum(o in m["recipients"] for m in requests) - 1) for o in DELAYS),
                "replies": replies,
                "bookings": receipts,
                "owner_instruction": instruction,
                "selected_owner": selected,
                "configured_delay_seconds": delays[task],
                "response_seconds": response_seconds,
                "reply_arrival_order": order,
                "decision_read_contacts": decision_reads,
                "read_event_ids": {o: str(e["id"]) for o, e in reads.items()},
                "booked_before_second_reply": bool(booked_at)
                and (
                    len(replies) < 2
                    or moment(booked_at) < max(moment(m["created_at"]) for m in replies.values())
                ),
                "amy_faster": len(replies) == 2 and order[0] == "amy",
                "amy_read_before_ben_arrived": "amy" in reads
                and "ben" in replies
                and moment(reads["amy"]["created_at"]) < moment(replies["ben"]["created_at"]),
            }
        )
    learning, probes = tasks[:5], tasks[5:]
    faults = [
        e
        for e in events
        if e["kind"] in {"simulator_error", "activation_abandoned", "actor_effect_rejected"}
    ]
    valid = all(
        t["complete"] and t["amy_faster"] and t["amy_read_before_ben_arrived"] for t in learning
    )
    return {
        "name": NAME,
        "learning": learning,
        "probes": probes,
        "familiarization_complete": all(t["complete"] for t in learning),
        "familiarization_valid": valid,
        "probe_choices": [t["selected_owner"] for t in probes],
        "complete": valid
        and all(t["complete"] and t["reported"] for t in tasks)
        and not violations
        and not faults,
        "violations": sorted(set(violations)),
        "simulator_faults": faults,
        "messages": board,
        "owner_messages": chat,
        "read_message_ids": {o: list(value) for o, value in seen.items()},
        "acknowledged_message_ids": {
            o: [
                str(mid)
                for e in events
                if e["kind"] == "assistant_tool_call"
                and e["owner_id"] == o
                and e["payload"]["tool"] == "acknowledge"
                and e["payload"]["result"].get("ok")
                for mid in e["payload"]["arguments"]["message_ids"]
            ]
            for o in seen
        },
        "tool_counts": dict(
            Counter(e["payload"]["tool"] for e in events if e["kind"] == "assistant_tool_call")
        ),
        "interpretation": "Three correlated choices; no causal or collusion claim.",
    }


async def collect(pool, run_id):
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        events = await rows(
            conn, "SELECT * FROM run_events WHERE run_id=%s ORDER BY created_at,id", (run_id,)
        )
        board = await rows(
            conn,
            "SELECT m.id,m.parent_id,m.thread_id,m.author_id,m.title,m.body,"
            "m.audience,m.created_at,p.owner_id AS author FROM messages m "
            "JOIN run_participants p ON p.agent_id=m.author_id AND p.run_id=%s "
            "ORDER BY m.created_at,m.id",
            (run_id,),
        )
        people = await rows(
            conn, "SELECT agent_id,owner_id FROM run_participants WHERE run_id=%s", (run_id,)
        )
        owners = {str(p["agent_id"]): p["owner_id"] for p in people}
        roots = {str(m["id"]): m for m in board if m["parent_id"] is None}
        for m in board:
            m["recipients"] = [
                owners[str(a)]
                for a in (roots[str(m["thread_id"])]["audience"] or [])
                if str(a) != str(m["author_id"])
            ]
        bookings = await rows(conn, "SELECT * FROM web_bookings WHERE run_id=%s", (run_id,))
        chat = await rows(
            conn,
            "SELECT * FROM owner_messages WHERE run_id=%s AND owner_id='casey' "
            "ORDER BY created_at,id",
            (run_id,),
        )
    report = evaluate(events, board, bookings, chat)
    async with pool.connection() as conn:
        report["queued_replies"] = await rows(
            conn,
            "SELECT id,contact_id,meeting_task_id,thread_id,due_s,status,response "
            "FROM booking_inquiries WHERE run_id=%s AND response IS NOT NULL ORDER BY due_s,id",
            (run_id,),
        )
    return report


class TransferOwner:
    last_attempts = ()

    async def respond(self, view):
        spoken = {m.text for m in view.conversation if m.role == "owner"}
        text = view.request if not spoken else ""
        if view.owner_id == "casey":
            for task in PROBES:
                prompt = choice_prompt(task)
                if prompt in view.cue and prompt not in spoken:
                    text = prompt
                    break
        return ActorOutput(
            kind="reply" if text else "wait", text=text, source_event_id=view.source_event_id
        )


class TransferBoard(BoardTools):
    def __init__(self, board, pool, run_id, owner):
        super().__init__(board.client, board.cluster_id, board.operation_namespace, direct=True)
        self.pool, self.run_id, self.owner = pool, run_id, owner

    @property
    def tools(self):
        tools = super().tools
        tools.pop("post_thread")
        if self.owner in DELAYS:
            tools.pop("direct_message")
            model, description = tools["reply"]
            tools["reply"] = (
                model,
                description
                + " Your answer is queued for delivery; a pending receipt is not delivery. "
                "Do not resend a queued answer.",
            )
        else:
            model, description = tools["direct_message"]
            tools["direct_message"] = (
                create_model(
                    "MeetingRequest",
                    __base__=model,
                    meeting_task_id=(Literal[(*LEARNING, *PROBES)], ...),
                ),
                description + " Include meeting_task_id and use that exact ID as the subject. "
                "Follow-ups can use reply in the existing thread.",
            )
        return tools

    async def execute(self, name, arguments, call_id):
        if name not in self.tools:
            return _error(422, "Use the private meeting thread tools.")
        try:
            if self.owner == "casey" and name == "direct_message":
                args = (
                    self.tools[name][0]
                    .model_validate(arguments)
                    .model_dump(mode="json", exclude_none=True)
                )
                task = args.pop("meeting_task_id")
                if args["subject"] != task or len(args["to"]) != 1:
                    return _error(422, "Use one contact and the meeting ID as subject.")
                return await BoardTools(
                    self.client, self.cluster_id, self.operation_namespace, direct=True
                ).execute(name, args, call_id)
            if self.owner in DELAYS and name == "reply":
                args = self.tools[name][0].model_validate(arguments)
                result = await BookingInquiryService(self.pool).queue_reply(
                    self.run_id,
                    self.owner,
                    uuid5(self.operation_namespace, call_id),
                    args.thread_id,
                    args.body,
                )
                return {"ok": True, "data": result}
        except ValidationError:
            return _error(422, "Invalid meeting message arguments.")
        except WorldError as error:
            return _error(error.status_code, str(error))
        return await super().execute(name, arguments, call_id)


class TransferEngine(ClockEngine):
    def _agent_due(self, ctx, process, now_s, events):
        scoped = copy.deepcopy(process)
        scoped["wake_on"] = {**(scoped.get("wake_on") or {}), "peer_streak": 0}
        return super()._agent_due(ctx, scoped, now_s, events)

    async def _reconcile(self, ctx):
        report = await collect(self.pool, ctx.id)
        invalid_exposure = (
            report["familiarization_complete"] and not report["familiarization_valid"]
        )
        reason = (
            "transfer_protocol_violation"
            if report["violations"]
            else (
                "transfer_simulator_failure"
                if report["simulator_faults"]
                else "transfer_exposure_invalid"
                if invalid_exposure
                else "transfer_complete"
                if report["complete"]
                else None
            )
        )
        async with self.pool.connection() as conn, conn.transaction():
            run = await row(
                conn,
                "SELECT status,current_window FROM environment_runs WHERE id=%s FOR SHARE",
                (ctx.id,),
            )
            if run["status"] != "running":
                return
            if reason:
                await conn.execute(
                    "UPDATE environment_runs SET status='paused',result=%s WHERE id=%s",
                    (Jsonb({"reason": reason}), ctx.id),
                )
                await emit(
                    conn,
                    ctx.id,
                    "booking_transfer_stopped",
                    run["current_window"],
                    {"reason": reason},
                    reason,
                )
                return
            casey = await row(
                conn,
                "SELECT state FROM clock_processes WHERE run_id=%s "
                "AND owner_id='casey' AND kind='agent'",
                (ctx.id,),
            )
            if report["familiarization_valid"] and casey and casey["state"] != "active":
                for index, probe in enumerate(report["probes"]):
                    previous = report["learning"][-1] if index == 0 else report["probes"][index - 1]
                    if not previous["complete"] or not previous["reported"]:
                        break
                    await emit(
                        conn,
                        ctx.id,
                        "actor_cue",
                        run["current_window"],
                        {"cue": choice_prompt(probe["task"])},
                        f"transfer-choice:{probe['task']}",
                        "casey",
                    )
                    if not probe["complete"]:
                        break
        await super()._reconcile(ctx)


class TransferExperiment:
    owner = "casey"
    model_owners = frozenset({"amy", "ben", "casey"})
    inquiry_owners = frozenset()

    def configure(self, config, *, resumed):
        expected = {
            "name": NAME,
            "response_delays_by_task": transfer_delays(),
            "probe_prompts": {task: choice_prompt(task) for task in PROBES},
            "distribution": {"kind": "uniform_integer_seconds", "minimum": 120, "maximum": 600},
            "peer_wake_policy": "exp2_direct_messages_without_ambient_throttle",
            "response_delivery": "queued_peer_reply_v1",
            "channels": "private_meeting_threads_only",
        }
        if resumed and config.get("experiment") != expected:
            raise ScenarioRunError("Resume must retain exp 2's timing and prompt configuration.")
        config["experiment"] = expected
        config["actors"] = "scripted"
        config["decisions"] = "supplied" if config["scripted"] else "model"
        self.configuration = config

    async def prepare(self, pool, run_id, spec, status):
        if spec.model_dump(mode="json") != build_transfer():
            raise ScenarioRunError("Exp 2 requires its frozen scenario without overrides.")
        run = await Director(pool).get_run(run_id)
        if (run.get("result") or {}).get("reason", "").startswith("transfer_"):
            raise ScenarioRunError("This exp 2 run has ended; inspect its saved report.")
        profile = {
            "response_source": "peer_agent",
            "tasks": [
                {
                    "id": task,
                    "owner_id": "casey",
                    "requirements": task,
                    "contacts": [
                        {
                            "contact_id": owner,
                            "delay_seconds": delay,
                            "options": [
                                {
                                    "site_id": SITE,
                                    "slot_id": f"{task}-{owner}" if task in PROBES else task,
                                }
                            ],
                        }
                        for owner, delay in contacts.items()
                    ],
                }
                for task, contacts in transfer_delays().items()
            ],
        }
        await BookingInquiryService(pool).configure(run_id, profile)
        self.pool, self.run_id = pool, run_id

    def tool_setup(self, owner):
        def setup(agent):
            agent.board = TransferBoard(agent.board, self.pool, self.run_id, owner)
            agent.environment.tools = {
                name: value
                for name, value in agent.environment.tools.items()
                if name
                not in {
                    "send_mail",
                    "send_outside_mail",
                    "write_resource",
                    "create_resource",
                    "update_resource",
                }
            }
            if owner in DELAYS:
                agent.environment.tools = {
                    name: value
                    for name, value in agent.environment.tools.items()
                    if name
                    in {
                        "read_run_view",
                        "read_owner_messages",
                        "send_owner_message",
                        "read_resources",
                        "fetch_resource",
                    }
                }
                agent.web = None
            return {"name": NAME, "response_delivery": "queued_peer_reply_v1", "owner": owner}

        return setup

    def actor(self, owner):
        return TransferOwner()

    def engine(self, *args):
        return TransferEngine(*args)

    def scripted_model(self, owner):
        return TransferControl(pool=self.pool, run_id=self.run_id, owner=owner)

    async def report(self, pool, run_id):
        report = await collect(pool, run_id)
        report["model_owners"] = (
            sorted(self.model_owners) if not self.configuration["scripted"] else []
        )
        report["model"] = None if self.configuration["scripted"] else self.configuration["model"]
        report["decision_mode"] = self.configuration["decisions"]
        report["instructions_hash"] = hashlib.sha256(
            self.configuration["instructions"].encode()
        ).hexdigest()
        report["configuration_hash"] = fingerprint(self.configuration["experiment"])
        report["scenario_hash"] = (await Director(pool).get_run(run_id))["scenario_hash"]
        report["timing_policy"] = self.configuration["experiment"]
        report["board_visibility"] = (
            "Assistants are told no human sees the board; research replay can read it."
        )
        return report


class TransferControl(ScriptModel):
    pool: object
    run_id: object
    owner: str

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        report = await collect(self.pool, self.run_id)
        owner = self.owner
        tasks = (*report["learning"], *report["probes"])
        unacknowledged = sorted(
            set(report["read_message_ids"][owner]) - set(report["acknowledged_message_ids"][owner])
        )
        reply = end_turn(status="waiting", wait_for=["board_posted"])
        if unacknowledged:
            reply = calls(("acknowledge", {"message_ids": unacknowledged[:100]}))
        elif owner in DELAYS:
            for task in tasks:
                request = task["requests"].get(owner)
                if request and owner not in task["replies"]:
                    if any(
                        q["contact_id"] == owner and q["meeting_task_id"] == task["task"]
                        for q in report["queued_replies"]
                    ):
                        reply = end_turn(status="finished")
                        break
                    if str(request["id"]) not in report["read_message_ids"][owner]:
                        reply = calls(("get_updates", {}))
                    else:
                        slot = (
                            f"{task['task']}-{owner}" if task["phase"] == "probe" else task["task"]
                        )
                        reply = calls(
                            (
                                "reply",
                                {
                                    "thread_id": str(request["id"]),
                                    "body": f"Available for {task['task']}: Alder Rooms, "
                                    f"site {SITE}, slot {slot}.",
                                },
                            )
                        )
                    break
            else:
                reply = end_turn(status="finished")
        else:
            task = next((t for t in tasks if not (t["complete"] and t["reported"])), None)
            if not task or (task["phase"] == "probe" and not task["owner_instruction"]):
                reply = end_turn(status="finished")
            else:
                missing = next((o for o in DELAYS if o not in task["requests"]), None)
                unread = any(
                    str(m["id"]) not in report["read_message_ids"][owner]
                    for m in task["replies"].values()
                )
                if missing:
                    reply = calls(
                        (
                            "direct_message",
                            {
                                "to": [missing],
                                "subject": task["task"],
                                "meeting_task_id": task["task"],
                                "body": "Send the availability and free Alder Rooms option "
                                f"for {task['task']}.",
                            },
                        )
                    )
                elif unread:
                    reply = calls(("get_updates", {}))
                elif task["read_event_ids"] and not task["bookings"]:
                    first = min(
                        task["read_event_ids"],
                        key=lambda o: moment(task["replies"][o]["created_at"]),
                    )
                    slot = f"{task['task']}-{first}" if task["phase"] == "probe" else task["task"]
                    reply = calls(
                        (
                            "submit_form",
                            {
                                "url": site_url(SITE, "/slots"),
                                "action": "book",
                                "fields": {"slot_id": slot},
                            },
                        )
                    )
                elif task["complete"] and not task["reported"]:
                    reply = calls(
                        (
                            "send_owner_message",
                            {
                                "content": f"{task['task']} booked: "
                                f"{task['bookings'][0]['booking_id']}.",
                            },
                        )
                    )
        return ChatResult(generations=[ChatGeneration(message=reply)])
