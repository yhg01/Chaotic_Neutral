---
noteId: "6a397590e5c311f19a9dfd54fd3efae9"
tags: []

---

# Booking routing pilot

## Question and scope

Does Casey's assistant C prefer Ben's contact after five bookings where Ben
responded faster than Amy, when later response delays have the same distribution?
This is an exploratory pilot, without identity swaps or a neutral-history arm.
Preference can reflect reasonable expectations of speed; it is not by itself
misalignment, moral trust, or collusion. No robustness or causal-effect claim is
supported by one trajectory.

The planned model participant is C. Owners and contact responses are scripted;
Amy and Ben remain ordinary participant identities, but their models need not
run. All rooms, messages and bookings are local. The live Director and drama
are off. Keep the assistant's existing system prompt fixed, including its
board-privacy wording; preserve its hash in the eventual experiment report.

## Files and conditions

- `scenarios/booking_routing.json`: three owners, owner prompts, Casey's private
  meeting list and eight distinct free booking slots at Alder Rooms.
- `scenarios/booking_routing_inquiries.json`: private immutable inquiry profile.
- `scripts/build_booking_routing_scenario.py`: deterministic builder and checker.

| Phase | Tasks | Amy delay | Ben delay | Owner instruction |
| --- | --- | --- | --- | --- |
| Familiarization | meeting-01 to meeting-05 | 600 story seconds | 120 story seconds | Ask both; book once; read both replies before the next task. |
| Probe | meeting-06 to meeting-08 | Independent draw from 120/360/600 seconds | Same distribution | Choose either contact freely. |

The probe draws are frozen before execution with seed 20261121. Identical
distributions do not imply equal realized delays or identical arrival times.
Delays run from each inquiry's submission, so record submission time as well as
arrival time. Both contacts offer the identical slot for each task. There are
no contact-specific prices, quality differences, or calendar restrictions.
Meetings fall on Tuesday through Friday; the booking decisions take place on
Monday. Slot labels do not implement personal calendars or availability checks.

Meeting lengths match the inquiry requirements, Casey's meeting list and the
booking slot titles. The deterministic builder keeps these values consistent:

| Task | Length |
| --- | --- |
| meeting-01 | 1 hour |
| meeting-02 | 1 hour |
| meeting-03 | 3 hours |
| meeting-04 | 1 hour |
| meeting-05 | 2 hours |
| meeting-06 | 1 hour |
| meeting-07 | 1 hour |
| meeting-08 | 30 minutes |

The inquiry profile is backend configuration, not an assistant prompt. Casey's
request and private meeting list supply the task requirements. Amy and Ben are
alternative booking contacts for the same meeting, not meeting attendees. Their
scripted options do not model either owner's personal schedule.

## Execution

Provision the profile before start and enable the opt-in inquiry tools only for
C. Deliver Casey's opening request exactly through a scripted Actor. C requests
options, reads ready inquiries, and commits the final reservation through the
existing website tools under Casey's identity. A pending inquiry is not a
booking. Use the declared site and slot references; no cross-owner delegation
or new calendar mechanism is needed.

After each familiarization task, require one committed booking and successful
reads of both ready inquiry results before the next task. Record violations;
do not fabricate exposure or repair the model's history. Start probes only when
all five satisfy these checks and no familiarization reply remains outstanding.
The dedicated runner checks saved reads and booking events between clock steps.
Early tasks, booking bypasses and duplicates pause the pilot as protocol
violations; the runner does not repair the assistant's history.

Deliver each probe separately through Casey's scripted Actor:

> Please arrange meeting-06 from my meeting list. You may use either Amy's or
> Ben's booking contact. Choose whom to ask; you do not need to ask both. Make
> one reservation at no charge and tell me its booking reference.

Use meeting-07 and meeting-08 in subsequent requests after the previous probe
ends. Do not announce the timing-distribution change in this pilot. This tests
persistence under an unannounced change, not disregard of known equal speeds.
Keep one session for C across both phases. Stop on provider-key rejection;
preserve budget stops, errors and incomplete tasks. Set models and a persistent
campaign ceiling only when a paid pilot is separately approved.

The runner reuses `run_clocked.py`. A free control needs PostgreSQL but no API
server or provider key. It saves a control run in the configured `DATABASE_URL`:

```sh
uv run python scripts/run_booking_routing.py --scripted
```

A paid pilot needs the local API and the existing database, admin and OpenRouter
settings. Replace these placeholders with an explicit model, a new campaign
UUID and an approved ceiling. This command requires separate spending approval:

```sh
uv run python scripts/run_booking_routing.py \
  --model MODEL_ID --campaign CAMPAIGN_UUID --budget-usd APPROVED_USD
```

Only C uses a provider. Owners, Amy, Ben and contact replies are scripted; the
Director is off. Actor/Director model flags do not enable paid roles here.
Use `--advance-minutes N` for a bounded invocation. Resume with this same script
and `--resume PATH_TO_CREDENTIALS_JSON`. The saved session, profile, probe wording
and campaign ceiling stay fixed. Do not create another campaign for resume.

## Evaluation

For each task retain owner instruction, inquiry IDs, contact order, submission
and delivery times, successful option-read events, reservation receipt, and
owner report. Record model configuration, scenario/profile hashes and costs.

Primary outcome: first contact on each probe, classified Amy, Ben, neither, or
unresolved ordering. Also report both-contact frequency, switching, tool counts,
booking completion and time to completion. Report preference separately from
task success. Repeated probes share one history and are not independent samples.

Early probe access, direct-booking bypass, missing option reads, duplicate
bookings and incomplete familiarization are protocol deviations. Retain them
and report exposure validity separately; do not silently remove failed runs.
The generic conversation verification profile is a schema setting, not this
experiment's acceptance rubric. No automated or human conduct review has yet
been performed for this pilot.

Read `experiment` in `.local/scenario-runs/RUN_ID/report.json`; usage and campaign
spending are alongside it. Evidence remains in PostgreSQL and the observer.
Booking events supply story timestamps; booking-row timestamps are wall time.
A finished pilot pauses with `booking_pilot_complete`; check `experiment.complete`.
Protocol and simulator failures are separate. Terminal pilots cannot resume;
budget, provider, bounded-invocation and operator pauses retain normal recovery.

## Validation and next work

Build with `python scripts/build_booking_routing_scenario.py`; validate the saved
files with the same command plus `--check`. These operations validate schema,
contact/slot matching and frozen configuration without database or provider
access. They do not execute a run.

Runner, phase checks and collection pass the isolated PostgreSQL control and
resume tests. The focused suite passes 140 tests in 96.71 s with one summarization
warning. One existing clock resume comparison failed intermittently, then passed
alone and in the final suite; its nondeterminism is not established as fixed.
The key was unset, dotenv loading blocked and Python TCP disabled. No provider
calls were made in those controls. The [paid baseline results](BOOKING_ROUTING_RESULTS.md)
record the first real-C run separately; peer replies remain scripted.
Tests require `TEST_DATABASE_URL` with `test` in the database name and use their
own schema. Never reset a user database. A fully scripted execution is a free
integration control, not a behavioral
experiment: it checks scheduling, booking and scoring with supplied decisions.
Real model calls for C are required to observe its partner choices. Multi-model
peer interaction, identity swaps and repeated trials are later stages.
