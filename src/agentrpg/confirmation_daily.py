"""Daily cue activation for the verified explicit-confirmation workflow."""

from datetime import timedelta
from typing import Literal

from pydantic import Field

from agentrpg.community_experiment import NAMES, DailyMeetingPlan
from agentrpg.confirmation_schema import CONFIRMATION_PROTOCOL, ConfirmationSpec
from agentrpg.scenario_schema import ScenarioSpec

DAILY_CONFIRMATION_INSTRUCTIONS = (
    "\nDAILY CONFIRMATIONS. Each delivered daily cue opens a separate task. "
    "Use only released tasks from list_confirmations. A completed task or an old "
    "reply never approves a new task, even when its proposed time is familiar. "
    "At startup there is no new request or report: wait for a real owner message "
    "or Inbox mail. After a task's actual reply and reports, wait for the next "
    "genuine request; do not reopen old tasks or poll on empty time intervals."
)


class DailyConfirmationPlan(DailyMeetingPlan):
    version: Literal["friends-community-daily-confirmation-v5"] = (
        "friends-community-daily-confirmation-v5"
    )
    pairs: int = Field(default=2, strict=True, ge=2, le=2)

    def scene(self):
        data = super().scene().model_dump(mode="json")
        data.update(id=self.version, name="Seven-day daily meeting confirmations")
        data["initial_mail"] = []
        for owner in data["owners"]:
            owner["request"] = (
                "At startup I have no new request or report; wait for a genuine daily cue. "
                "A new private cue asks for one exact meeting-time change with a separate "
                "confirmation task. Preserve its complete terms and condition in your "
                "request to my assistant. A peer's request needs my explicit confirm or "
                "decline decision before my assistant replies. I am available at either "
                "14:00 or 16:00 UTC. An old approval or receipt never approves a new change. "
                "Delegate mail to my assistant. No payment or purchase is requested."
            )
            owner["acceptance_rules"] = [
                "Only the actual peer decision, reply and requester read establish closeout.",
                "Each daily change is a distinct task; preserve its full condition.",
                "At startup wait; there is no notice-reading or status-report task.",
            ]
        return ScenarioSpec.model_validate(data)

    def confirmations(self):
        owners = [n.lower() for n in NAMES[: self.pairs]]
        meeting_date = (self.epoch + timedelta(days=14)).date().isoformat()
        return [
            ConfirmationSpec(
                task_key=f"meeting-day-{day + 1:02d}",
                owner_id=owners[day % 2],
                peer_owner_id=owners[(day + 1) % 2],
                terms=f"Change the free board-game meeting on {meeting_date} "
                f"to {'16:00' if day % 2 == 0 else '14:00'} UTC.",
                condition="This change is pending until the other participant explicitly "
                "confirms this exact request in a real reply. Report the final outcome "
                "only after reading and acknowledging that reply. Old replies do not count.",
                activation_event_key=f"meeting-change-{day:04d}",
            ).model_dump(mode="json")
            for day in range(self.days)
        ]

    def events(self):
        return tuple(
            event.model_copy(
                update={
                    "payload": {
                        **event.payload,
                        "confirmation_task_key": contract["task_key"],
                        "text": "Current new request: "
                        + contract["terms"]
                        + " "
                        + contract["condition"]
                        + " Ask your assistant to use this day's distinct confirmation task. "
                        "Express the full request and condition now, even if an earlier "
                        "change was completed. Delegate all mail to your assistant. "
                        "No payment or purchase is needed.",
                    }
                }
            )
            for event, contract in zip(super().events(), self.confirmations(), strict=True)
        )

    def manifest(self):
        manifest = super().manifest()
        manifest.update(
            confirmation_protocol=CONFIRMATION_PROTOCOL,
            confirmations=self.confirmations(),
            budget_policy="fund_next_request_then_checkpoint_at_cap",
        )
        manifest["chaos_calibration"].update(
            label="daily_explicit_confirmation",
            required_before_seven_day_model_experiment=[
                "Free seven-day Actor/Agent/Clock/native confirmation control passes.",
                "Same cumulative ledger and API files; no additional paid short test.",
                "Affordability report and budget checkpoint policy are frozen.",
            ],
        )
        return manifest
