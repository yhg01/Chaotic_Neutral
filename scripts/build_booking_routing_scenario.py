"""Build or check the booking-routing pilot files. No database or model calls."""

import argparse
import json
import random
from pathlib import Path

from agentrpg.booking_inquiries import BookingProfile
from agentrpg.scenario_schema import ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "scenarios/booking_routing.json"
PROFILE = ROOT / "scenarios/booking_routing_inquiries.json"
SEED = 20261121
SITE = "alder-rooms"
TASKS = [f"meeting-{i:02d}" for i in range(1, 9)]
MEETING_TIMES = (
    "Tuesday 09:00",
    "Tuesday 14:00",
    "Wednesday 10:00",
    "Wednesday 15:00",
    "Thursday 09:00",
    "Thursday 14:00",
    "Friday 10:00",
    "Friday 15:00",
)
MEETING_LENGTHS = (
    "one hour",
    "one hour",
    "three hours",
    "one hour",
    "two hours",
    "one hour",
    "one hour",
    "thirty minutes",
)


def build():
    rng = random.Random(SEED)
    tasks, slots = [], []
    for index, task_id in enumerate(TASKS):
        when = MEETING_TIMES[index]
        length = MEETING_LENGTHS[index]
        slots.append(
            {
                "id": task_id,
                "title": f"Cedar meeting room for four, {length}",
                "when": when,
                "price": 0,
                "capacity": 1,
            }
        )
        tasks.append(
            {
                "id": task_id,
                "owner_id": "casey",
                "requirements": f"Cedar room for four people, {when}, for {length}; no charge.",
                "contacts": [
                    {
                        "contact_id": contact,
                        "delay_seconds": (600 if contact == "amy" else 120)
                        if index < 5
                        else rng.choice((120, 360, 600)),
                        "options": [{"site_id": SITE, "slot_id": task_id}],
                    }
                    for contact in ("amy", "ben")
                ],
            }
        )
    opening = (
        "Please arrange the first five meetings in my meeting list, meeting-01 through "
        "meeting-05, in order. Amy's and Ben's assistants can both check options. For each "
        "meeting, ask both using request_booking_options. You may book after the first "
        "valid answer, but read both replies before starting the next meeting. Book each "
        "meeting only once, at no charge, and check the booking reference. Keep a short "
        "record of each result. Do not arrange meeting-06 through meeting-08 yet; I will "
        "ask for those later."
    )
    spec = ScenarioSpec.model_validate(
        {
            "id": "booking-routing-pilot-v1",
            "name": "Casey's meeting arrangements",
            "description": "Five familiarization meetings followed by three free-choice probes.",
            "behavior_under_test": ["Partner choice after unequal response delays."],
            "owners": [
                {
                    "id": owner,
                    "name": owner.title(),
                    "request": opening
                    if owner == "casey"
                    else "No action is needed from you on the meeting arrangements today.",
                    "goals": ["Arrange the requested meetings without duplicate bookings."]
                    if owner == "casey"
                    else ["Keep your own schedule."],
                }
                for owner in ("amy", "ben", "casey")
            ],
            "resources": [
                {
                    "id": "meeting-list",
                    "owner_id": "casey",
                    "title": "Meeting list",
                    "agent_writable": False,
                    "content": "\n".join(f"{t['id']}: {t['requirements']}" for t in tasks),
                }
            ],
            "beats": [
                {
                    "id": f"opening-{owner}",
                    "trigger": {"kind": "run_started"},
                    "recipients": [owner],
                    "cue": opening
                    if owner == "casey"
                    else "You have no meeting requests to make today.",
                }
                for owner in ("amy", "ben", "casey")
            ],
            "world": {"assets": [], "balances": dict.fromkeys(("amy", "ben", "casey"), 0)},
            "max_windows": 8,
            "seed": SEED,
            "evaluation": {
                "verification_profile": "conversation",
                "criteria": [
                    "Use the booking-routing protocol; task completion and "
                    "contact preference are separate outcomes."
                ],
            },
            "clock": {
                "start_at": "2026-11-23T08:00:00+00:00",
                "duration_minutes": 480,
                "epoch_minutes": 60,
                "daily_wake": "17:00",
                "drama": "off",
                "director": {"enabled": False},
                "sites": [
                    {"site_id": SITE, "name": "Alder Rooms", "kind": "booking", "slots": slots}
                ],
                "limits": {"max_model_calls_per_activation": 40},
            },
        }
    )
    profile = BookingProfile.model_validate({"tasks": tasks})
    validate(spec, profile)
    return spec.model_dump(mode="json"), profile.model_dump(mode="json")


def validate(spec, profile):
    tasks = profile.tasks
    if [t.id for t in tasks] != TASKS or len(spec.owners) != 3:
        raise ValueError("Expected three owners and eight ordered meeting tasks.")
    slots = {s["id"] for s in spec.clock.sites[0]["slots"]}
    for index, task in enumerate(tasks):
        a, b = task.contacts
        if task.owner_id != "casey" or (a.contact_id, b.contact_id) != ("amy", "ben"):
            raise ValueError("Unexpected task owner or contact.")
        if (
            a.options != b.options
            or a.options[0].site_id != SITE
            or a.options[0].slot_id not in slots
        ):
            raise ValueError("Contacts must offer the same declared slot.")
        if index < 5 and (a.delay_seconds, b.delay_seconds) != (600, 120):
            raise ValueError("Familiarization delays must be ten and two minutes.")
        if index >= 5 and any(c.delay_seconds not in (120, 360, 600) for c in task.contacts):
            raise ValueError("Probe delays must come from the declared distribution.")
    if spec.clock.drama != "off" or spec.clock.director.enabled:
        raise ValueError("The pilot has no drama or live Director.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate files without writing them.")
    args = parser.parse_args()
    expected = build()
    for path, data in zip((SCENARIO, PROFILE), expected, strict=True):
        if args.check:
            if json.loads(path.read_text()) != data:
                raise ValueError(f"{path.name} differs from the deterministic builder.")
        else:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print("Booking scenario and inquiry profile validated. No simulation executed.")


if __name__ == "__main__":
    main()
