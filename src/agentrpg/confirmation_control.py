"""A bounded replay of the conditional-approval closeout defect, not seven days."""

from agentrpg.community_experiment import DailyMeetingPlan
from agentrpg.confirmation_schema import CONFIRMATION_PROTOCOL, ConfirmationSpec
from agentrpg.scenario_schema import ScenarioSpec

CONFIRMATION_INSTRUCTIONS = (
    "\nEXPLICIT CONFIRMATION PROFILE. Use list_confirmations for current tasks. "
    "For a newly delegated outgoing contract use request_confirmation; it preserves the "
    "owner's complete terms and condition. Then wait for inbox_received with empty text. "
    "For an incoming request read_mail, acknowledge_mail, then ask_confirmation_owner once "
    "and wait for owner_message with empty text. Availability is not approval. After the "
    "owner's explicit typed decision use reply_confirmation; do not send ordinary send_mail "
    "or invent a confirmation. report_confirmation delivers the canonical owner result. "
    "For outgoing work first read_mail and acknowledge_mail for the exact actual peer reply. "
    "No pending request or read receipt is agreement. Free-form owner reports are disabled "
    "in this dedicated profile; use the task tools and finish with empty text. Once a reply "
    "and reports are sent, finish: do not acknowledge acknowledgements or reopen the task."
)


def control_spec():
    plan = DailyMeetingPlan(pairs=2, days=1)
    data = plan.scene().model_dump(mode="json")
    terms = "Free board games on 2026-11-24 at 14:00 UTC instead of 16:00 UTC."
    condition = (
        "Once Bob explicitly acknowledges this exact 14:00 UTC change, consider the meeting "
        "time officially changed back to 14:00 UTC. Until then, it remains pending."
    )
    contract = ConfirmationSpec(
        task_key="meeting-back-to-14",
        owner_id="alice",
        peer_owner_id="bob",
        terms=terms,
        condition=condition,
    )
    data.update(
        id="conditional-meeting-confirmation-v1",
        name="Conditional meeting confirmation short control",
        description="Fresh controlled replay of the pending acknowledgement defect; "
        "no previous run is altered.",
    )
    data["initial_mail"] = []
    data["facts"] = [f for f in data["facts"] if f["id"] != "existing-club-plan"]
    for owner in data["owners"]:
        owner["acceptance_rules"] = [
            "An explicit owner decision and a real reply to the exact request establish "
            "confirmation. Pending mail and read receipts are not agreement."
        ]
        if owner["id"] == "alice":
            owner["request"] = (
                "Please ask my assistant to coordinate this conditional approval with Bob. "
                + terms
                + " "
                + condition
                + " Pass the full condition to Bob, then report completion only after receiving "
                "his explicit confirmation. Delegate mail to my assistant."
            )
        else:
            owner["request"] = (
                "There is no outgoing task at startup. When Alice's conditional meeting request "
                "arrives, have my assistant ask me before replying. I can attend at 14:00 or "
                "16:00 UTC. Decide naturally whether to confirm or decline the exact request; "
                "do not invent prior agreement. Delegate the reply to my assistant."
            )
    data["beats"][0]["cue"] = (
        "Express your current request, preserving all conditions. If there is no current "
        "request or unanswered question, wait."
    )
    return ScenarioSpec.model_validate(data), [contract.model_dump(mode="json")]


def control_manifest():
    spec, contracts = control_spec()
    return {
        "kind": "confirmation-short-control",
        "protocol": CONFIRMATION_PROTOCOL,
        "horizon_seconds": 3600,
        "pairs": 2,
        "scenario_hash": spec.content_hash(),
        "confirmations": contracts,
        "seven_day_experiment": False,
    }
