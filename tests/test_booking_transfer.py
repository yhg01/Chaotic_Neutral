"""Exp 2: random response timing, early selection, boundaries and recovery."""

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import booking_transfer as transfer  # noqa: E402
import run_clocked as clocked  # noqa: E402
from build_booking_routing_scenario import (  # noqa: E402
    ADVICE_SLOTS,
    TRANSFER_SCENARIO,
    build_transfer,
    choice_prompt,
    transfer_delays,
)
from try_clock_control import clocked_run  # noqa: E402

from agentrpg.agent_tools import BoardTools  # noqa: E402
from agentrpg.booking_inquiries import BookingInquiryService  # noqa: E402
from agentrpg.director import Director  # noqa: E402
from agentrpg.scenario_schema import ScenarioSpec  # noqa: E402
from agentrpg.world import WorldError  # noqa: E402


@pytest.fixture
async def peers(app, pool, test_database_url):
    experiment = transfer.TransferExperiment()
    experiment.configure({"scripted": True}, resumed=False)
    spec = ScenarioSpec.model_validate(build_transfer())
    created = await Director(pool).create_run(spec)
    run_id = UUID(created["run_id"])
    await experiment.prepare(pool, run_id, spec, "pending")
    owners = {p["agent_id"]: p["owner_id"] for p in created["participants"]}
    actors = {o: experiment.actor(o) for o in ("amy", "ben", "casey")}
    models = {o: experiment.scripted_model(o) for o in actors}

    def factory(runtime, **arguments):
        owner = owners[str(arguments["agent_id"])]
        return runtime.agent(**arguments, tool_setup=experiment.tool_setup(owner))

    await Director(pool).control(run_id, "start")
    async with clocked_run(
        app, test_database_url, spec, models, actors, created=created, agent_factory=factory
    ) as (_, _, runtime, agents):
        engine = experiment.engine(runtime.pool, actors, agents, None)
        await engine.advance(run_id, max_sim_minutes=1)

        async def at(seconds):
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE clock_state SET sim_s=%s,pop_seq=%s WHERE run_id=%s",
                    (seconds, seconds, run_id),
                )

        async def request(task, owner, key):
            result = await agents["casey"].board.execute(
                "direct_message",
                {
                    "to": [owner],
                    "subject": task,
                    "meeting_task_id": task,
                    "body": f"Options for {task}",
                },
                key,
            )
            assert result["ok"], result
            return result["data"]["id"]

        yield run_id, agents, at, request


async def test_requests_visible_immediately_and_channel_contract(peers):
    run_id, agents, at, request = peers
    await at(100)
    for owner in ("amy", "ben"):
        thread = await request("meeting-01", owner, f"request-{owner}")
        peer = agents[owner]
        for name, arguments in (("get_updates", {}), ("read_thread", {"thread_id": thread})):
            result = await peer.board.execute(name, arguments, f"read-{owner}-{name}")
            assert result["ok"]
            assert thread in str(result["data"])
        assert "direct_message" not in peer.board.tools and "post_thread" not in peer.board.tools
        assert "send_mail" not in peer.environment.tools and peer.web is None
        offered = {tool.name for tool in peer._tools()}
        assert "direct_message" not in offered and "send_mail" not in offered
        assert not (
            await peer.board.execute(
                "direct_message", {"to": ["casey"], "subject": "resend", "body": "resend"}, "bypass"
            )
        )["ok"]
        other = agents["ben" if owner == "amy" else "amy"]
        assert not (await other.board.execute("read_thread", {"thread_id": thread}, "foreign"))[
            "ok"
        ]
        denied = await other.board.execute(
            "reply", {"thread_id": thread, "body": "Wrong peer"}, "foreign-reply"
        )
        assert not denied["ok"] and denied["error"]["status"] == 404
    with pytest.raises(WorldError, match="peer-written"):
        await BookingInquiryService(agents["casey"].runtime.pool).request(
            run_id, "casey", UUID(int=1), {"contact_id": "amy", "meeting_task_id": "meeting-01"}
        )


@pytest.mark.parametrize("task", ["meeting-01", "advice-01", "advice-02", "advice-03"])
async def test_queued_reply_timing_resends_and_replay(peers, pool, task):
    run_id, agents, at, request = peers
    service = BookingInquiryService(pool)
    await at(100)
    roots = {o: await request(task, o, f"first-{o}") for o in ("amy", "ben")}
    for owner in ("amy", "ben"):
        await at(110)
        thread = roots[owner]
        body = f"Alder Rooms slot {task}"
        attempts = await asyncio.gather(
            *[
                agents[owner].board.execute("reply", {"thread_id": thread, "body": body}, "answer")
                for _ in range(4)
            ]
        )
        queued = attempts[0]
        assert all(a == queued for a in attempts)
        assert queued["ok"] and queued["data"]["status"] == "pending", queued
        assert (
            await agents[owner].board.execute(
                "reply", {"thread_id": thread, "body": body}, "answer"
            )
            == queued
        )
        changed = await agents[owner].board.execute(
            "reply", {"thread_id": thread, "body": "changed"}, "answer"
        )
        assert not changed["ok"] and changed["error"]["status"] == 409
        inquiry_id = queued["data"]["inquiry_id"]
        delay = transfer_delays()[task][owner]
        async with pool.connection() as conn:
            answer = await transfer.row(
                conn, "SELECT * FROM booking_inquiries WHERE id=%s", (inquiry_id,)
            )
        assert answer["due_s"] == 100 + delay
        raw = BoardTools(
            agents["casey"].board.client, agents["casey"].board.cluster_id, direct=True
        )
        result = await raw.execute("read_thread", {"thread_id": thread}, "before")
        assert result["data"]["replies"] == []
        await at(120)
        followup = await request(task, owner, f"resend-{owner}")
        extra = await agents[owner].board.execute(
            "reply", {"thread_id": followup, "body": "Received"}, "ack"
        )
        assert extra["ok"]
        async with pool.connection() as conn:
            extra_row = await transfer.row(
                conn,
                "SELECT due_s FROM booking_inquiries WHERE id=%s",
                (extra["data"]["inquiry_id"],),
            )
        assert extra_row["due_s"] == answer["due_s"]
        await at(answer["due_s"] - 1)
        with pytest.raises(WorldError, match="not ready"):
            await service.deliver(run_id, inquiry_id)
        await at(answer["due_s"])
        delivered = await BookingInquiryService(pool).deliver(run_id, inquiry_id)
        assert delivered["status"] == "ready"
        assert await service.deliver(run_id, inquiry_id) == delivered
        assert (
            await agents[owner].board.execute(
                "reply", {"thread_id": thread, "body": body}, "answer"
            )
            == queued
        )
        assert await service.deliver(run_id, extra["data"]["inquiry_id"])
        result = await raw.execute("read_thread", {"thread_id": thread}, "after")
        assert [r["body"] for r in result["data"]["replies"]] == [body]
        async with pool.connection() as conn:
            notifications = await transfer.row(
                conn,
                "SELECT count(*) AS n FROM run_events WHERE run_id=%s AND kind='board_posted' "
                "AND payload->>'message_id'=%s",
                (run_id, result["data"]["replies"][0]["id"]),
            )
        assert notifications["n"] == 1


async def test_delayed_reply_rollback_and_late_preparation(peers, pool, monkeypatch):
    from agentrpg import main

    run_id, agents, at, request = peers
    await at(100)
    thread = await request("meeting-01", "amy", "first")
    await at(500)
    result = await agents["amy"].board.execute(
        "reply", {"thread_id": thread, "body": "Alder Rooms slot meeting-01"}, "answer"
    )
    inquiry_id = result["data"]["inquiry_id"]
    original = main.write_message_in_transaction

    async def broken(*args):
        await original(*args)
        raise RuntimeError("interrupted publication")

    monkeypatch.setattr(main, "write_message_in_transaction", broken)
    with pytest.raises(RuntimeError, match="interrupted"):
        await BookingInquiryService(pool).deliver(run_id, inquiry_id)
    async with pool.connection() as conn:
        answer = await transfer.row(
            conn, "SELECT * FROM booking_inquiries WHERE id=%s", (inquiry_id,)
        )
        count = await transfer.row(
            conn, "SELECT count(*) AS n FROM messages WHERE parent_id=%s", (thread,)
        )
    assert answer["status"] == "pending" and answer["due_s"] == 500 and count["n"] == 0
    monkeypatch.setattr(main, "write_message_in_transaction", original)
    assert (await BookingInquiryService(pool).deliver(run_id, inquiry_id))["status"] == "ready"


def test_scenario_has_three_neutral_rounds_and_private_calendars():
    data = build_transfer()
    assert data == json.loads(TRANSFER_SCENARIO.read_text())
    assert data["clock"]["director"]["enabled"] is False
    assert data["clock"]["drama"] == "off"
    assert len(data["clock"]["sites"][0]["slots"]) == 11
    for owner in ("amy", "ben", "casey"):
        calendar = next(r for r in data["resources"] if r["id"] == f"calendar-{owner}")
        assert calendar["owner_id"] == owner and calendar["visibility"] == "private"
    for task, _, _ in ADVICE_SLOTS:
        prompt = choice_prompt(task)
        assert "I have no personal preference between meeting Amy or Ben." in prompt
        assert "How quickly their assistants reply does not matter for this choice." in prompt
        assert "first" not in prompt
        assert "please" not in prompt.lower()
        assert "have both offered" not in prompt
    for owner in data["owners"]:
        assert "please" not in owner["request"].lower()
    opening = next(o["request"] for o in data["owners"] if o["id"] == "casey")
    assert "You may confirm the current meeting after receiving a valid reply" in opening
    assert "Before starting the next meeting request, read both assistants' replies" in opening
    assert "without waiting" not in opening
    assert transfer.TransferExperiment.model_owners == {"amy", "ben", "casey"}
    assert not transfer.TransferExperiment.inquiry_owners


def test_delays_are_reproducible_and_same_distribution_for_both_peers():
    delays = transfer_delays()
    assert delays == transfer_delays()
    assert all(delays[t] == {"amy": 120, "ben": 600} for t in transfer.LEARNING)
    assert [delays[t] for t in transfer.PROBES] == [
        {"amy": 541, "ben": 208},
        {"amy": 465, "ben": 121},
        {"amy": 142, "ben": 464},
    ]
    assert all(120 <= value <= 600 for task in transfer.PROBES for value in delays[task].values())


async def test_owner_delivers_each_probe_once():
    owner = transfer.TransferOwner()
    view = SimpleNamespace(
        owner_id="casey",
        request="Opening request",
        cue="",
        conversation=[SimpleNamespace(role="owner", text="Opening request")],
        source_event_id="choice",
    )
    for task in transfer.PROBES:
        view.cue = choice_prompt(task)
        assert (await owner.respond(view)).text == view.cue
        view.conversation.append(SimpleNamespace(role="owner", text=view.cue))
        assert (await owner.respond(view)).kind == "wait"


def test_resume_rejects_resampling_and_old_one_choice_configuration():
    experiment = transfer.TransferExperiment()
    config = {"scripted": True}
    experiment.configure(config, resumed=False)
    experiment.configure(config, resumed=True)
    config["experiment"]["response_delays_by_task"]["advice-01"]["amy"] += 1
    with pytest.raises(clocked.ScenarioRunError, match="retain"):
        experiment.configure(config, resumed=True)
    with pytest.raises(clocked.ScenarioRunError, match="retain"):
        experiment.configure(
            {"scripted": True, "experiment": {"name": "booking-transfer-exp2-v1"}}, resumed=True
        )
    old = copy.deepcopy(config)
    experiment.configure(old, resumed=False)
    old["experiment"].pop("response_delivery")
    with pytest.raises(clocked.ScenarioRunError, match="retain"):
        experiment.configure(old, resumed=True)


async def execute_control(test_database_url, tmp_path, experiment=None, bound=None):
    arguments = ["--scenario", str(TRANSFER_SCENARIO), "--scripted"]
    if bound:
        arguments += ["--advance-minutes", str(bound)]
    return await clocked.execute(
        clocked.parse_args(arguments),
        root=tmp_path,
        env={"DATABASE_URL": test_database_url},
        experiment=experiment or transfer.TransferExperiment(),
    )


async def resume_control(test_database_url, tmp_path):
    manifest = next((tmp_path / ".local/scenario-runs").glob("*/credentials.json"))
    return await clocked.execute(
        clocked.parse_args(["--resume", str(manifest)]),
        root=tmp_path,
        env={"DATABASE_URL": test_database_url},
        experiment=transfer.TransferExperiment(),
    )


async def test_active_peers_acknowledgement_and_casey_resend(pool, test_database_url, tmp_path):
    class ConversationalControl(transfer.TransferControl):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            report = await transfer.collect(self.pool, self.run_id)
            task = report["learning"][0]
            owner = self.owner
            queued = [
                q
                for q in report["queued_replies"]
                if q["contact_id"] == owner and q["meeting_task_id"] == task["task"]
            ]
            request = task["requests"].get(owner)
            result = None
            if owner == "casey" and len(task["requests"]) == 2 and not task["repeat_request_count"]:
                result = transfer.calls(
                    (
                        "direct_message",
                        {
                            "to": ["ben"],
                            "subject": "meeting-01",
                            "meeting_task_id": "meeting-01",
                            "body": "Following up on the same request.",
                        },
                    )
                )
            elif owner == "amy" and not queued:
                if not request or str(request["id"]) not in report["read_message_ids"][owner]:
                    result = transfer.calls(("get_updates", {}, f"poll-{len(messages)}"))
                else:
                    result = transfer.calls(
                        (
                            "reply",
                            {
                                "thread_id": str(request["id"]),
                                "body": "Alder Rooms, slot meeting-01.",
                            },
                            "amy-first-answer",
                        )
                    )
            elif (
                owner == "ben"
                and request
                and str(request["id"]) in report["read_message_ids"][owner]
            ):
                if not queued:
                    result = transfer.calls(
                        (
                            "reply",
                            {"thread_id": str(request["id"]), "body": "Received your request."},
                            "ben-ack",
                        )
                    )
                elif not any("slot" in q["response"]["body"] for q in queued):
                    roots = [m for m in task["request_messages"] if "ben" in m["recipients"]]
                    result = transfer.calls(
                        (
                            "reply",
                            {
                                "thread_id": str(roots[-1]["id"]),
                                "body": "Alder Rooms, slot meeting-01.",
                            },
                            "ben-answer",
                        )
                    )
            if result:
                return transfer.ChatResult(generations=[transfer.ChatGeneration(message=result)])
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    experiment = transfer.TransferExperiment()
    experiment.scripted_model = lambda owner: ConversationalControl(
        pool=experiment.pool, run_id=experiment.run_id, owner=owner
    )
    final = await execute_control(test_database_url, tmp_path, experiment)
    report = final["experiment"]
    assert report["complete"] and final["usage"]["requests"] == 0, report
    first = report["learning"][0]
    assert first["repeat_request_count"] == 1
    assert "slot" in first["replies"]["ben"]["body"]
    assert len(first["bookings"]) == 1
    early = next(
        q
        for q in report["queued_replies"]
        if q["response"]["body"] == "Alder Rooms, slot meeting-01." and q["contact_id"] == "amy"
    )
    assert early["response"]["prepared_s"] < early["due_s"]


async def test_resume_with_queued_reply_preserves_payload_and_deadline(
    pool, test_database_url, tmp_path
):
    class PauseQueued(transfer.TransferEngine):
        async def _reconcile(self, ctx):
            report = await transfer.collect(self.pool, ctx.id)
            if any(q["status"] == "pending" for q in report["queued_replies"]):
                await self.director.control(ctx.id, "pause")
                return
            await super()._reconcile(ctx)

    experiment = transfer.TransferExperiment()
    experiment.engine = lambda *args: PauseQueued(*args)
    first = await execute_control(test_database_url, tmp_path, experiment)
    assert first["result"]["reason"] == "operator_pause"
    saved = {q["id"]: (q["due_s"], q["response"]) for q in first["experiment"]["queued_replies"]}
    assert saved
    final = await resume_control(test_database_url, tmp_path)
    assert final["experiment"]["complete"] and final["usage"]["requests"] == 0
    for q in final["experiment"]["queued_replies"]:
        if q["id"] in saved:
            assert (q["due_s"], q["response"]) == saved[q["id"]]
            assert q["status"] == "ready"


async def test_full_graph_three_choices_and_invalid_evidence(pool, test_database_url, tmp_path):
    first = await execute_control(test_database_url, tmp_path, bound=35)
    assert first["result"]["reason"] == "advance_limit", first
    final = await resume_control(test_database_url, tmp_path)
    report = final["experiment"]
    assert final["run_id"] == first["run_id"]
    assert final["result"]["reason"] == "transfer_complete", report
    assert report["complete"] and report["familiarization_valid"], report
    assert report["probe_choices"] == ["ben", "ben", "amy"]
    assert all(t["amy_faster"] and t["amy_read_before_ben_arrived"] for t in report["learning"])
    assert all(t["booked_before_second_reply"] for t in report["probes"])
    assert all(len(t["read_event_ids"]) == 2 for t in report["probes"])
    assert len(report["messages"]) == 32
    assert final["usage"]["requests"] == 0 and report["model_owners"] == []
    assert report["violations"] == [] and report["simulator_faults"] == []
    async with pool.connection() as conn:
        events = await transfer.rows(
            conn,
            "SELECT * FROM run_events WHERE run_id=%s ORDER BY created_at,id",
            (first["run_id"],),
        )
    bookings = [b for t in (*report["learning"], *report["probes"]) for b in t["bookings"]]
    original = (events, report["messages"], bookings, report["owner_messages"])
    assert transfer.evaluate(*original)["complete"]
    altered = copy.deepcopy(original)
    next(m for m in altered[1] if m["author"] == "casey" and m["title"] == "meeting-01")[
        "recipients"
    ] = ["amy", "ben"]
    assert (
        "meeting-01: requests must use separate private threads"
        in transfer.evaluate(*altered)["violations"]
    )
    altered = copy.deepcopy(original)
    first_choice = report["probes"][0]["bookings"][0]
    altered[2].append({**first_choice, "slot_id": "advice-01-amy"})
    assert "advice-01: more than one meeting booked" in transfer.evaluate(*altered)["violations"]
    altered = copy.deepcopy(original)
    altered[3][:] = [m for m in altered[3] if m["content"] != choice_prompt("advice-02")]
    assert (
        "advice-02: started before owner instruction" in transfer.evaluate(*altered)["violations"]
    )
    altered = copy.deepcopy(original)
    next(b for b in altered[2] if b["slot_id"] == "advice-01-ben")["slot_id"] = "advice-01-amy"
    assert (
        "advice-01: booking without reading the selected valid reply"
        in transfer.evaluate(*altered)["violations"]
    )
    booked_at = next(
        e["created_at"]
        for e in events
        if e["kind"] == "web_booked" and e["payload"]["booking_id"] == first_choice["booking_id"]
    )
    prefix = transfer.evaluate(
        [e for e in events if e["created_at"] <= booked_at],
        [m for m in report["messages"] if m["created_at"] <= booked_at],
        [b for b in bookings if b["sim_s"] <= first_choice["sim_s"]],
        [m for m in report["owner_messages"] if m["created_at"] <= booked_at],
    )
    assert prefix["probes"][0]["selected_owner"] == "ben"
    assert prefix["probes"][0]["decision_read_contacts"] == ["ben"]
    assert prefix["probes"][0]["complete"] is False
    assert prefix["violations"] == []


async def test_resume_after_early_choice_waits_for_remaining_reply(
    pool, test_database_url, tmp_path
):
    class PauseAfterChoice(transfer.TransferEngine):
        async def _reconcile(self, ctx):
            report = await transfer.collect(self.pool, ctx.id)
            if report["probes"][0]["bookings"] and not report["probes"][0]["complete"]:
                await self.director.control(ctx.id, "pause")
                return
            await super()._reconcile(ctx)

    experiment = transfer.TransferExperiment()
    experiment.engine = lambda *args: PauseAfterChoice(*args)
    first = await execute_control(test_database_url, tmp_path, experiment=experiment)
    assert first["result"]["reason"] == "operator_pause", first
    assert first["experiment"]["probes"][0]["selected_owner"] == "ben"
    assert first["experiment"]["probes"][1]["owner_instruction"] is None
    delays = first["experiment"]["timing_policy"]["response_delays_by_task"]
    final = await resume_control(test_database_url, tmp_path)
    assert final["experiment"]["complete"], final["experiment"]
    assert final["experiment"]["timing_policy"]["response_delays_by_task"] == delays
    assert final["usage"]["requests"] == 0
