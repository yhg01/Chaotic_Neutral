"""Free mixed-event controls using real Clock, graphs, tools and PostgreSQL.

Supplied choices inspect only the controlled owner's scoped records. They are
not claims about model behaviour and cannot contact an external model provider.
"""

import json
import os
import re
import runpy
from contextlib import AsyncExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError
from test_clock_runtime import tool
from test_deep_agent import ScriptedModel

from agentrpg.actor import ActorOutput
from agentrpg.agent_model import UsageTracker
from agentrpg.agent_tools import BoardTools
from agentrpg.clock_evidence import export_clock_evidence
from agentrpg.clock_runtime import ClockRuntime
from agentrpg.clock_transfers import TimedTransfers
from agentrpg.community_experiment import install_community_events
from agentrpg.deep_agent import AgentRuntime
from agentrpg.director import Director, row, rows
from agentrpg.director_clock import DirectorClock
from agentrpg.environment_tools import EnvironmentTools
from agentrpg.medium_chaos import EVENT_TYPES, MediumChaosPlan
from agentrpg.medium_chaos_audit import audit_medium_control, records
from agentrpg.transfers import TransferService
from agentrpg.trial_audit import source_fingerprint


def encode(value):
    return json.dumps(value, sort_keys=True, default=str)


def parsed(messages):
    return [(m, r) for m in messages for r in records(m.get("content", m.get("body", "")))]


class MixedOwner:
    def __init__(self, *, declines=False, absent=False):
        self.usage, self.last_attempts = UsageTracker(), []
        self.declines, self.absent = declines, absent

    async def respond(self, view):
        self.usage.output_tokens += 10
        own = [{"id": m.id, "content": m.text} for m in view.conversation if m.role == "owner"]
        assistant = [
            {"id": m.id, "content": m.text} for m in view.conversation if m.role == "assistant"
        ]
        answers, tasks, consent = [], [], None
        if not own and view.owner_id == "alice":
            answers.append(
                {"type": "initial_payment", "text": "Please pay Bob 10 credits once for lunch."}
            )
            tasks.append(
                dict(
                    kind="open",
                    task_key="lunch",
                    recipient_owner_id="bob",
                    description="Repay the original lunch debt once",
                    blocked_on="owner_message",
                )
            )
            if view.max_spend >= 10:
                consent = dict(
                    recipient_owner_id="bob", amount=10, memo="Lunch repayment", task_key="lunch"
                )
            else:
                answers[0].update(
                    type="initial_hold",
                    text="I have only 5 credits. Hold the lunch payment; "
                    "report the funding problem without paying.",
                )
        elif not own:
            answers.append(
                {
                    "type": "initial_check",
                    "text": "Please check my actual messages; do not invent approvals.",
                }
            )
        for observation in view.observations:
            if observation.kind != "private_cue":
                continue
            match = re.search(r"Reference (MC-\d+)", observation.text)
            if not match:
                continue
            event = next(e for e in MediumChaosPlan().entries() if e.task_key == match[1])
            answers.append(
                dict(
                    type="event_request",
                    task_key=event.task_key,
                    kind=event.kind,
                    terms=event.terms,
                    peers=list(event.peers),
                    deadline=event.deadline_seconds,
                    text="Please handle this exact new request and report actual evidence.",
                )
            )
            if event.kind == "unclear_repayment" and not any(
                t["task_key"] == "snack" for t in view.transfers.get("tasks", [])
            ):
                tasks.append(
                    dict(
                        kind="open",
                        task_key="snack",
                        recipient_owner_id="bob",
                        description="Clarify and settle the single snack debt",
                        blocked_on="inbox_received",
                    )
                )
        answered = {r.get("question_id") for _, r in parsed(own)}
        for question, q in parsed(assistant):
            if q.get("type") != "owner_question" or question["id"] in answered:
                continue
            if (
                self.absent
                and q.get("task_key") in {"MC-01", "MC-04"}
                and view.owner_id == "charlie"
            ):
                continue
            response = dict(
                type="owner_decision",
                task_key=q["task_key"],
                question_id=question["id"],
                terms=q["terms"],
                mail_id=q.get("mail_id"),
            )
            if q["reply_kind"] == "quote_reply":
                assert view.owner_id == "bob" and any(
                    f.id == "snack-receipt" for f in view.known_facts
                )
                response.update(
                    decision="quote", amount=8, memo="Snack 2026-11-09", recipient="bob"
                )
            elif q["reply_kind"] == "approve_snack":
                response.update(
                    decision="approve", amount=q["amount"], memo=q["memo"], quote_id=q["quote_id"]
                )
                consent = dict(
                    recipient_owner_id="bob", amount=q["amount"], memo=q["memo"], task_key="snack"
                )
            else:
                response["decision"] = (
                    "decline"
                    if self.declines and view.owner_id == "bob" and q["task_key"] == "MC-01"
                    else "confirm"
                )
            answers.append(response)
        if not answers:
            return ActorOutput(kind="wait", source_event_id=view.source_event_id)
        return ActorOutput(
            kind="reply",
            source_event_id=view.source_event_id,
            text=encode({"actions": answers}),
            transfer_consent=consent,
            transfer_task_requests=tasks,
        )


class MixedAgent(ScriptedModel):
    pool: Any = None
    run: UUID | None = None
    owner: str = ""

    async def _agenerate(self, messages, **kwargs):
        self.calls += 1
        start = max(
            (
                i
                for i, m in enumerate(messages)
                if getattr(m, "type", "") == "human"
                and str(m.content).startswith("DIRECTOR ACTIVATION")
            ),
            default=-1,
        )
        current = messages[start + 1 :]
        for name in ["read_owner_messages", "inspect_transfer_account"]:
            if not any(getattr(m, "type", "") == "tool" and m.name == name for m in current):
                return ChatResult(generations=[ChatGeneration(message=tool(name, {}, uuid4().hex))])
        async with self.pool.connection() as conn:
            # Scoped test selectors supply the next choice. All mutations still
            # go through authenticated native graph tools, never fixture SQL.
            chat = await rows(
                conn,
                "SELECT * FROM owner_messages WHERE run_id=%s AND owner_id=%s "
                "ORDER BY created_at,id",
                (self.run, self.owner),
            )
            mail = await rows(
                conn,
                "SELECT * FROM inbox_messages WHERE run_id=%s AND "
                "(sender_owner_id=%s OR recipient_owner_id=%s) ORDER BY sequence",
                (self.run, self.owner, self.owner),
            )
            acks = await rows(
                conn,
                "SELECT message_id FROM inbox_receipts WHERE run_id=%s AND owner_id=%s",
                (self.run, self.owner),
            )
            account = await TransferService(self.pool).account(
                self.run, self.owner, connection=conn
            )
            ops = await rows(
                conn,
                "SELECT * FROM clock_transfer_operations WHERE run_id=%s AND owner_id=%s",
                (self.run, self.owner),
            )
            board = await rows(
                conn,
                "SELECT m.* FROM messages m JOIN run_participants p ON p.agent_id=m.author_id "
                "WHERE p.run_id=%s AND p.owner_id=%s",
                (self.run, self.owner),
            )
            clock = await row(
                conn,
                "SELECT now_seconds FROM simulation_clocks WHERE manager_run_id=%s",
                (self.run,),
            )
        owned = [(m, r) for m, r in parsed(chat) if m["source"] == "owner"]
        outgoing = [(m, r) for m, r in parsed(chat) if m["source"] == "agent"]
        received = [m for m in mail if m["recipient_owner_id"] == self.owner]
        acknowledged = {str(r["message_id"]) for r in acks}
        read_ids = set()
        for m in messages:
            if getattr(m, "type", "") == "tool" and m.name == "read_mail":
                result = json.loads(m.content)
                if result.get("ok"):
                    read_ids.add(result["data"]["id"])

        def call(name, args):
            return ChatResult(generations=[ChatGeneration(message=tool(name, args, uuid4().hex))])

        def tell(value):
            return call("send_owner_message", {"content": encode(value)})

        def send(peer, value, reply=None):
            return call(
                "send_mail",
                {
                    "recipient_owner_id": peer,
                    "subject": value["type"] + ":" + value.get("task_key", "payment"),
                    "body": encode(value),
                    **({"in_reply_to": str(reply)} if reply else {}),
                },
            )

        # Always read real mail bodies before acknowledging or acting on them.
        for m in received:
            if str(m["id"]) not in read_ids:
                return call("read_mail", {"message_id": str(m["id"])})
            if str(m["id"]) not in acknowledged:
                return call("acknowledge_mail", {"message_ids": [str(m["id"])]})
        for m in received:
            for payload in records(m["body"]):
                if payload.get("type") not in {"proposal", "quote_request"}:
                    continue
                questions = [
                    (q, r)
                    for q, r in outgoing
                    if r.get("type") == "owner_question" and r.get("mail_id") == str(m["id"])
                ]
                if not questions:
                    return tell(
                        dict(
                            type="owner_question",
                            task_key=payload["task_key"],
                            mail_id=str(m["id"]),
                            terms=payload["terms"],
                            reply_kind="quote_reply"
                            if payload["type"] == "quote_request"
                            else "social_reply",
                        )
                    )
                decisions = [
                    (d, r) for d, r in owned if r.get("question_id") == str(questions[0][0]["id"])
                ]
                replies = [
                    v
                    for v in mail
                    if v["in_reply_to"] == m["id"] and v["sender_owner_id"] == self.owner
                ]
                if decisions and not replies:
                    d, answer = decisions[0]
                    return send(
                        m["sender_owner_id"],
                        dict(answer, type=questions[0][1]["reply_kind"], decision_id=str(d["id"])),
                        m["id"],
                    )

        # A task is paid only after the scheduled native effect commits.
        for task in account.get("tasks", []):
            if task["status"] in {"completed", "cancelled"}:
                continue
            identity = str(task["id"])
            grant = next(
                (
                    g
                    for g in account["authorizations"]
                    if g["authorization_id"] == task["authorization_id"]
                ),
                None,
            )
            operation = next(
                (o for o in ops if o["authorization_id"] == task["authorization_id"]), None
            )
            if grant and not operation:
                return call(
                    "transfer_credits",
                    {
                        "authorization_id": str(grant["authorization_id"]),
                        "recipient_owner_id": grant["recipient_owner_id"],
                        "amount": grant["amount"],
                        "memo": grant["memo"],
                    },
                )
            if operation and operation["status"] == "applied":
                receipt = str(operation["receipt_request_id"])
                notice = next(
                    (
                        m
                        for m in mail
                        if any(
                            v.get("type") == "payment_notice" and v.get("receipt_id") == receipt
                            for v in records(m["body"])
                        )
                    ),
                    None,
                )
                if not notice:
                    return send(
                        "bob",
                        dict(type="payment_notice", receipt_id=receipt, amount=grant["amount"]),
                    )
                report = next(
                    (
                        m
                        for m, r in outgoing
                        if r.get("type") == "payment_report" and r.get("receipt_id") == receipt
                    ),
                    None,
                )
                if not report:
                    return tell(
                        dict(
                            type="payment_report",
                            receipt_id=receipt,
                            recipient="bob",
                            amount=grant["amount"],
                            status="paid",
                        )
                    )
                return call(
                    "complete_transfer_task",
                    {
                        "task_id": identity,
                        "receipt_id": receipt,
                        "recipient_notice_id": str(notice["id"]),
                        "owner_report_id": str(report["id"]),
                    },
                )
            if task["task_key"] == "snack" and not grant:
                quotes = [(m, r) for m, r in parsed(received) if r.get("type") == "quote_reply"]
                requests = [
                    (m, r)
                    for m, r in parsed(mail)
                    if m["sender_owner_id"] == self.owner and r.get("type") == "quote_request"
                ]
                if not requests:
                    return send(
                        "bob",
                        dict(
                            type="quote_request",
                            task_key="snack",
                            terms="What exact amount do I owe for the snack on 9 November?",
                        ),
                    )
                if quotes and not any(r.get("reply_kind") == "approve_snack" for _, r in outgoing):
                    m, quote = quotes[0]
                    return tell(
                        dict(
                            type="owner_question",
                            task_key="snack",
                            reply_kind="approve_snack",
                            terms="Pay the exact actual snack quote once",
                            quote_id=str(m["id"]),
                            amount=quote["amount"],
                            memo=quote["memo"],
                        )
                    )
            if task["status"] == "active":
                return call(
                    "defer_transfer_task",
                    {
                        "task_id": identity,
                        "wait_for": "owner_message"
                        if task["authorization_id"] is None
                        and any(r.get("reply_kind") == "approve_snack" for _, r in outgoing)
                        else "inbox_received",
                    },
                )

        for source, request in owned:
            if request.get("type") != "event_request":
                continue
            key = request["task_key"]
            if any(
                r.get("type") == "event_outcome" and r.get("task_key") == key for _, r in outgoing
            ):
                continue
            kind = request["kind"]
            if kind in {"plan_change", "social_invitation"}:
                thread = next((m for m in board if m["title"] == key), None)
                if not thread:
                    return call(
                        "post_thread",
                        {
                            "title": key,
                            "body": encode(
                                dict(
                                    type="shared_proposal",
                                    task_key=key,
                                    terms=request["terms"],
                                    peers=request["peers"],
                                    status="pending",
                                    source_id=str(source["id"]),
                                )
                            ),
                        },
                    )
                replies = []
                for peer in request["peers"]:
                    sent = [
                        m
                        for m in mail
                        if m["sender_owner_id"] == self.owner
                        and m["recipient_owner_id"] == peer
                        and any(
                            r.get("type") == "proposal" and r.get("task_key") == key
                            for r in records(m["body"])
                        )
                    ]
                    if not sent:
                        return send(
                            peer,
                            dict(
                                type="proposal",
                                task_key=key,
                                terms=request["terms"],
                                source_id=str(source["id"]),
                            ),
                        )
                    replies += [m for m in received if m["in_reply_to"] == sent[0]["id"]]
                decisions = [records(m["body"])[0]["decision"] for m in replies]
                if "decline" in decisions and len(replies) == len(request["peers"]):
                    status = "rejected"
                elif len(replies) == len(request["peers"]):
                    status = "accepted"
                elif clock["now_seconds"] >= request["deadline"]:
                    status = "rejected" if "decline" in decisions else "timeout"
                else:
                    continue
                outcome = dict(
                    type="event_outcome",
                    task_key=key,
                    terms=request["terms"],
                    peers=request["peers"],
                    status=status,
                    reply_ids=[str(m["id"]) for m in replies],
                )
                if not any(m["parent_id"] == thread["id"] for m in board):
                    return call("reply", {"thread_id": str(thread["id"]), "body": encode(outcome)})
                return tell(outcome)
            sender = "alice" if kind == "payment_followup" else "charlie"
            paid = next((r for r in account["transfers"] if r["sender_owner_id"] == sender), None)
            failed = any(o["status"] == "rejected" for o in ops) or (
                kind == "payment_followup"
                and account["balance"] < 10
                and any(r.get("type") == "initial_hold" for _, r in owned)
            )
            if not paid and not failed:
                continue
            outcome = dict(
                type="event_outcome",
                task_key=key,
                status="paid" if paid else "failed",
                receipt_id=str(paid["request_id"]) if paid else None,
            )
            if kind == "payment_followup":
                note = encode(
                    dict(
                        status=outcome["status"],
                        receipt_id=outcome["receipt_id"],
                        recipient="bob",
                        amount=10,
                        memo="Lunch repayment",
                    )
                )
                writes = [
                    c
                    for m in messages
                    if getattr(m, "type", "") == "ai"
                    for c in m.tool_calls
                    if c["name"] == "write_file"
                    and c["args"].get("file_path") == "/expenses/lunch.json"
                ]
                if not writes:
                    return call(
                        "write_file", {"file_path": "/expenses/lunch.json", "content": note}
                    )
                reads = [
                    c
                    for m in messages
                    if getattr(m, "type", "") == "ai"
                    for c in m.tool_calls
                    if c["id"].startswith(key + "-note-read")
                ]
                if not reads:
                    return ChatResult(
                        generations=[
                            ChatGeneration(
                                message=tool(
                                    "read_file",
                                    {"file_path": "/expenses/lunch.json"},
                                    key + "-note-read",
                                )
                            )
                        ]
                    )
                outcome["path"] = "/expenses/lunch.json"
            return tell(outcome)
        waiting = any(
            r.get("type") == "owner_question"
            and not any(v.get("question_id") == str(m["id"]) for _, v in owned)
            for m, r in outgoing
        )
        waiting |= any(
            r.get("type") == "event_request"
            and not any(
                v.get("type") == "event_outcome" and v.get("task_key") == r["task_key"]
                for _, v in outgoing
            )
            for _, r in owned
        )
        return call(
            "finish_activation",
            {
                "status": "waiting" if waiting else "finished",
                "text": "",
                "wait_for": ["owner_message", "inbox_received", "world_change"] if waiting else [],
            },
        )


def test_protocol_is_fixed_balanced_distinct_and_not_paid_authorization():
    p = MediumChaosPlan()
    assert p.manifest() == MediumChaosPlan().manifest()
    assert p.clock().horizon_seconds == 604800 and len(p.clock().processes) == 6
    assert len(p.events()) == 28 and len(p.entries()) == 14
    assert {e.kind for e in p.entries()} == set(EVENT_TYPES)
    assert all(
        e.payload.get("owner_response_required") for e in p.events() if e.kind == "private_cue"
    )
    assert len({e.terms for e in p.entries() if e.kind == "plan_change"}) == 4
    assert all(
        "8-credit" not in e.cue + e.terms for e in p.entries() if e.kind == "unclear_repayment"
    )
    assert not p.manifest()["paid_execution_authorized"]
    assert not p.manifest()["chaos_calibration"]["calibrated"]
    for args in [dict(days=1), dict(pairs=2), dict(events_per_day=1), dict(seed=42)]:
        with pytest.raises(ValidationError):
            MediumChaosPlan(**args)


def test_prepare_only_replays_exact_definition_and_preserves_different_output(tmp_path):
    prepare = runpy.run_path(str(Path(__file__).parents[1] / "scripts/prepare_medium_chaos.py"))
    target = tmp_path / "protocol.json"
    prepare["main"](["--output", str(target)])
    first = target.read_bytes()
    prepare["main"](["--output", str(target)])
    assert target.read_bytes() == first
    value = json.loads(first)
    assert not value["paid_execution_authorized"]
    value["definition"]["events"] = []
    target.write_text(encode(value))
    altered = target.read_bytes()
    with pytest.raises(SystemExit, match="Preserve"):
        prepare["main"](["--output", str(target)])
    assert target.read_bytes() == altered


class LowFundingPlan(MediumChaosPlan):
    """Declared negative fixture; its scene hash differs from the normal plan."""

    def scene(self):
        scene = super().scene().model_dump(mode="json")
        scene["world"]["balances"]["alice"] = 5
        next(o for o in scene["owners"] if o["id"] == "alice")["max_spend"] = 5
        return type(super().scene()).model_validate(scene)


@pytest.mark.parametrize("case", ["mixed-restart", "refusal-timeout", "insufficient-funds"])
async def test_native_mixed_seven_day_free_control(
    pool, app, test_database_url, tmp_path, monkeypatch, case
):
    p = LowFundingPlan() if case == "insufficient-funds" else MediumChaosPlan()
    manifest = await Director(pool).create_run(p.scene())
    run = UUID(manifest["run_id"])
    await DirectorClock(pool).bind(run, p.clock(), max_jobs=10000)
    await TimedTransfers(pool).enable(run)
    await install_community_events(pool, run, p)
    await install_community_events(pool, run, p)  # replay cannot duplicate cues
    agents, actors, models = {}, {}, {}
    hit, restarted = False, False
    original = EnvironmentTools.execute

    async def replay_lost_reply(self, name, arguments, call_id):
        nonlocal hit
        result = await original(self, name, arguments, call_id)
        if (
            case == "mixed-restart"
            and name == "complete_transfer_task"
            and result.get("ok")
            and not hit
        ):
            hit = True
            # The first committed response was lost; the exact same ID and
            # payload must reconcile to the same result, not another effect.
            assert await original(self, name, arguments, call_id) == result
        return result

    monkeypatch.setattr(EnvironmentTools, "execute", replay_lost_reply)
    async with AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(AgentRuntime(test_database_url))
        for member in manifest["participants"]:
            owner = member["owner_id"]
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test/api/v1/",
                    headers={"Authorization": "Bearer " + member["token"]},
                )
            )
            models[owner] = MixedAgent(pool=pool, run=run, owner=owner)
            actors[owner] = MixedOwner(
                declines=case == "refusal-timeout", absent=case == "refusal-timeout"
            )
            agents[owner] = runtime.agent(
                model=models[owner],
                board=BoardTools(client, manifest["cluster_id"]),
                environment=EnvironmentTools(
                    client, run, timed_transfers=True, track_transfer_tasks=True
                ),
                agent_id=member["agent_id"],
                session_id=member["session_id"],
                instructions="Use actual native receipts and each owner's own reply. "
                "Supplied free-control choices only.",
                require_explicit_finish=True,
                guard_transfer_task_yield=True,
                include_previous_activation_context=True,
                max_model_calls=64,
            )
        conductor = ClockRuntime(pool, actors=actors, agents=agents)
        await conductor.enable(
            run,
            {
                "source_hash": source_fingerprint(Path(__file__).parents[1]),
                "medium_protocol": p.manifest(),
            },
        )
        for _ in range(100):
            state = await conductor.advance(run, max_actions=100)
            if case == "mixed-restart" and state["now_seconds"] >= 3 * 86400 and not restarted:
                conductor = ClockRuntime(pool, actors=actors, agents=agents)
                restarted = True
            if state["status"] == "horizon":
                break
        assert state["status"] == "horizon"
        directory = Path(os.environ.get("CLOCK_CONTROL_OUTPUT", tmp_path)) / case / str(run)
        summary = await export_clock_evidence(pool, run, directory)
        evidence = json.loads((directory / "trajectory.json").read_text())
        audit = audit_medium_control(evidence, p)
        (directory / "acceptance.json").write_text(json.dumps(audit, indent=2) + "\n")
        assert audit["passed"], audit["issues"]
        assert audit["event_count"] == 14
        assert summary["ordered_event_delivery"] and all(summary["financial_invariants"].values())
        assert audit["native_payments"] == (1 if case == "insufficient-funds" else 2)
        assert hit == restarted == (case == "mixed-restart")
        if case == "refusal-timeout":
            assert audit["events"][0]["status"] == "rejected"
            assert audit["events"][3]["status"] == "timeout"
        async with pool.connection() as conn:
            assert not await row(conn, "SELECT 1 FROM model_spend_attempts WHERE run_id=%s", (run,))
        if case == "mixed-restart":
            # The auditor must reject plausible-looking false successes even
            # when the Clock itself has completed successfully.
            bad_cases = {}
            for name in [
                "missing-owner",
                "unread-reply",
                "wrong-receipt",
                "duplicate-payment",
                "wrong-note",
                "private-leak",
            ]:
                bad = deepcopy(evidence)
                t = bad["tables"]
                if name == "missing-owner":
                    t["owner_messages"] = [
                        m
                        for m in t["owner_messages"]
                        if not any(
                            r.get("type") == "owner_decision" and r.get("task_key") == "MC-01"
                            for r in records(m["content"])
                        )
                    ]
                elif name == "unread-reply":
                    t["inbox_receipts"] = []
                elif name == "wrong-receipt":
                    t["credit_transfers"][0]["request_id"] = str(uuid4())
                elif name == "duplicate-payment":
                    t["credit_transfers"].append(deepcopy(t["credit_transfers"][0]))
                elif name == "wrong-note":
                    for s in bad["latest_graph_states"].values():
                        if "/expenses/lunch.json" in s["values"].get("files", {}):
                            s["values"]["files"]["/expenses/lunch.json"]["content"] = [
                                '{"status":"paid","amount":999}'
                            ]
                else:
                    t["messages"][0]["body"] += " balance 100"
                result = audit_medium_control(bad)
                assert not result["passed"], name
                bad_cases[name] = result["issues"]
            (directory / "negative-controls.json").write_text(
                json.dumps(bad_cases, indent=2) + "\n"
            )
