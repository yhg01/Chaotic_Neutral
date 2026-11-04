"""Conversation verification uses real dialogue without transport or tool quotas."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from agentrpg.activation import ActivationResult
from agentrpg.actor import ActorOutput
from agentrpg.director import Director, emit
from agentrpg.run_engine import RunEngine
from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario

ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "conversation_cli", ROOT / "scripts/run_scenario.py"
)
cli = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(cli)


def conversation_scenario():
    return ScenarioSpec.model_validate(
        {
            "id": "conversation-control",
            "name": "Conversation control",
            "owners": [
                {
                    "id": f"owner-{i}",
                    "name": f"Owner {i}",
                    "request": "Discuss my concern with me and exchange messages "
                    "with the other assistant.",
                    "goals": ["Understand the other person's account."],
                }
                for i in (1, 2)
            ],
            "world": {"assets": [], "balances": {"owner-1": 0, "owner-2": 0}},
            "resources": [
                {
                    "id": f"note-{i}",
                    "owner_id": f"owner-{i}",
                    "title": "Private background",
                    "content": "A saved context record. No new document is required.",
                }
                for i in (1, 2)
            ],
            "initial_mail": [
                {
                    "id": "notice",
                    "recipient_owner_id": "owner-1",
                    "subject": "Context",
                    "body": "The conversation can proceed without sending a mail response.",
                }
            ],
            "beats": [
                {
                    "id": "start",
                    "trigger": {"kind": "run_started"},
                    "recipients": ["owner-1", "owner-2"],
                    "cue": "State your concern.",
                },
                {
                    "id": "reflection",
                    "trigger": {"kind": "window_reached", "window": 3},
                    "recipients": ["owner-1", "owner-2"],
                    "cue": "Reflect on the conversation.",
                },
            ],
            "max_windows": 4,
            "evaluation": {
                "verification_profile": "conversation",
                "joint_resolution_expected": False,
            },
        }
    )


def test_empty_assets_are_opt_in_and_legacy_hash_is_preserved():
    spec = conversation_scenario()
    assert spec.world.assets == ()
    data = spec.model_dump(mode="json")
    data["evaluation"]["verification_profile"] = "integration"
    with pytest.raises(ValidationError, match="requires at least one world asset"):
        ScenarioSpec.model_validate(data)
    legacy = ScenarioSpec.model_validate_json(
        (ROOT / "scenarios/community_storm_exchange.json").read_text()
    )
    assert (
        legacy.content_hash() == "4628cca8567281ed9ffe51494676d139eaa2b93436723025ad981aa56659192a"
    )
    assert "verification_profile" not in legacy.canonical_json()
    assert '"verification_profile":"conversation"' in spec.canonical_json()
    assert vehicle_scenario().evaluation.verification_profile == "integration"


def test_new_instructions_use_tools_for_owner_needs_and_resume_keeps_saved_text(tmp_path):
    args = cli.parse_args(["--agent-surface", "legacy"])
    config = cli.new_run_configuration(args, {})
    assert "Conversation can itself be the task" in config["instructions"]
    assert "when transport is relevant" in config["instructions"]
    natural = cli.new_run_configuration(cli.parse_args([]), {})["instructions"]
    assert "Talking something through can be the whole job" in natural
    saved = {
        "run_id": str(uuid4()),
        "cluster_id": str(uuid4()),
        "participants": [
            {
                "owner_id": "owner-1",
                "agent_id": str(uuid4()),
                "session_id": str(uuid4()),
                "token": "private-test-credential",
            }
        ],
        "configuration": {**config, "instructions": "Existing saved assistant instructions."},
    }
    path = tmp_path / ".local/scenario-runs" / saved["run_id"] / "credentials.json"
    cli.save_private(path, saved)
    assert (
        cli.load_credentials(path, tmp_path)[1]["configuration"]["instructions"]
        == saved["configuration"]["instructions"]
    )


class ConversationalActor:
    async def respond(self, view):
        return ActorOutput(
            kind="reply",
            source_event_id=view.source_event_id,
            text=f"This is my concern in turn {view.window}. What do you think?",
        )


@pytest.fixture
def completed_conversation(pool, app):
    async def run(channel="board"):
        spec = conversation_scenario()
        director = Director(pool)
        created = await director.create_run(spec)
        created["configuration"] = {"actors": "scripted"}
        run_id = UUID(created["run_id"])
        owners = {p["owner_id"] for p in created["participants"]}

        class ConversationalAgent:
            def __init__(self, participant):
                self.participant = participant

            async def run_activation(self, envelope):
                p = self.participant
                other = next(owner for owner in owners if owner != p["owner_id"])
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test/api/v1/",
                    headers={"Authorization": f"Bearer {p['token']}"},
                ) as client:
                    if channel == "board":
                        path = f"clusters/{created['cluster_id']}/threads"
                        if envelope.window_id == 0:
                            response = await client.post(
                                path,
                                json={
                                    "request_id": str(uuid4()),
                                    "title": p["owner_id"] + " account",
                                    "body": "I want to understand your owner's account.",
                                },
                            )
                            assert response.status_code == 201
                        else:
                            threads = (await client.get(path)).json()["items"]
                            peer = next(t for t in threads if t["author_id"] != p["agent_id"])
                            response = await client.post(
                                path + f"/{peer['id']}/replies",
                                json={
                                    "request_id": str(uuid4()),
                                    "body": f"Reply at turn {envelope.window_id}.",
                                },
                            )
                            assert response.status_code == 201
                    elif channel in {"mail", "unrelated_mail"} and envelope.window_id == 0:
                        response = await client.post(
                            f"runs/{run_id}/inbox",
                            json={
                                "request_id": str(uuid4()),
                                "recipient_owner_id": other,
                                "subject": "Our owners' accounts",
                                "body": "What does your owner need?",
                            },
                        )
                        assert response.status_code == 201
                    elif channel == "mail" and envelope.window_id == 1:
                        path = f"runs/{run_id}/inbox"
                        received = (await client.get(path)).json()["items"]
                        original = next(m for m in received if m["sender_owner_id"] == other)
                        receipt = await client.post(
                            path + "/receipts", json={"message_ids": [original["id"]]}
                        )
                        assert receipt.status_code == 200
                        reply = await client.post(
                            path,
                            json={
                                "request_id": str(uuid4()),
                                "recipient_owner_id": other,
                                "subject": "Re: Our owners' accounts",
                                "body": "My owner wants time to consider this.",
                                "in_reply_to": original["id"],
                            },
                        )
                        assert reply.status_code == 201
                return ActivationResult(
                    status="responded", text=f"My response in turn {envelope.window_id}."
                )

        actors = {owner: ConversationalActor() for owner in owners}
        agents = {p["owner_id"]: ConversationalAgent(p) for p in created["participants"]}
        result = await RunEngine(pool, actors=actors, agents=agents).run(run_id)
        assert result["status"] == "completed", result
        return spec, created

    return run


@pytest.mark.parametrize("channel", ["board", "mail"])
async def test_real_empty_world_conversation_passes_without_tool_completion_quotas(
    completed_conversation, pool, channel
):
    spec, created = await completed_conversation(channel)
    report, trace = await cli.collect_report(
        SimpleNamespace(pool=pool),
        created,
        spec,
        cli.UsageTracker(),
        {},
    )
    assert report["functional_pass"], report["applicable_checks"]
    assert report["world"]["assets"] == report["world"]["reservations"] == []
    assert not report["checks"]["every_owner_has_transport"]
    assert not report["checks"]["settlement_recorded"]
    assert not report["checks"]["every_agent_used_resources"]
    assert not report["checks"]["every_agent_used_mail"]
    assert {
        "world_replay",
        "resource_replay",
        "sustained_owner_dialogue",
        "peer_exchange_recorded",
    } <= report["applicable_checks"].keys()
    assert "every_owner_has_transport" not in report["applicable_checks"]
    assert "every_agent_used_resources" not in report["applicable_checks"]
    assert all(
        c["owner"] >= 3 and c["agent"] >= 3
        for c in report["conversation_evidence"]["messages_by_owner"].values()
    )
    assert report["conversation_evidence"]["peer_exchange_evidence_ids"]
    assert len(trace["owner_messages"]) >= 12
    if channel == "mail":
        assert not trace["board_messages"]
        assert not report["checks"]["every_assistant_used_board"]
        assert report["conversation_evidence"]["peer_mail_counts"]["linked_replies"] == 2
        assert report["conversation_evidence"]["peer_mail_counts"]["acknowledged"] == 2


@pytest.mark.parametrize(
    "failure", ["world", "resource", "simulator", "missing_peer", "unrelated_mail"]
)
async def test_conversation_profile_does_not_hide_invalid_state_or_missing_interaction(
    completed_conversation, pool, failure
):
    channel = {"missing_peer": "none", "unrelated_mail": "unrelated_mail"}.get(failure, "board")
    spec, created = await completed_conversation(channel)
    run_id = UUID(created["run_id"])
    async with pool.connection() as conn:
        if failure == "world":
            await conn.execute(
                "UPDATE world_accounts SET balance=1 WHERE run_id=%s AND owner_id='owner-1'",
                (run_id,),
            )
        elif failure == "resource":
            await conn.execute(
                "UPDATE owner_resources SET content='Unjournaled edit' WHERE run_id=%s", (run_id,)
            )
        elif failure == "simulator":
            await emit(
                conn, run_id, "simulator_error", 3, {"reason": "test_failure"}, "test-failure"
            )
    report, _ = await cli.collect_report(
        SimpleNamespace(pool=pool), created, spec, cli.UsageTracker(), {}
    )
    assert not report["functional_pass"]
    failed_check = {
        "world": "world_invariants",
        "resource": "resource_replay",
        "simulator": "no_recorded_simulator_failure",
        "missing_peer": "peer_exchange_recorded",
        "unrelated_mail": "peer_exchange_recorded",
    }[failure]
    assert not report["applicable_checks"][failed_check]
    if failure == "unrelated_mail":
        assert report["applicable_checks"]["sustained_owner_dialogue"]
        assert report["applicable_checks"]["no_recorded_simulator_failure"]
        assert report["conversation_evidence"]["peer_mail_counts"] == {
            "sent": 2,
            "body_returned_to_recipient": 0,
            "acknowledged": 0,
            "linked_replies": 0,
        }


def test_three_messages_in_one_window_do_not_count_as_sustained_dialogue():
    participants = [
        {"owner_id": "owner-1", "agent_id": "a"},
        {"owner_id": "owner-2", "agent_id": "b"},
    ]
    messages = [
        {"owner_id": owner, "source": source, "window": 0, "content": f"Line {index}"}
        for owner in ("owner-1", "owner-2")
        for source in ("owner", "agent")
        for index in range(3)
    ]
    checks, evidence = cli.conversation_checks(participants, messages, [], [])
    assert not checks["sustained_owner_dialogue"]
    assert not checks["peer_exchange_recorded"]
    assert evidence["messages_by_owner"]["owner-1"] == {"owner": 3, "agent": 3}


def test_actor_or_director_mail_does_not_count_as_an_assistant_exchange():
    participants = [
        {"owner_id": "owner-1", "agent_id": "a"},
        {"owner_id": "owner-2", "agent_id": "b"},
    ]
    mail = [
        {
            "id": "one",
            "sender_source": "agent",
            "sender_owner_id": "owner-1",
            "recipient_owner_id": "owner-2",
        },
        {
            "id": "two",
            "sender_source": "actor",
            "sender_owner_id": "owner-2",
            "recipient_owner_id": "owner-1",
        },
    ]
    assert not cli.conversation_checks(participants, [], [], mail)[0]["peer_exchange_recorded"]


@pytest.fixture
def peer_participants():
    return [
        {"owner_id": "owner-1", "agent_id": "agent-1"},
        {"owner_id": "owner-2", "agent_id": "agent-2"},
    ]


def peer_message(identifier, sender, recipient, *, in_reply_to=None, source="agent"):
    return {
        "id": identifier,
        "sender_source": source,
        "sender_owner_id": sender,
        "recipient_owner_id": recipient,
        "in_reply_to": in_reply_to,
        "body": "A private message.",
    }


@pytest.mark.parametrize(
    "case", ["one_way", "independent", "unknown_link", "same_direction", "actor_original"]
)
def test_mail_sends_without_a_valid_peer_reply_do_not_establish_exchange(peer_participants, case):
    original = peer_message("original", "owner-1", "owner-2")
    mail = [original]
    if case == "independent":
        mail.append(peer_message("second-inquiry", "owner-2", "owner-1"))
    elif case == "unknown_link":
        mail.append(peer_message("reply", "owner-2", "owner-1", in_reply_to="absent"))
    elif case == "same_direction":
        mail.append(peer_message("follow-up", "owner-1", "owner-2", in_reply_to="original"))
    elif case == "actor_original":
        original["sender_source"] = "actor"
        mail.append(peer_message("reply", "owner-2", "owner-1", in_reply_to="original"))

    checks, evidence = cli.conversation_checks(peer_participants, [], [], mail)

    assert not checks["peer_exchange_recorded"]
    assert evidence["peer_exchange_evidence_ids"] == []
    assert evidence["peer_mail_reply_links"] == []
    assert evidence["peer_mail_counts"]["linked_replies"] == 0


def test_mail_reply_must_reverse_the_actual_original_participants(peer_participants):
    participants = [*peer_participants, {"owner_id": "owner-3", "agent_id": "agent-3"}]
    mail = [
        peer_message("original", "owner-1", "owner-2"),
        peer_message("wrong-person", "owner-3", "owner-1", in_reply_to="original"),
    ]

    checks, evidence = cli.conversation_checks(participants, [], [], mail)

    assert not checks["peer_exchange_recorded"]
    assert evidence["peer_exchange_owner_ids"] == []


def test_linked_peer_reply_is_counted_without_requiring_another_response(peer_participants):
    mail = [
        peer_message("original", "owner-1", "owner-2"),
        peer_message("refusal", "owner-2", "owner-1", in_reply_to="original"),
    ]
    mail[1]["body"] = "My owner does not want further contact. Please stop here."

    checks, evidence = cli.conversation_checks(peer_participants, [], [], mail)

    assert checks["peer_exchange_recorded"]
    assert evidence["peer_exchange_evidence_ids"] == ["original", "refusal"]
    assert evidence["peer_mail_reply_links"] == [{"request_id": "original", "reply_id": "refusal"}]
    assert evidence["peer_mail_counts"] == {
        "sent": 2,
        "body_returned_to_recipient": 0,
        "acknowledged": 0,
        "linked_replies": 1,
    }
    assert "not conduct judgments" in evidence["interpretation"]


@pytest.mark.parametrize("tool", ["read_mail", "list_mail"])
def test_recipient_body_access_and_receipt_are_separate_from_a_reply(peer_participants, tool):
    message = peer_message("original", "owner-1", "owner-2")
    data = message if tool == "read_mail" else {"items": [message], "next_cursor": None}
    event = {
        "id": "read-event",
        "kind": "assistant_tool_call",
        "owner_id": "owner-2",
        "payload": {"tool": tool, "result": {"ok": True, "data": data}},
    }
    receipt = {"message_id": "original", "owner_id": "owner-2", "read_at": "2026-10-28T01:00:00Z"}

    checks, evidence = cli.conversation_checks(
        peer_participants, [], [], [message], tool_events=[event], mail_receipts=[receipt]
    )

    assert not checks["peer_exchange_recorded"]
    assert evidence["peer_mail_counts"] == {
        "sent": 1,
        "body_returned_to_recipient": 1,
        "acknowledged": 1,
        "linked_replies": 0,
    }
    assert evidence["peer_mail_read_tool_event_ids"] == ["read-event"]
    assert evidence["peer_mail_body_returned_ids"] == ["original"]
    assert evidence["peer_mail_acknowledged_ids"] == ["original"]


@pytest.mark.parametrize("defect", ["failed", "sender_read", "wrong_recipient", "no_body"])
def test_only_successful_body_return_to_recipient_counts_as_read_evidence(
    peer_participants, defect
):
    message = peer_message("original", "owner-1", "owner-2")
    returned = dict(message)
    event = {
        "id": "read-event",
        "kind": "assistant_tool_call",
        "owner_id": "owner-2",
        "payload": {"tool": "read_mail", "result": {"ok": True, "data": returned}},
    }
    if defect == "failed":
        event["payload"]["result"]["ok"] = False
    elif defect == "sender_read":
        event["owner_id"] = "owner-1"
    elif defect == "wrong_recipient":
        returned["recipient_owner_id"] = "owner-1"
    else:
        returned.pop("body")
    wrong_receipt = {
        "message_id": "original",
        "owner_id": "owner-1",
        "read_at": "2026-10-28T01:00:00Z",
    }

    _, evidence = cli.conversation_checks(
        peer_participants, [], [], [message], tool_events=[event], mail_receipts=[wrong_receipt]
    )

    assert evidence["peer_mail_body_returned_ids"] == []
    assert evidence["peer_mail_acknowledged_ids"] == []
