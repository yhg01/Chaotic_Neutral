# Elapsed simulated-time Clock

Date: 9 November 2026. Contract: `elapsed-seconds-v1`.

`SimulationClock` is an opt-in Director-side component in
`src/agentrpg/simulation_clock.py`, persisted by migration 018. It provides a
real elapsed-time coordinate and a durable event queue. It is independent of
the legacy `RunEngine`, which still uses Actor, Agent and settlement windows.
Old frozen scenarios, prompts, checkpoints and window numbers retain their
original meaning. No legacy window has been renamed as an hour or day.

This stage implements and locally verifies the clock component. It does not
attach an Agent model, world transaction or BOLD OS adapter to that clock.
`task_success` remains null in its snapshots. An unbound Clock has no
`world_run_id`; the follow-up Director binding supplies its actual run ID as
routing metadata. There is no public HTTP route or Agent tool for clock
administration.

## Frozen time rules

| Rule | `elapsed-seconds-v1` contract |
| --- | --- |
| Time coordinate | Whole simulated seconds after an explicit timezone-aware epoch; normalized to UTC. Numeric epochs, naive timestamps and fractional epoch seconds are rejected. |
| Horizon | A positive integer number of seconds, up to 31,536,000. Seven days means exactly 604,800 seconds. |
| Cutoff | The active interval is `[0, horizon_seconds)`. At the horizon the clock becomes terminal. Events exactly at or after it remain pending and are reported. |
| Event order | Ascending `(at_seconds, priority, ASCII event_key)`. Stable request/event IDs are part of the recorded inputs. No retroactive insert before the committed cursor is accepted, including within the same second. |
| Parallel progress | Each owner has distinct Actor and Agent lanes. There are up to 15 owners and 30 lanes. Different lanes may have overlapping work durations; the next global time is the earliest queued event, rather than the sum of parallel durations. |
| Decision barrier | A ready lane must select work or a timed wait before the clock jumps to a later second. Events at the current second may still be delivered. An unresolved decision cannot be skipped to manufacture elapsed days. |
| Generation duration | `max(1, ceil(60 * output_tokens / output_tokens_per_minute))`. The positive integer rate is frozen. Tokens must be explicitly supplied; missing usage is rejected. Wall-clock provider latency is not fictional time. |
| Tool duration | A positive integer duration must exist in the frozen `tool_seconds` map. Unknown tools are rejected. A completion event establishes that clock duration elapsed; it is not a bank receipt or proof that a tool succeeded. |
| Timed wait | A ready lane chooses an explicit `until_seconds > now_seconds`. Selected external event kinds may interrupt that wait. The original timer is cancelled in the same transaction as the wakeup. |
| Busy lane | External notifications are retained at their scheduled time but do not cancel running work. Completion makes that lane ready. A later adapter must consume the retained notification when preparing its job. |
| Pause | Pause and resume persist without moving simulated time. At a terminal horizon, resume is rejected. |
| Capacity | The frozen total event cap is at most 100,000, including cancelled and delivered records. Input payloads are at most 8,192 bytes. Exhaustion fails atomically; it never silently drops events. |

The token rate and tool durations are **simulation parameters**, not provider
performance measurements. The controls use 600 output tokens/minute, a supplied
600-token generation and a synthetic seven-second tool. Their parallel
completions are second 60 and second 7. A production experiment must freeze its
own declared parameters and report them. It must not infer missing duration or
success from free-form model text.

An event scheduled after other events at the current second must sort after the
committed cursor. Otherwise it is rejected. This makes a late commit visible
instead of silently rewriting history. New causal work has a minimum duration
of one second; callers cannot use zero-duration work to create an endless
same-time execution loop. The event cap remains an additional bound.

## Persistence and recovery

The four new tables store immutable-through-service contracts, process lanes,
events and operation receipts. Each command locks only its own clock row in a
short transaction. Other clocks and the existing board remain independent.
The service makes no external call while holding that transaction.

Use a stable UUID `request_id` for each command. An identical retry returns its
saved response, including after another process resumes the clock. Changed
content under that ID raises `ClockConflict`. External events also have stable
keys: identical resubmission cannot repeat delivery, while changed event content
conflicts. Keys and receipts are scoped to the clock.

Time advancement, timer cancellation, process wakeup, event delivery and the
operation receipt commit together. A local PostgreSQL failure injected just
before receipt storage verifies that all these changes roll back. Retrying the
same request then delivers once. The service has no destructive reset or
budget operation.

Snapshots distinguish `horizon`, pending events and `queue_drained`. They never
claim business success. The pending event sample is limited to 100 rows and
includes an explicit truncation flag and total counts.

## Local validation

Run in a **separate PostgreSQL test database**:

```sh
uv run python scripts/validate_simulation_clock.py
```

`TEST_DATABASE_URL` must name a database containing `test`. The command creates
one randomly named validation schema, applies migrations only there, and removes
only that schema on exit. Credentials stay in the environment. The development
database, prior trajectories and model campaign are not used by the controls.

The command exercises one hour, one day and seven days with 15 owner pairs. It
uses supplied clock choices, two overlapping work intervals and hourly reminder
events. For each control, a separate Python process reads a persisted pause and
replays a committed step without advancing time or repeating delivery. Events
just before, at and after the cutoff test the boundary. This is a clock test,
not a model conversation or successful task script.

Results: [control report](simulation-clock-control-results.json) and
[verification summary](simulation-clock-validation-results.json).
The initial focused suite passes 114 checks. The final complete suite passes
1,290 checks in 109.30 seconds, with no failures, errors or skips. It includes
39 new Clock cases. The supplied controls deliver 3, 26 and 170 events for the
hour, day and seven-day horizons, with 32 events retained at each cutoff
(30 lane timers plus the exact-boundary and post-boundary markers).
Test Clock IDs belong to a deleted test schema and are not trajectory URLs.
The current web console continues to replay legacy environment runs.

## Integration gates before medium chaos

The [Director–Clock follow-up](DIRECTOR_CLOCK.md) verifies durable dispatch and
private dialogue, including process termination and atomic result delivery.
It addresses the job/notification portion of gate 1 below. It is not a live
provider adapter or timed world-tool execution profile; those parts remain open.
The subsequent [timed-transfer extension](CLOCK_TRANSFERS.md) verifies the first
world-tool completion path with actual simulated debit/credit and native
receipts. Broader timed tools and the live-model driver remain separate gates.

The seven-day preparation plan remains non-executable. The following work is
required before a model experiment can use the new clock:

1. **Durable Director adapter.** Bind a new versioned world-run type to a clock.
   Save dispatch jobs and notification consumption with stable clock/event/job
   identities. Store returned output before applying it. Resume an unknown
   dispatch without making a duplicate provider call or world effect. An
   in-flight generation with unknown token usage must block advancement past
   its unresolved lane. Existing graph counters and the cumulative cost ledger
   remain authoritative.
2. **Scoped views and world effects.** Supply the current simulated timestamp
   through private participant views. Release declared tool effects at their
   completion event and link actual committed receipts. Preserve authorization,
   settlement and owner privacy. Define queued-notification consumption and
   interruption for real mail, owner chat, board updates and shared objects.
3. **Recurring event producer.** Freeze recurrence, stable instance IDs,
   timezone rules and expiry. Enqueue each instance once; do not backfill missed
   events invisibly. The present controls explicitly enqueue reminders; they
   do not implement a new recurring plot compiler.
4. **Intentional setting and shared objects.** Obtain and freeze the colleague's
   scenario and centralised-object contract. A solicitor, club or two competing
   businesses require different legal simulation facts, ownership and permissions.
   Do not invent those facts during a clock test.
5. **Medium-chaos calibration.** Declare permitted event types, seed, rates,
   severity, exclusions and expected invariants. Separate zero-chaos controls
   from disturbance cases. Clock reminders alone are not medium chaos.
6. **Staged acceptance.** Run a supplied end-to-end world control, then a bounded
   short real-model check and a one-simulated-day rehearsal. Record faults,
   pending tasks, context growth, repeated/no-progress choices, intervention and
   costs. Only then run the seven-simulated-day model experiment.

The same campaign remains capped at USD 10. This stage makes zero provider
requests. It does not authorize resetting historical spend or uncertain costs.
Future model reservations must use the actual remaining allowance, including
Actor and review costs, rather than treating the full USD 10 as new funds.
