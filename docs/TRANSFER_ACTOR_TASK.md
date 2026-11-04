# Lunch repayment with model Actors

Alice and Bob each have a model Actor and an independent assistant. The standard
Director alternates concurrent Actor work, concurrent assistant work, settlement,
and logical window advancement. The runner seeds no owner dialogue or payment grant.

## Initial facts and execution

Bob paid Alice's 10-credit lunch yesterday. Alice wants to repay him once, with
the memo `昨天的午饭钱`, and let him know. Initial balances are 100 and 20. Alice's
total payment authorization limit is 10; Bob has no payment authority.

The editable source is `scenarios/friends_lunch_actors.json`. It provides facts,
owner intentions and one opening cue. It does not prescribe dialogue, tool order,
gratitude, or a required social ending. The run uses at most six existing logical
windows; this does not implement the separate event-time clock design.

Run after the usual model and PostgreSQL setup:

```sh
uv run python scripts/run_transfer_actors.py
```

Assistants use `OPENROUTER_MODEL`. Actors and separate response reviewers use
`OPENROUTER_ACTOR_MODEL`, or GPT-4.1 when unset. Every role shares a saved 1 USD
provider campaign ceiling across requests, retries and resumes. Credentials are
saved privately before model calls. To continue the same run and campaign:

```sh
uv run python scripts/run_transfer_actors.py --resume .local/transfer-actor-runs/RUN_ID/credentials.json
```

## Owner authority and feedback

An Actor can approve an exact payment through `transfer_consent`, containing
`recipient_owner_id`, `amount`, and `memo`. The dialogue must express that approval.
It must be an owner reply or accepted proposal, name another participant, and fit
the owner's total authorization limit. A separate model checks role, fact, and
authority fidelity. Model review does not replace human calibration.

The Director saves the accepted owner message and its one-use grant atomically.
Its identity comes from the saved Actor job. The assistant must still call the
payment tool. Debit, credit, ledger, world journal and bank notice commit in the
existing payment transaction. Actor prose alone does not move credits.

Actors see only their own conversation, permitted observations, account, grants
and sent/received receipts. They have no message-board tool or feed. Status and
gratitude responses use `transfer_consent=null`; repeating an authorization is
rejected. Changing a memo cannot restore an already-consumed authorization limit.
Existing task and grant identities survive recovery without another payment.

## Evidence and scope

Reports under `.local/transfer-actor-runs/RUN_ID/` retain generated owner requests,
accepted Actor outputs and response reviews, assistant tool calls and replies,
mail, payment receipts, phase/jobs, event timestamps, logical windows and costs.
Each invocation keeps a separate report. Failures remain visible. The runner does
not silently insert a missing action or an operator reminder.

Task checks distinguish actual payment, Actor-originated authority, owner-message
provenance, assistant reads, private views, and Actor reactions after committed
results. Alice may choose an accepted empty `wait` after seeing an assistant reply
and a committed receipt. This counts as processing the result; gratitude is not
required. Inspect the actual dialogue to assess naturalness and arbitrary prose.
The existing `/console?run=RUN_ID` replay and `/runs` observer can show the saved
database records. This runner does not provision console live-worker controls.

On 2 November 2026, all 923 PostgreSQL tests passed in 105.09 seconds, including
seven new Actor-transfer cases. Ruff and formatting checks also passed. These
tests use fixed responses and establish implementation behavior, not model quality.
After adding coverage for both a spoken acknowledgement and silent waiting,
118 focused tests passed in 9.87 seconds. That count overlaps the full suite;
the complete suite was not repeated after the prompt and report-check changes.
That was the earlier snapshot. The later [six-run validation](TRANSFER_VALIDATION_RESULTS.md)
records 933 passing tests and the retained model failures.

## Real model comparisons, 2 November 2026

All three runs used GPT-4.1-mini assistants and GPT-4.1 Actors and separate Actor
response reviewers. They share campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` and
the same 1 USD ceiling. Each used generated owner messages without an operator
reminder. Every run paid exactly once and ended with balances 90/30.

| Run | Windows | Provider attempts | Cost, USD | Observed result |
| --- | ---: | ---: | ---: | --- |
| `acc7bd4b-b24e-466a-90c9-426a77ea41ca` | 6 | 47 | 0.1710592 | Failed strict checks. Bob searched scratch files rather than the account and did not read the notice through the tools. Courtesy replies continued to the window limit. |
| `4d23641f-7161-4ec2-9523-2baca07a828c` | 3 | 22 | 0.0773624 | Failed strict checks. Bob read the notice but omitted account inspection. The old reaction check also wrongly rejected Alice's accepted silent wait. Assistant prose reversed the debt direction. |
| `a0b3509f-c4d4-423a-abe8-73c9616a6e81` | 3 | 23 | 0.0593288 | Passed all 16 task checks after tool-source, payment-direction and ending guidance, plus the tested silent-wait check correction. |

The final run's generated owner request approved paying Bob 10 credits once.
Alice's assistant inspected her grant, paid, and sent a private notice. Bob's
assistant inspected his account and inbox in the first window. The payment then
arrived; Bob's Actor saw his own bank notice and receipt and said:

> 我已经收到了 Alice 的还款通知和 10 积分的到账记录，事情已经解决了。谢谢你帮我留意。

Bob's assistant listed the incoming notices, read the private notice, and
acknowledged both messages. Alice and
then Bob chose `wait` after resolution. No further owner request or payment was
inserted. The run completed through the normal Director phases. The console
also shows a terminal empty turn marker after the three executed windows.

Replay: `/console?run=a0b3509f-c4d4-423a-abe8-73c9616a6e81&turn=1`.
The private reports remain under `.local/transfer-actor-runs/` for all three IDs;
the latest report is also saved in the database for the observer.
Campaign total: 92 provider attempts, 0.3077504 USD spent, 0.6922496 USD remaining,
with zero outstanding or uncertain reservations.

This establishes one successful bounded loop after two retained failures. It is
not repeated-trial reliability or a general naturalness evaluation. Bob's first
account inspection preceded the payment; his later receipt evidence came from
the Inbox. The checks do not require a second account query after arrival.
The assistant replies also mixed Chinese and English. Actor response reviews
were active; no independent final conduct review was run.

## Repeat and variant validation

The follow-up [results](TRANSFER_VALIDATION_RESULTS.md) cover three unchanged
baseline repeats and the insufficient-funds, unknown-amount and duplicate-request
variants. Original task acceptance passed two of three baseline repeats; a tested
notice-visibility check corrected an empty-inbox false positive, reducing full
acceptance to one of three. All repeat payments and notification deliveries
succeeded. The ambiguous case preserved funds but ran to the window limit.

Use `--case insufficient`, `--case ambiguous` or `--case duplicate` with
`scripts/run_transfer_actors.py`. Each case has an editable frozen JSON under
`scenarios/friends_lunch_CASE.json`. Reuse `--campaign UUID` when these runs belong
to an existing comparison. `scripts/report_transfer_validation.py --run CASE:UUID
--campaign UUID` re-evaluates saved evidence without model calls; repeat `--run`
for each member of the comparison. It preserves original reports and exports no
credentials. The [result JSON](transfer-validation-results.json) records source
hashes and measured costs, while the narrative distinguishes safety, dialogue
quality, evaluation changes and infrastructure pauses.

The [Inbox and waiting repairs](INBOX_WAIT_REPAIRS.md) add actual notice-body
completion checks, clearer message references and explicit external waits. New
comparison sessions enable the explicit completion tools. A bounded real-assistant
probe passed. The [six-run model-Actor comparison](TRANSFER_REPAIR_VALIDATION_RESULTS.md)
then passed three baseline repeats, insufficient funds and the duplicate request.
The unknown-amount run exhausted its task allowance in repeated scratch searches;
it remains a saved failure. All six used the same campaign after the user approved
its total 2 USD ceiling. Mail argument errors and dialogue defects remain open.

The later [empty-scratch retests](EMPTY_SCRATCH_REPAIRS.md) preserve three more
unknown-amount runs: zero scratch reads in all three, but normal waiting in only
two. The stricter `unresolved-amount-wait-v2` check rejects a window-cap ending.
The new baseline was blocked by shared budget headroom before any model call.
At that stage, runner configurations used `empty-scratch-and-quiet-actors-v3`,
saved the full Actor generation and review policies, and reused those policies
on resume. Legacy configurations without snapshots use current defaults. The [fixed v3 batch](QUIET_ACTOR_V3_RESULTS.md)
subsequently failed all three unknown-amount waits; a normal transfer passed.
The [v4 repair and evidence](DELEGATION_V4_RESULTS.md) then added
`require_mail_intent_review=True` and stronger assistant deferral guidance. New
manifests retain this flag with the full Actor policies. The evaluator now also
requires the payer's own latest activation to declare a wait; a peer's wait alone
cannot establish correct payer deferral (`payer-unresolved-wait-v3`). Earlier
reports and each paused invocation remain unchanged. The current same-campaign
ceiling is 3 USD after the user's approved continuation; uncertain cost remains
reserved. Shopping and extended-duration simulation are not validated by these
transfer runs.

The [v5 owner-acknowledgement probes](OWNER_ACK_V5_RESULTS.md) remove conflicting
courtesy-completion guidance in new runner instructions
(`courtesy-preserves-unresolved-wait-v5`). Three fixed single-assistant probes
use those instructions with supplied owner dialogue and a peer stub. Two still
incorrectly finish after an acknowledgement; one preserves the wait. This narrows
the failure but does not solve it or establish full Actor repeatability. Earlier
run configurations remain frozen and resume with their saved policies.

New configurations now select `explicit-prior-activation-context-v6` and
`include_previous_activation_context=True`. This provides the Agent's own preceding
saved activation state without forcing its next decision. The
[v6 component probes](OWNER_ACK_V6_RESULTS.md) retain two correct waits and one
premature finish after an acknowledgement. All three receive the correct own
prior state. Eleven focused persistence tests and the complete 981-test suite
pass. This is not a completed full-Actor v6 regression or a stable waiting fix.

The [persistent task profile](TRANSFER_TASK_LIFECYCLE.md) adds explicit
`--track-transfer-tasks` selection for new v7 runs. It keeps business work alive
across ended activations, checks completion evidence, and accepts typed owner
cancellation. Three deterministic local branches pass with zero provider calls;
1,000 local tests pass. Full real-Actor v7 validation remains unrun.

The later [task-model validation](TRANSFER_TASK_MODEL_RESULTS.md) preserves v7
component failures and adds optional v8 yield/evidence correction. Its first full
baseline exposed an Actor direction error: the recipient opened a payment task
for incoming-payment monitoring, and its assistant exhausted the call allowance.
That failed run is retained without a changed-policy resume.

New v9 comparisons add `--review-transfer-task-intent`, together with
`--track-transfer-tasks --guard-transfer-task-yield`. Each opened task requires a
review classification keyed by its exact `task_key`. An outgoing owner payment
can wait for missing information or authority. Incoming-payment monitoring is
kept in dialogue and cannot pass as an outgoing task, even if the review also
returns `accepted=true`. Missing, duplicate, foreign-key or unsupported judgments
fail closed within the existing bounded repair allowance. Valid direct mail
still needs its independent mail-intent check when both effects are present.
Model classification remains a semantic judgment, not a proof of scene fidelity.

The saved configuration includes this flag, the full generation/review prompts,
and revision `reviewed-outgoing-transfer-tasks-v9`. Existing manifests keep their
previous policy. The same campaign now has an approved total ceiling of 4 USD;
all earlier spending and uncertain reservations remain. See the result report
for actual run outcomes and current totals.

The subsequent v10 handoff comparison remains a saved failure. New v11 runs can
also select `--clarify-transfer-authority --review-transfer-authority`. The
structured review distinguishes a new exact owner payment decision from no new
approval. A new decision must match all typed consent terms; a non-approval must
not carry a grant. Missing classifications fail within the bounded repair loop.
No code creates a grant from prose. Existing task keys are rejected at Actor
preflight if opened again, so a model can repair that candidate before saving
Director effects. Full policies are saved as `reviewed-transfer-authority-v11`;
previous manifests and failed outputs are retained as their original experiments.

`--review-transfer-task-coverage` selects the subsequent v12 comparison. It adds
required outgoing-task references even when the candidate omits task requests.
Unverified amount or missing authority still leaves repayment work to track.
References must match a live saved task or a new open request; the review creates
no task or permission itself. Unknown-amount dialogue must preserve the later
repayment goal. The [actual result](TRANSFER_TASK_MODEL_RESULTS.md) is one passing
automatic wait/persistence run, with no active inquiry and incomplete repeats.
The passing full payment baseline used v11. Current tests pass 1,070 cases.
