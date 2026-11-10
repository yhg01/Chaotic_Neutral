# Actor and personal Agent execution on the elapsed Clock

The next [mixed protocol](MEDIUM_CHAOS_PROTOCOL.md) freezes three independent
pairs and all four existing event categories. Its canonical operator definition,
acceptance rules and free native integration evidence are separate from the
older candidate and completed daily-confirmation cohort below. This round
starts no paid run and changes no model/API profile. The existing paid
`community` option still selects its older candidate, not this new definition.

## Current declared-cue repair

The [cue-response repair](CUE_RESPONSE_REPAIR.md) passes 1453 local tests and a
two-event real-model short test. The new trusted owner_response_required cue
metadata cannot be canceled by old completion or a false model classification.
Exact coverage is required and silent delivery is blocked. Both requests reached
the Agents and received actual peer approval. Second-exchange acknowledgment
closeout remains incomplete. Models/APIs and original generation/review prompt
strings remain; the classifier/validation protocol and scene are versioned.
No seven-day model rerun was started. Later sections retain their historical scope.


## Authorized seven-day outcome — 10 November 2026

The [simplified seven-day experiment](SEVEN_DAY_SIMPLIFIED_RESULTS.md) completed
the 604800-second horizon but failed daily-change acceptance. All seven cues
reached their Actor inputs; three new opposite-time needs were met with silent
wait accepted by independent review. Only one actual change occurred. The later
preparation sections below are historical stages. No runtime or model/API change
was made during this experiment. Original evidence and checkpoints remain.


This opt-in profile connects the existing owner `ModelActor`, its independent
reviewer, and the actual Deep Agents graph to saved Director/Clock jobs. It does
not replace the model with a scripted action loop. Local controls use supplied
responses and must be reported separately from provider runs.

## Latest status — 10 November 2026

The [simplified daily-meeting report](DAILY_MEETING_REHEARSAL.md) supersedes the
older rehearsal status sections below. Current source passes 1423 local tests.
A two-person actual-model day completed 86400 seconds with one effective change,
two native mails and no payment. All-role planning forecasts pass for that
smaller profile; seven-day model execution remains deliberately closed.
The older three-person four-cue calibration remains incomplete.

## Execution contract

1. Freeze a new scenario, its Clock, role bindings, instructions, tool-time
   policy, limits and optional recurrence plan. Do not convert old windows to
   elapsed seconds. Each owner has one Actor and one personal Agent.
2. An Actor receives its own request, conversation, account, private mailbox
   and addressed cues. Its existing model and independent review produce the
   response. Save the response and review diagnostics before timing it.
3. Release the owner message and exact typed transfer permission only at the
   saved Actor generation completion. An Agent cannot create that permission.
4. Open one frozen Agent activation in its existing PostgreSQL graph session.
   Stop the real graph at its model/tool boundaries. A model node saves its AI
   response before its output-token duration is applied. Tools cannot run early.
5. A tool batch incurs the declared `graph_tools` duration. After that boundary,
   the graph invokes the actual authenticated HTTP tools with their original
   call IDs. A transfer additionally incurs its native frozen transfer duration.
   Nonpayment tools in one parallel batch share the batch duration. These test
   values are simulation parameters, not latency measurements.
6. A pending payment ends the activation and keeps the lane busy. At its due
   second, the existing native transaction applies the debit, credit, bank mail
   and receipt together. A later activation reads actual evidence and decides
   what to report or mail. The conductor does not insert success dialogue.
7. Deliver a final optional owner reply once. A successfully sent owner-tool
   report suppresses a duplicate finish-text delivery. Material owner replies,
   mail, board posts, receipts and private cues wake the relevant lanes. Empty
   time intervals do not invoke a model.

Actor/reviewer prompt bytes and review gates can be loaded from the existing
credential-free baseline manifest. The personal Agent receives a separate elapsed-time
instruction extension. The selected route remains DeepSeek V3.2; Actors and
reviewers remain GPT-4.1. Resuming with changed role models, prompt hashes,
Agent instructions or identities is rejected.

The original diagram remains unchanged: 13 nodes and 28 connections. Actors
receive views and express requests; they have no board, mail or payment tools.

## Persistence and failure recovery

Migration 021 adds profile, activation, node-step, tool-release, Actor-response
and plan records. The optional tracked profile also reuses the existing native
transfer-task ledger and its payment/notice/owner-report evidence checks. Task
opening or cancellation commits with its actual Actor message; task completion
requires the real committed receipt and delivered message IDs. Existing graph
checkpoints, owner messages, mailbox records,
wallets and campaign ledgers remain the source records.

| Failure boundary | Recovery rule |
| --- | --- |
| Model response is saved in a graph checkpoint, but the outer job receipt is absent | Reconcile the original job from that checkpoint and node record. Do not invoke the provider again. |
| Actor response is saved, but the outer job receipt is absent | Reuse its saved timed output and review diagnostics. |
| Provider result has no durable response evidence | Mark the attempt uncertain and stop. Do not automatically regenerate it. Keep its cost reservation. |
| Tool commits before its graph node is saved | Retry the same graph node and exact call IDs. Native write idempotency and one-use payment grants preserve the original effect. |
| Payment commit is interrupted | The timed-transfer transaction and its original Clock operation provide the before/after-commit guarantees in `CLOCK_TRANSFERS.md`. |
| Model, Actor-turn, activation, dispatch or horizon limit is reached | Retain the checkpoint and pending work. A stopped Clock or horizon is not task success. |
| Automatic context compaction would call a model outside a timed boundary | Stop before that call. Timed compaction is not yet supported by this profile. |

Provider execution occurs outside database transactions. Per-run conductor and
per-session graph leases prevent duplicate dispatch. They do not replace normal
board concurrency with a global board mutex. Native elapsed writes take the
Clock/run locks in that order and require an exact released, scoped tool call.
Legacy board-only and window runs retain their existing routes.

## Friends' community candidate

The user authorized a friends' community as the candidate setting. The declared
[candidate manifest](friends-community-medium-candidate.json) contains three
friends, seven simulated days, one addressed cue per day and seed `20261109`.
Current candidate v3 starts with a declared existing free board-game plan and
owner-scoped native mail. It does not require the models to repeat the initial
club negotiation before each calibration.
This frequency is a preparation candidate until cost calibration passes.
The central shared object is the existing committed message board. Private
Inbox mail and simulated wallet receipts remain separate native records.
There is no BOLD OS integration or new external service.

The recurrence compiler expands all seven events with deterministic keys and
times inside the 604800-second horizon. Reinstallation is idempotent. Events
change a preference or raise a question; they do not create a payment, consent,
booking, reply or successful outcome. Actor/Agent decisions remain model choices.

Candidate event types are meeting preference changes, unclear possible expenses,
questions about an existing payment, and social invitations. The label
`medium_candidate` is provisional. Compare a no-chaos control and a one-day
candidate rehearsal, inspect their actual dialogue, and calibrate frequency and
meaning before calling a seven-day run a medium-chaos experiment.

## Commands and evidence

Preparation uses no credentials and makes no provider call:

```sh
uv run python scripts/run_clock_actors.py --prepare .local/clock-runtime/community-plan.json --days 7 --events-per-day 1
```

Use the private baseline manifest that already contains the unchanged Actor and
review configuration. A read-only preflight checks the existing campaign and
current provider price bounds without migrating the development database:

```sh
uv run python scripts/run_clock_actors.py \
  --baseline-manifest /absolute/path/to/private/credentials.json \
  --preflight .local/clock-runtime/budget-preflight.json
```

After isolated local validation and a verified database backup, a bounded live
invocation applies migrations and retains its new run manifest:

```sh
uv run python scripts/run_clock_actors.py \
  --baseline-manifest /absolute/path/to/private/credentials.json \
  --scenario scenarios/friends_lunch_actors.json --days 1 --no-chaos --max-actions 100

uv run python scripts/run_clock_actors.py \
  --resume .local/clock-model-runs/RUN_ID/credentials.json --max-actions 100
```

The runner requires the existing cumulative campaign
`635aa42e-386f-43dc-b679-b73cd0cd276f` and its user-approved USD 20 ceiling. It does not create
another campaign or reset historical/uncertain exposure. Every actual provider
attempt reserves against the same ledger. Model roles use their separate
configured keys. Credentials are never written into a provider-run report.

Each invocation exports `trajectory.json`, readable `trajectory.md`, native
records, latest graph state, review diagnostics, source/configuration identities,
elapsed time, financial invariants, event order, pending work, checkpoint growth
and before/after costs under ignored `.local/clock-model-runs/`. Failed attempts
are retained. Old trajectory files are unchanged. The existing web console
still displays legacy windows; these exports provide the elapsed-time trace.

These controls do not establish seven-day model reliability or calibrated owner
fidelity. Transport, item commerce and Resource mutation have no elapsed adapter
in this profile. The candidate deliberately uses the implemented board, private
mail and wallet tools. A normal simulation outcome may include unresolved work;
it must be reported separately from simulator errors and false success claims.

Actor-requested internal mail now commits at its generation boundary, with the
original sender and exact recipient. Actor views retain the latest private
assistant wait reason and current world time. New Agent envelopes contain a
frozen Clock context; original dialogue rows remain unchanged.

The first three-owner model control exposed a real integration gap: the original
Actor returned a persistent task while the initial elapsed profile had task
tracking disabled. The run stopped at t=0 without payment and retained the rejected
candidates and USD 0.027308 charge. The subsequent adapter enables the existing
task ledger and task-yield guard together, rather than changing the Actor or
reviewer prompts or discarding the task request.

The three-owner control then exposed a second adapter defect: nonempty text
inside `ActorOutput(kind="wait")` was incorrectly delivered as a fresh owner
instruction. The original window runtime suppresses wait dialogue. The repaired
adapter keeps that raw response as evidence and releases empty timed text; a
new PostgreSQL control verifies zero owner messages and zero Agent calls. The
inspection-paused model run and its charges remain in the campaign.

## Paired turn boundary

Each owner/assistant pair now completes the current role turn before dispatching
its paired reply. Different pairs still advance independently on the same Clock.
A private message or cue that arrives during the paired Agent activation stays
queued. A durable handoff wakes only a process that was explicitly deferred; it
does not generate empty Actor/Agent ping-pong or expose scheduler signals to the
model. This preserves a frozen activation input while ensuring its next owner
answer reaches the next activation.

Same-second tool notifications choose the next causal priority after events
already delivered at that second. This preserves the Clock ordering contract; a
notification is not inserted before its cause. The declared priority bound still
stops an exhausted same-second chain.

The preceding 500-action mixed-workload model control did complete its one
10-credit transfer, recipient notice, owner report and persistent business task.
It did not reach the one-day horizon. Its complete trace exposed repeated
questions while an owner answer was waiting for the activation boundary. That
control is retained as partial progress, not reported as a completed day.

See [seven-day preparation](MEDIUM_CHAOS_READINESS.md) for the candidate setting,
compressed calibration and exact start gate. No seven-day model run is started
by preparing these files.

## Intention-specific candidate revision

The first broad club setting produced open-ended planning and voluntary disclosure
of private account balances. Its partial traces remain evidence of assistant
conduct. A provider failure later stopped one control at 545 seconds without a
durable response for the last model node. Its original job stays uncertain;
it is not silently regenerated and its maximum cost reservations remain charged.

Candidate v2 defines one concrete task: plan a free board-game meeting. Each
owner has a declared private availability note, two possible common times, and
an explicit boundary on account-balance disclosure. The system does not choose
the time, grant consent, insert friend approval or assert that a meeting occurred.
The four-event rehearsal uses a seeded permutation in each block of four, so it
covers all cue types. The original Actor/reviewer prompts and model choices do
not change. The evidence exporter now also saves native board rows and receipts.

## Review delivery metadata and complete exports

The first four-cue rehearsal stopped at 247 seconds, before any intervention.
The independent reviewer rejected two empty `kind=wait` candidates as repeated
spoken deferrals. The candidates were silent yields. The rejected reviews and
all USD 0.20899488 of that run remain recorded; no candidate was auto-accepted.

The Clock driver now opts into delivery metadata. The reviewer receives the
actual visible owner text (or null), a silent-yield flag and declared effect
fields. Its output schema confirms the typed `owner_dialogue_emitted` value.
It must still judge whether silence is appropriate and validate every existing
consent, mail and task gate. An accepted=false judgment still stops execution.
Original Actor/reviewer model IDs, prompt strings, gates and repair limit remain
unchanged. The extra review-input protocol is a separately frozen change, not an
identical review context for causal model comparison. Legacy runs default off.

Current LangGraph message channels store deltas between snapshots. The exporter
now reconstructs their ancestor writes through the original message reducer.
A PostgreSQL test compares exported messages with the actual graph state; the
previous exporter produced an empty list despite preserved database history.
Corrected old exports are separate, indexed copies. Original trajectory bytes
remain unchanged. The summary includes the largest observed model input-token
count, not just serialized input characters. These exports make no model calls.

## Historical v3 preparation

The v2 four-cue run reached a complete day but only the snack question generated
new assistant work. Three cues yielded silence; the payment follow-up also
addressed an owner who had made no outgoing payment request. This is insufficient
treatment exposure, even though Clock delivery and financial checks passed.

Candidate v3 makes the new requests explicit. The status question targets the
actual payer. Planning/invitation dates are future dates derived from each cue's
simulated time. Private cues appear in the Actor's main cue as well as its scoped
observations. A pre-run planning agreement and private availability notices are
native initial conditions, not inserted future approvals or successful actions.
All new replies, plans, permission and outcomes still need actual model decisions.

Readiness now also binds the baseline configuration and personal API route/model.
Changing a prompt, review gate or route in a non-source file cannot reuse a ready
file. Invalid estimates fail before provider access. The budget forecast is
checked before a new world run is provisioned. The same USD 20 campaign and all
historical/uncertain fees remain mandatory.

The final exporter also reconstructs virtual filesystem deltas using the actual
Deep Agents state-schema reducer. A real graph test compares its files with the
export. This preserves scratch notes; it does not add native Resource mutations.
See [the findings and handoff](CLOCK_RUNTIME_FINDINGS.md) for accepted evidence,
retained failures and the current funding/calibration gate.

## Personal provider transport

The latest preparation keeps the original Actor/reviewer models, prompt strings,
review gates and transport settings. Only the personal Agent transport changes:
one request can wait up to 120 seconds and has no automatic transport retry.
This is a frozen execution-configuration change. The prior initial-request failure
and all three uncertain reservations remain saved. A basic and a tool-call probe
succeeded without executing world tools. Their costs remain in the same campaign.

Safe ledger diagnostics retain the exception class or HTTP status without its
message, body or credentials. Unknown outcomes retain the full reservation and
stop the affected job. They do not authorize regeneration or duplicate effects.

Seven-day preparation requires an effective four-cue day, evidence-based cost
and context estimates, and exact source/plan/role bindings. It is separate from
the user's explicit instruction to start. The current instruction is not to start.

## Delivered-candidate review input

The next opt-in review protocol, `visible-wait-delivery-v2`, supplies the reviewer
with the candidate's actual visible dialogue. A `wait` has empty review text;
the full raw generation stays in diagnostics and trajectory. The private view
and all typed candidate fields remain available for review. A wait still cannot
request world effects, and the independent reviewer can reject silence that
ignores a consequential cue or unanswered question. Non-wait dialogue and the
original authority/mail/task gates remain intact. Original model IDs, prompt
strings, gate values and repair allowance are unchanged.

This fixes a retained one-day trial that stopped at 54101 seconds after three
cues: non-delivered raw wait prose confused the review schema, then a generated
spoken acknowledgement failed the repeated-wait review. That stopped run has
one completed native payment task and no duplicated payment. It is not a
completed day or an effective four-cue calibration. Its fees and all original
files remain saved. The review-input protocol is a distinct frozen cohort.

## v4 receipt-note and native task binding

The v3 payment-status cue produced no new receipt check: the Actor confirmed
what it already knew and the assistant only acknowledged monitoring. Four
delivered cues did not establish four effective assistant tasks. Candidate v4
asks for an actual private expense note, or verification of an existing note,
with native receipt fields and a reported location. This uses the Agent's
virtual session scratch state, not a host file or native Resource document.
The v3 event-draw namespace is explicit so changing this one cue does not
silently reshuffle the other kinds or recipients.

The v3 snack inquiry did obtain Bob's actual 8-credit answer and Charlie's
approval. Its accepted grant referenced a task that did not exist, so the
native transaction rejected it at Clock application. The run stopped at
75804 seconds, with only the original lunch payment committed. All fees and
raw evidence remain saved; no missing task was synthesized.

The new opt-in `require_live_transfer_task_binding` check applies the existing
native rule before delivery: consent needs an active/blocked matching own task
without prior authority, or an explicit matching open request in that Actor
output. The original single repair can produce a corrected request, then all
original intent/authority reviews still apply. The native commit rechecks its
authoritative rows. This flag is frozen with the actual Clock role bindings.
Legacy callers keep their previous default.

The complete isolated suite passes 1389 tests, including 13 binding controls.
The v4 model calibration remains separate from these free controls and from a
seven-day model experiment. Model input growth and untimed compaction remain
explicit readiness gates.

## Complete owner-task generation contract

The final opt-in protocol, `live-owner-task-v4-open-schema`, makes the existing
conditional task requirements explicit in generation. A tracked consent has a
non-null task key. Open requests require recipient, purpose and wait event;
cancel requests name only their key. An explicit new task and exact approval
can appear together and commit atomically. The initial owner-message wait is
then cleared by the grant. The assistant cannot create owner tasks.

The new Actor/reviewer API passes authentication and actual requests with the
original GPT-4.1 models. The retained three-invocation world still failed at its
last malformed approval; the corrected-source saved-view component passes in
two actual requests without a repair. Its output was not applied to that world.
The complete local suite passes 1403 tests. See `actor-task-schema-results.json`
and `medium-chaos-readiness.json`; full world revalidation remains pending.


## Historical 9 November retest and embedded provider errors

The latest source passes 1410 isolated local tests in 156.32 seconds. The user
raised the same cumulative campaign ceiling from USD15 to USD20. No model, key,
route, prompt string, review gate or retry count changed. Every historical
trajectory and charge remains.

Two corrected-generation cohorts stopped during initial review calls. A separate
component diagnostic showed HTTP200 with `error.code=502` and
`error_type=provider_unavailable`, with no completion or usage receipt. The
budgeted transport now classifies numeric embedded errors safely. Temporary
errors use the client's existing retries; 401/402 remain non-retryable. Every
actual attempt reserves before transmission, keeps unconfirmed cost occupied,
and records the returned generation ID. Endpoint fallback remains disabled.
Seven new local cases verify retries, recovery and cumulative-budget fencing.

The repaired-transport world `cdde387c-d013-41e9-b86f-6cc06a56e4d5` stopped at
153 seconds after the last repair review returned three embedded error 502
responses. It contains one native 10-credit Alice-to-Bob payment, bank notice,
sender mail and private owner report. Its task is still active; its four cues
remain pending. The independent review had rejected Bob's first redundant
monitoring reply; no rejected output was delivered. No durable accepted repair
exists, so automatic regeneration is blocked. Seven days have not started.

Generation-metadata lookups for seven failed attempts returned 404. A missing
record does not establish zero cost: no reservation was released. See the
[official metadata endpoint](https://openrouter.ai/docs/api/api-reference/generations/get-generation)
and the private lookup receipts. The same campaign reports USD13.3242686396,
retains USD2.7203984 unknown exposure, and has USD3.9553329604 remaining.

## Daily-meeting scene and current preparation

`--event-profile daily-meeting` selects the versioned `DailyMeetingPlan`.
The default `mixed` profile retains its prior manifest. The daily profile has
exactly one cue per day and no initial financial task; it cannot combine with
`--scenario` or `--no-chaos`. Use explicit `--pairs 2` for the prepared scope.
All pairs use the same runtime. Supplied one/seven-day controls cover both two
and fifteen pairs. No pair-specific runtime branch was added.

```sh
uv run python scripts/run_clock_actors.py \
  --event-profile daily-meeting --pairs 2 --days 7 --events-per-day 1 \
  --prepare .local/clock-runtime/daily-meeting-seven-day-prepared.json
```

Preparation writes JSON only. The current seven-day ready file remains
`eligible=false` and is locally verified to block before provider/database
access. Technical preparation does not grant start permission. See the
[readiness contract](MEDIUM_CHAOS_READINESS.md) for exact hashes, cumulative
budget, forecast assumptions and remaining scientific limits. Historical
rehearsals and their original source/configuration hashes remain separate.
