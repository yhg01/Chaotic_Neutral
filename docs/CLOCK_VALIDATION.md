# Clock validation and seven-day readiness

Audit date: 8 November 2026. Upstream baseline:
`098b14204b3215a1d77df3946f70d4681a838559`. Local branch:
`codex/clock-validation`. All new runtime decisions are supplied, with zero
provider calls. The requested long-run target is **seven simulated days**,
not seven days of wall-clock uptime.

## Current Clock contract

The implementation is a logical-window conductor in `run_engine.py` and
`director.py`. It runs Actor, Agent and settlement phases, then advances
`current_window`. It stores event and job identities, window boundaries, private
delivery receipts and bounded plot-beat state. It is not a continuous-time
priority-queue scheduler. Provider latency does not advance fictional time.

This audit tests the public implementation. The public repository did not expose
a separate new Clock branch, `medium chaos` configuration, BOLD OS adapter or
centralised-object contract at the recorded baseline. A colleague's unpublished
implementation needs its own commit and audit; these results cannot validate it.

## Executed evidence

The original Director, scenario-schema and wait/wakeup suite passes **81 tests
in 7.09 seconds**. Seven additional boundary probes initially produce **five
passes and two failures in 2.35 seconds**. Both failures reproduce CLOCK-001.
After the validation fix and additional positive/expiry cases, the combined
focused suite passes **92 tests in 9.15 seconds**. The final complete regression
passes **1,198 tests in 104.02 seconds**. Counts overlap. Ruff, formatting and
diff whitespace checks pass.

The machine's inherited PostgreSQL setting initially pointed to an unusable
temporary cluster and the original service refused connections. No existing
data directory was initialized, reset or edited. The audit instead used a
separate temporary PostgreSQL instance and the project's isolated per-test
schemas. An infrastructure setup failure is not a Clock assertion failure.

The structured [results](clock-validation-results.json) retain baseline and
post-fix test outcomes, exact source fingerprints, the complete final regression
result, compatibility checks and probe observations. Test run IDs belong to
temporary schemas that are removed after each test; they are not replay URLs
for the development database. Credentials and private PostgreSQL configuration
remain under ignored `.local/clock-audit/` files.

## Findings

| ID | Finding | Evidence | Disposition |
| --- | --- | --- | --- |
| CLOCK-001 | Scenario compilation accepted a clock cue earlier than its prerequisite could possibly fire. The cue can never execute. | A window-2 cue depends on a window-5 cue. A second reproducer passes through a recurring prerequisite whose earliest eligible occurrence is too late. Both were accepted by the original schema. | Fixed locally. Compilation now rejects provably impossible chronological dependencies before run creation. |
| CLOCK-002 | A run can end as `completed` with `reason=window_limit` while a final post-settlement cue remains undelivered. | A two-window integration probe leaves one final Actor cue undelivered when the horizon closes. | Retained current cutoff behavior. The long-run report must distinguish cutoff, drained work and task success; status alone is insufficient. |
| CLOCK-003 | No explicit mapping from a window to simulated seconds or dates exists. | The current schema exposes window counts and beat windows; it has no run epoch, elapsed simulated seconds, `ready_at` or timed `wait_until` contract. | Seven-day readiness gap. Do not label seven rounds, 100 windows or shifted database timestamps as seven simulated days. |
| CLOCK-004 | Capacity limits are spread across schema, database and operator interfaces. | Runs allow 1–100 windows; beats and snapshots use 0–99. Actor/Agent activation limits default to 20 and cap at 100; beats permit at most ten uses. | Versioned capacity/time design is needed. Seven days at hourly resolution would need 168 intervals, already beyond current bounds. Raising one Python limit alone is insufficient. |
| CLOCK-005 | Medium chaos is not a frozen executable profile in this baseline. | No declared medium profile, event intensity/frequency contract or corresponding runner was found. | Wait for or define the scenario/object/chaos contract before treating an experiment as medium chaos. |

CLOCK-001 computes a conservative earliest possible firing window in dependency
order. Fixed clock beats must not precede it. Recurring beats round that bound
to their next declared occurrence; all beats must remain within expiry and run
bounds. Event-based causes are not assumed to actually occur: the check rejects
provable impossibility, not every scene that might remain unresolved.
Same-window dependencies remain legal, including reversed declaration order.
All **13 committed scenario JSON files** still validate and retain their exact
canonical JSON and content hashes. No canonical field, database schema, model
prompt, permission, budget or settlement rule changed in this fix.

## Additional boundary checks

- A slow participant holds the current window. A second concurrent conductor
  is rejected, and only one completed-window event is saved.
- A failed Agent does not advance time. A new engine retries that original job
  while preserving the successful peer, Actor outputs, input hashes and counts.
- An operator pause during Actor work preserves the window and generated
  output; it is not overridden by worker completion.
- A 100-window run reaches a declared cue at window 99 and saves all 100
  boundaries. Waiting assistants are invoked only at `[0, 99]` and `[0]`,
  rather than being polled through empty windows.
- A recurring prerequisite fires before its dependent cue in the same window.
  A prerequisite later than an expiry is rejected for both recurring and
  event-based dependants.
- Existing tests cover recurring beat bounds, no historical clock backfill,
  future cues, private mail wakeup, saved-output recovery and pause races.

The 100-window probe uses supplied choices and reaches a window cap. It does
not establish model autonomy, simulated-day duration or wall-clock service uptime.

## Preparation stages

The [seven-day preparation plan](medium-chaos-seven-day-plan.json) is explicitly
non-executable and not a `ScenarioSpec`. Null fields are unresolved requirements,
not defaults. It prevents incomplete assumptions from being presented as a
configured experiment.

1. Agree the time contract: epoch, integer simulated-time unit, horizon boundary,
   action durations, generation-time policy, timed waits and deterministic
   same-time ordering. Preserve existing frozen runs on their original contract.
2. Define the selected intentional setting, centralised-object API and permitted
   chaos effects. Freeze the actual medium-level parameters and random seed.
3. Implement/version the required clock and persistence capacity. Verify pending
   events, deadlines, duplicate protection, interruption and original-ID recovery.
4. Run a supplied-decision short smoke test, then a one-simulated-day rehearsal.
   Verify time and accounting before a seven-day experiment.
5. Freeze the selected models/API and cumulative budget. The user intends to
   change API after this Clock round; no API migration is included here.
6. Execute seven simulated days and report the exact simulated horizon, actual
   runtime, pending work, costs, intervention count, retries, invalid simulator
   turns and observed Agent behavior. Keep all failures and resumed invocations.

No seven-day run has been launched. No provider key, campaign or approved
ceiling was changed. The prototype remains a window scheduler with the local
chronological-validation fix; this audit does not silently replace it with a
new time engine.

## 9 November component follow-up

The separate [elapsed-time Clock](SIMULATION_CLOCK.md) now implements the
`elapsed-seconds-v1` contract and passes isolated PostgreSQL controls at 3,600,
86,400 and 604,800 seconds. There are 15 owner pairs and 30 independent lanes.
The final full suite passes 1,290 tests; no provider request is made. This
addresses CLOCK-003/004 at the component level. It does not replace the legacy
window conductor, compile its recurring beats into elapsed time, connect world
effects to the clock, or resolve CLOCK-005. Those integrated acceptance gates
remain explicit in the seven-day preparation plan.
