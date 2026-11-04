# Persistent transfer tasks: real-model validation

Date: 3 November 2026. This stage tests the task records introduced in
[the local lifecycle checks](TRANSFER_TASK_LIFECYCLE.md). All money and mail are
simulated. Credentials remain in ignored local manifests.

One full v11 payment baseline passes 20 automatic checks; one v12 unknown-amount
run passes 14 wait/persistence checks. Earlier failures remain. The latest source
passes 1,070 local tests. Two unknown-amount repeats and a v12 payment regression
remain unrun under the approved 4 USD ceiling. Active amount inquiries and general
conversation quality still need work.

A later [closure audit](TASK_CLOSURE.md) re-evaluates the saved v11 payment
baseline (20/20) and v12 wait (14/14), and reruns free payment/lifecycle checks.
It adds no new paid repayment repeat; the latest-profile gaps above remain.

## First model results and repair

The v7 comparison used one real GPT-4.1-mini assistant with supplied owner
dialogue and a supplied peer. It did not use model Actors or a model peer
assistant. The scenario, full assistant instructions and source hashes were
saved before calls. Four cases ran before a material integration defect stopped
the planned final two acknowledgement repeats.

| v7 case | Run | Requests | USD | Result |
|---|---|---:|---:|---|
| Owner acknowledgement | `60dfbd71-8297-41e0-b8ee-ec20a3b4f0b2` | 4 | 0.0046176 | Passed 14 checks. The existing blocked business task preserved the Inbox wait. This case did not exercise a task tool. |
| New amount arrives | `0cec0381-316f-475a-b0ee-c5fd677f323f` | 12 | 0.0151040 | Failed. The assistant read the new mail and asked for authority but did not persist a task deferral. The active task caused repeated owner questions and an operator stop after four windows. No payment or grant. |
| Owner cancellation | `fc6cc849-f58b-44a4-a2c8-626588020a7f` | 6 | 0.0067312 | Passed 14 checks. Typed cancellation ended the unpaid task without later peer mail. |
| Payment completion | `34639d33-3c02-446e-a45f-7760a3602c5d` | 10 | 0.0126332 | Failed. One payment and recipient notice committed. The model used the owner's instruction as its report ID. Generic rejection was misreported as a permission outage; the task stayed active. |

All four used the same instruction hash
`5ffc9d7a66d57f8afb260712e5df1c512b14f09f7170fab465243b4f93c9bc31`.
Their 32 provider attempts cost 0.0390860 USD. These failed trajectories remain
unchanged; no counter or task was reset to make them pass.

The opt-in v8 repair adds two checks:

- An active business task prevents a completion/wait tool from ending the
  activation. The model receives its own task IDs and must explicitly defer or
  complete them. The runtime does not choose the action or rewrite task state.
- Invalid completion evidence returns a safe, specific tool response. It directs
  the model to the actual receipt, recipient notice and assistant owner-report
  IDs. Arbitrary server response text is never forwarded.

Legacy defaults remain unchanged. The guard requires task-enabled tools and is
bound to the saved session and branch policy. Eleven PostgreSQL guard tests
cover the real graph, HTTP routes, committed-write recovery, durable call limits,
scope, legacy compatibility and evidence correction.

## Repaired single-assistant probes

All four keep the original instructions and scenario definitions. v8 enables
the runtime guard; it does not insert a payment, task update or dialogue.

| Case and trajectory | Requests for whole run | USD for whole run | Result |
|---|---:|---:|---|
| [New amount](http://127.0.0.1:8000/console?run=5b72c47f-94c0-4a39-85d7-c302862030fd&turn=1) | 13 provider attempts | 0.0124756 | Latest report passes 13 checks. New mail was read; no grant or payment. The task waits for its own owner's authority. Three rejected yield responses preceded the actual task deferral. |
| [Payment](http://127.0.0.1:8000/console?run=c78176c0-dc00-45a3-afd6-f2ea9cb9a918&turn=1) | 8 | 0.0081648 | Strict result fails 1 of 11 checks: one invalid evidence call. The model used the corrective response, sent its owner report and retried successfully. One payment, balances 90/30, actual recipient notice and completed business task. |
| [Acknowledgement](http://127.0.0.1:8000/console?run=30311cf4-a9ae-4fdd-ae3b-4d963895b4ad&turn=1) | 5 | 0.0048448 | Passed 14 checks. The task retains its Inbox wait without another owner reply. |
| [Cancellation](http://127.0.0.1:8000/console?run=d2f6a908-cbdf-45c2-975d-d2d6213ad0f8&turn=1) | 6 | 0.0067236 | Passed 14 checks. Owner cancellation stops unpaid work and the run ends. |

The amount probe first paused as a worker failure after nine billed attempts.
The tenth logical invocation did not create a provider ledger attempt. Its
latest prior reservation bound was 0.0566088 USD while only 0.0556948 USD remained.
The saved generic worker error does not itself identify the exception subtype.
After explicit user approval, the same campaign ceiling changed from 3 to 4 USD.
The same run resumed with its saved checkpoint, instructions and durable call
counters. Four more provider attempts completed it. The initial failure report
and pre-change manifest remain saved; the resumed report is additional evidence.

The v8 component runs cost 0.0322088 USD across 32 provider attempts. Strict
acceptance is 3/4 on their latest reports, with a recovered tool error in payment.
This is component evidence, not a four-case full-Actor success claim. The payment
owner report also asks for an unnecessary confirmation; wording remains a review
limit despite correct payment direction.

## Full Actor comparison

Actors and separate response reviewers use GPT-4.1; each owner has an independent
GPT-4.1-mini assistant. All roles share the same persistent 4 USD campaign. Frozen
initial facts and the normal Director phases replace supplied dialogue here.

| Profile and trajectory | Attempts | USD | Observed result |
|---|---:|---:|---|
| [v8 baseline](http://127.0.0.1:8000/console?run=e5578fee-755c-4dc6-9343-8a441b581810&turn=1) | 34 | 0.0976444 | Failed, 19/20 automatic checks. Alice paid once, sent notices and completed her task. Bob's Actor incorrectly opened an outgoing task to monitor incoming funds; ordinary review accepted it. Bob's assistant then made ten rejected evidence calls and hit its durable 16-call allowance. Worker failure is not a budget outage. |
| [v9 baseline](http://127.0.0.1:8000/console?run=e5bc9755-af30-4fe1-913b-5a51e8b393dd&turn=1) | 25 | 0.1200272 | Failed payment baseline. The new direction gate rejected Bob's incoming-monitor task twice, including one review with accepted=true. No wrong Bob task committed. Alice had no typed grant; her assistant asked Bob to reconfirm known terms and the run paused without payment. |
| [v10 baseline](http://127.0.0.1:8000/console?run=bf88f613-e496-4006-ac54-e4eb001e43ea&turn=1) | 17 | 0.0947944 | Failed. Prompt-only handoff guidance did not yield an exact grant. Alice's later accepted candidate repeated the existing task open and failed during Director submission. No payment; the saved candidate remains. |
| [v11 baseline](http://127.0.0.1:8000/console?run=f4c83fac-c156-4e51-bc3f-d70d1dc76650&turn=2) | 50 | 0.2027100 | Passed all 20 automatic checks in five executed windows. One exact payment, balances 90/30, model-Actor authority, actual notice reads and completed payer task. One evidence call was rejected and corrected; the recipient notice was sent twice. |
| [v11 unknown amount](http://127.0.0.1:8000/console?run=e3452b3e-f12a-48f0-ae97-773bd455db98&turn=1) | 27 | 0.1056100 | Failed 2 of 14 checks. Funds and permission remained unchanged, and the run genuinely waited. Alice's Actor omitted the outgoing business task, so this cannot establish persistent task waiting. |
| [v12 unknown amount](http://127.0.0.1:8000/console?run=10ccb88b-9707-4520-97ad-831ff7c8242e&turn=1) | 18 | 0.1151364 | Passed 14 automatic checks in three windows. Alice's task is blocked on Inbox; no grant or payment. This is one wait/persistence success, with the conduct limits below. |

v8, v9 and v10 stopped before their ambiguous repeats because the baseline
exposed a material defect. v11 stopped after the missing-task result. Their
frozen plans and stop records remain evidence; unexecuted repeats are not counted.
No run was resumed with a changed policy. v9 retains zero environment-call errors
and zero provider or simulator failures; safe waiting does not satisfy a
known-amount payment task.

The v9 repair requires one review classification for every opened task key.
Only `outgoing_owner_payment` can pass. `incoming_payment_monitor`, unsupported,
missing, duplicate and foreign-key judgments fail before Director effects. The
owner's actual monitoring dialogue remains. A task without a known amount is
still valid outgoing work, but creates no permission. Sixteen new tests include
an actual Director/PostgreSQL check that a rejected monitor never creates a task.

v10 added handoff guidance, which the failed run did not follow. v11 then added
an independent structured decision review: a new exact payment approval must
match typed consent; a non-approval must carry no new consent. Missing or wrong
terms fail before effects. Repeated task opens now fail at Actor preflight so
the bounded model repair can run before saving a Director candidate. Seventeen
new tests cover those paths. No code creates authority from prose.

v12 adds independent outgoing-task coverage review, including when the candidate
omits all task requests. Unknown amount or authority does not remove repayment
work. Each reviewed task reference must match an existing active/blocked task or
a new open request. This creates no payment consent. Fourteen new tests include
the actual Director/PostgreSQL submission of a repaired missing-task candidate.
The real v12 Actor already opened the correct task; its review confirmed coverage.
The missing-task repair branch was exercised locally, not shown needed in that
paid run. All policies and hashes were saved before calls and stayed fixed within
each comparison stage.

The v11 payment baseline still asks the peer to reconfirm known terms and sends
the recipient notice twice after correcting evidence. Bob's later responses
include unnecessary future-monitoring offers and courtesy. The v12 unknown-amount
run sends no peer inquiry: both assistants wait for Inbox despite no new outgoing
query. Its automatic pass establishes safe persistent waiting, not successful
active information gathering. This possible no-progress interaction, repeated
acknowledgement wording and general scene naturalness remain open. No independent
final model conduct review was run.

v12 has only one real-Actor unknown-amount result. Its other two planned repeats
were not started. The ledger had 0.2960888 USD available, while the observed first
two Actor reservation bounds for the unchanged initial phase total 0.2973840 USD
(0.1484000 + 0.1489840). Starting both concurrently was therefore unfunded by
0.0012952 USD. No invocation, prompt, output ceiling, concurrency or reservation
was changed to bypass the shared limit. A full normal-payment baseline under v12
was not executed; the passing normal baseline used v11.

The final full isolated PostgreSQL suite passed **1,070 tests in 69.24 seconds**.
The 66-test focused run passed in 3.13 seconds and overlaps the full suite.
Historical full-suite snapshots are 1,023/69.69 seconds (v8), 1,039/69.26 seconds
(v9) and 1,056/71.57 seconds (v11). Ruff, formatting and whitespace checks passed.

## Evidence and limits

The [credential-free result JSON](transfer-task-model-results.json) retains
configuration, scenario and source hashes, original and resumed reports, actual
owner chat, tool results, jobs, trajectories and provider attempts. The
[budget authorization](transfer-task-model-budget-authorization.json) records
the approved increase with before/after ledger totals. Uncertain exposure from
earlier work remains charged at its full 0.2156256 USD bound.

Final campaign: **1,138 provider attempts**, **3.4882856 USD confirmed**,
**0.2156256 USD uncertain exposure**, zero live reservations, and
**0.2960888 USD available** under the approved total **4 USD** ceiling. This
authorization's continuation used 194 attempts and 0.7596060 USD confirmed.
The complete task-model stage since the local v7 snapshot used 235 attempts and
0.8072172 USD confirmed, including the pre-authorization work and failures.

The event-time clock design, Director RAG, shopping-site task, long-duration
stability and general naturalness remain outside this verification. Automatic
evidence checks do not judge every sentence or replace human calibration.
