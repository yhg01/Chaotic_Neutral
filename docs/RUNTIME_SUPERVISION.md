# Durable runtime supervision

The first stability step is implemented as an opt-in policy. It classifies
participant failures, saves retry deadlines and detects executable shopping
tasks that make no business progress. Existing checkpoints, task IDs, consent,
world settlement and the model campaign remain authoritative.

This change does not resume paid testing. The user kept the 5 USD ceiling and
stopped paid calls. The earlier all-model shopping run remains at two of three
purchases. The new live experiment uses supplied decisions and no provider.

The above is the original supervision experiment's budget and model checkpoint.
A later [authorized closure](TASK_CLOSURE.md) completes the existing model shopping
run at 3/3 under 5.5 USD. That continuation does not add multi-hour or model
supervision acceptance.

## Failure handling

| Failure | Behavior |
| --- | --- |
| Known provider transport failure, retryable provider HTTP status, Actor model timeout, transient tool transport failure | Save a classified failure. An enabled supervisor schedules exponential backoff with bounded jitter, then resumes the existing jobs and graph checkpoints. |
| Budget denial, task call/time limit, identity or input conflict, rejected provider request, unknown exception | Preserve the checkpoint and stop automatic recovery. Never infer retryability from exception text. |
| Actor contract violation | Preserve candidate diagnostics and mark the simulation invalid, as before. |
| Operator pause, waiting for events, no-progress pause | Preserve the pause. The supervisor cannot resume it. |

The default policy permits two retries and never exceeds the frozen scenario's
recovery limit. Retry counts come from persisted job attempts. Successful peers
are not executed again. Saved outputs are applied with the existing stable IDs.
Assistant call counters and all confirmed or uncertain model costs remain in
their original ledgers.

Migration 016 adds `failure_kind` and `failure_retryable` to jobs. Each failed
attempt also has an immutable `job_failure` event, so successful recovery does
not erase the failure history. Provider and tool exception bodies are excluded.

`run_supervision` stores the frozen policy, failure signature, retry deadline
and recovery count. The deadline is committed before waiting. Recreating a
process does not start another full delay. The supervisor locks the same run
row as operator controls, rechecks its status and reason, and atomically resumes
only a matching temporary worker failure. It holds no database transaction while
waiting and makes no model call.

Policies must be bound before the first job. Historical runs cannot enable a
new policy on resume, and an existing policy cannot silently change. Legacy
generic errors have no retryable classification and remain stopped. Complete
turn boundaries include supervision and health records; copied task references
belong to the isolated child. An older boundary without required supervision
state is rejected.

## No-progress checks

The current watchdog covers open shopping tasks after committed decision
windows. A task must have sufficient current authority, funds, matching stock
and acceptable price before lack of progress counts against it. Submitted tasks
wait for settlement. Missing authority, stock, funds or acceptable terms is
recorded as a blocked wait, without inventing consent or purchases.

The check uses task state, relevant cart contents and actual order status. Chat
replies, raw cart-version increments, tool counts and unrelated peer activity
cannot reset it. Repeated cart states count as a cycle. The detector records
seen states, so removing and re-adding the same product cannot extend the task
indefinitely. A new business state or changed blocking condition resets the
unchanged-window counter. Rechecking one boundary does not count it twice.

After three unchanged eligible checks, the run pauses with `reason=no_progress`
and attributed `task_stalled` events. It does not cancel a task, reset authority,
mark work complete or automatically resume. The first observation is a baseline,
not one of the three unchanged checks.

This is not a general semantic evaluator. It does not determine whether an
owner's dialogue sounds natural. It does not add a timeout to legitimate
authority, stock or settlement waits, nor override an existing event-wait pause.
Other task types need their own progress evidence before using this watchdog.

## Use

Start the local API, then create a new supplied-control experiment:

```sh
uv run python scripts/try_browser_shop.py --mode control --rounds 30 \
  --supervise --restart-every 6 --inject-checkout-interruption
```

`--retry-delay-seconds` sets the initial delay for a new supervised run. The
default is one second; the maximum backoff is 30 seconds. The saved policy is
used on resume. An explicit CLI resume can resume an operator pause; the
automatic supervisor cannot. If a process dies while a temporary failure is
waiting, CLI resume keeps its existing deadline instead of bypassing it.

The following free probe creates a new control run, terminates only its own
worker after a retry deadline is committed, and resumes in a separate process:

```sh
uv run python scripts/probe_supervision_restart.py
```

It retains the complete recovered report and `restart-probe.json` under the
ignored run folder. A deliberately terminated worker has no final invocation
report; its saved failure, job checkpoint and original deadline remain in
PostgreSQL and in the recovered trace. The probe does not reset development data
or select a provider-backed mode.

## Verified result on 3 November 2026

Run `b4a4de1b-4b5f-45f4-87a1-ceffdca0ef75` passed all seven process-recovery
checks and all eleven shopping checks. The first owned worker was terminated
with SIGTERM during backoff. The second worker retained the same failed job,
original attempt count and retry deadline. There was one scheduled retry and
one recovery. Resume occurred 0.012266 seconds after the saved deadline.

The recovered control completed 92 logical windows and 30 accepted purchases.
One committed checkout was replayed from its saved operation, without another
debit. Alice ended at 560 credits and Bob at 540, conserving the initial 1,100.
The second invocation took 94.260 seconds and recreated its runtime 16 times.
The span between first and last completed windows was 105.080527 seconds;
these are different measurements and neither is multi-hour uptime.

There were zero provider requests. The shared campaign was identical before
and after: 5 USD ceiling, 4.5054840 confirmed, 0.4079752 uncertain, no live
reservations and 0.0865408 available. The earlier paid checkpoint is unchanged.

The focused suite passed 151 tests in 33.60 seconds. The full isolated
PostgreSQL suite passed 1,130 tests in 175.32 seconds. Counts overlap. Tests
cover operator pause during backoff, competing recovery claims, persistent
deadlines, retry exhaustion, safe budget stops, stock/authority waits, progress
cycles, real Agent graph/board replay after a committed post, actual Chrome
checkout replay, and isolated branch state. Ruff, formatting and whitespace
checks passed.

[Safe evidence](runtime-supervision-results.json) contains process IDs, the
original deadline, preserved failure events, exact checks, campaign snapshots
and source hashes. The [shopping evidence](browser-shop-results.json) retains
the new control alongside all earlier failed and passing runs. The local
trajectory is `/console?run=b4a4de1b-4b5f-45f4-87a1-ceffdca0ef75&turn=2`.
The local API was restarted after confirming zero active jobs. The in-app
observer loaded the completed run and showed both recovery events. Inspecting
the saved resume event confirmed the original job ID and preserved attempt.
[Observer screenshot](images/runtime-supervision-recovery.png).

Remaining work includes event-driven scheduling, bounded long-term memory,
model-behavior repairs, an operating-system service restart policy and longer
model trials under separately authorized spending. This result verifies the
recovery layer with supplied decisions; it does not establish model autonomy.

The [autonomy acceptance tools](AUTONOMY_ACCEPTANCE.md) add frozen repeated
controls and hash-only invocation/control audit. They distinguish automatic
recovery from Director/console operation and keep old coverage unknown. This
measurement does not add an operating-system restart policy or paid-model proof.
