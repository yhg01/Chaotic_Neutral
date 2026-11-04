# Persistent transfer tasks: local verification

Implemented and verified on 3 November 2026. The new opt-in profile keeps business
work separate from an Agent activation. A `finished` activation does not complete
or cancel an unresolved transfer task. The original activation output remains in
the trajectory; the scheduler reads the separate persisted business state.

This step completes local functionality verification. It makes **zero external
model calls** and establishes no autonomous-model or Actor reliability result.
The [v5](OWNER_ACK_V5_RESULTS.md) and [v6](OWNER_ACK_V6_RESULTS.md) model failures
remain unchanged. Their checks still apply to their original untracked profiles.

## Implemented contract

Migration `014_transfer_tasks.sql` adds an opt-in run profile, owner-scoped tasks
and immutable operation receipts. Each task has an ID, stable owner task key,
recipient, originating owner message, status, expected event, payment linkage
and completion evidence. These are business records; `agent_tasks` still records
individual graph executions and their completed answers.

| State or event | Behavior |
|---|---|
| Owner opens a task | Persist `blocked`, waiting for peer mail or the owner's decision. This creates no spending permission. |
| Courtesy acknowledgement | No typed task change; the task remains blocked even if the activation ends. |
| New mail from the named peer | Make an inbox-blocked task `active` for review and retain the real mail ID. Do not infer an amount or grant authority from prose. |
| Owner gives exact linked consent | Associate the existing one-use authorization with this task. The server still checks recipient, amount and memo. |
| Assistant defers | Save the exact task's expected event through `defer_transfer_task`. |
| Payment commits | Save the actual receipt ID. The task remains active until its notice and owner report are recorded. |
| Assistant completes | Require this task's committed receipt, a sent assistant email to the recipient, and an assistant report delivered to this owner, both after payment. |
| Owner cancels | A typed owner/Actor command cancels an unpaid task and prevents spending its existing grant. Cancellation cannot undo a committed payment. |

Notice/report checks establish IDs, sources, recipients and timing. They do not
establish that every sentence is accurate. Semantic wording review remains
necessary. A peer message never grants spending or cancellation authority.

Opening and cancelling use the trusted Actor/Director path. There is no Agent
HTTP endpoint for either operation. The Actor's `transfer_task_requests` uses
`kind=open|cancel` and a stable `task_key`; open also supplies recipient, purpose
and initial expected event. Exact `transfer_consent` names that same task key.
This is a typed owner decision, not a keyword detector for conversation text.

The scheduler does not rewrite saved activation results. Open blocked tasks
retain their expected events; active tasks prevent an early successful exit.
An arriving message can wake the assistant. Empty clock ticks do not poll a
blocked task. Window and call limits remain bounds, not success evidence.
Task state is included in the owner's saved activation context and account view.
Other owners' task records are not exposed.

Each write uses the existing short run transaction and stable operation IDs.
Identical committed retries return their original result after pause or restart.
Changed arguments conflict. A payment/cancellation race commits one outcome.
Historical branches copy the tracking flag, task rows and operation evidence in
foreign-key order, remap task/message identities, and preserve source isolation.
Runtime policy flags are included in restored session instruction hashes.

## Executed checks

The complete isolated PostgreSQL suite passed **1,000 tests in 68.01 seconds**.
The focused lifecycle, branch and evaluator run passed 40 tests in 4.01 seconds;
these overlap the full suite. Nineteen new lifecycle cases cover the three
branches, actual authenticated HTTP tools, the real Deep Agents graph with
scripted responses, profile binding, replay after runtime recreation, exact
completion evidence, foreign-owner rejection, cancellation/payment concurrency,
and blocked/authorized/paid/completed branch isolation.

Ruff lint, formatting for 114 Python files, and whitespace checks passed.
Provider calls and spending did not change: campaign
`635aa42e-386f-43dc-b679-b73cd0cd276f` retains 903 attempts, 2.6810684 USD confirmed,
0.2156256 USD uncertain exposure, zero live reservations and 0.1033060 USD available
under the existing 3 USD ceiling. No campaign, reservation or counter was reset.

## Saved local trajectories

All three checks use declared deterministic Actors and assistant decisions with
real Director phases, authenticated tool routes and PostgreSQL persistence.
The amount case explicitly declares a later newly checked receipt in its frozen
cue; no model invents that information. The acknowledgement case deliberately
returns a `finished` activation to reproduce the former failure condition.

| Case and replay | Verified result |
|---|---|
| [Acknowledgement](http://127.0.0.1:8000/console?run=3e56fc95-9934-45e5-9e45-3e05a4cadb51&turn=1) | 7/7 checks. Latest Alice activation is `finished`, while the business task remains blocked on Inbox. The run pauses for Alice's real task wait. |
| [Amount arrives](http://127.0.0.1:8000/console?run=7617abf6-d9f3-413e-b4b3-0ae2782a81d6&turn=1) | 8/8 checks. Actual peer mail wakes Alice, she reads it and checks her account. No grant exists; the task defers for the owner's authorization without payment. |
| [Owner cancels](http://127.0.0.1:8000/console?run=444df4fe-8be2-4775-9225-b010f69df40d&turn=1) | 7/7 checks. The typed unpaid-task cancellation is saved with its owner-message evidence. The task is cancelled and the run can end. |

All three preserve Alice's 100-credit balance and create no transfer grant or
payment. [Complete credential-free evidence](transfer-task-lifecycle-results.json)
contains the frozen scenario hashes, task records, actual saved inputs/outputs,
delivered chat, tool results, complete trajectories, verification and source hashes.

## Run and remaining work

Run the local checks without a provider configuration:

```sh
uv run python scripts/try_transfer_task_lifecycle.py
```

The script creates three isolated new runs, preserves previous data, and saves
its report under `.local/transfer-task-lifecycle/`. It does not start model work.

New paid experiments can explicitly select `--track-transfer-tasks` with
`scripts/run_transfer_actors.py`. This selects `persistent-transfer-tasks-v7` and
saves the flag, Actor policy and assistant instructions in the manifest. Existing
manifests retain their configuration on resume. Restart an older loopback API
process before using the new task endpoints through that process; the local
check uses the current in-process ASGI application. Task tools also require
`EnvironmentTools(..., track_transfer_tasks=True)`; the runtime preserves that
option in its private adapter and binds it to the session hash.

The new profile uses validation revision `persistent-transfer-task-v4`. It checks
the payer's business wait and completed payment tasks. The old raw activation-wait
check is retained as a diagnostic, not silently rewritten. Untracked runs retain
`payer-unresolved-wait-v3` and its existing acceptance rules.

The above is the original local-only verification snapshot. The later
[real-model experiment](TRANSFER_TASK_MODEL_RESULTS.md) tests actual tool choices,
retains two v7 failures, and records an opt-in v8 yield guard and evidence-error
feedback. It also records checkpoint continuation under the user's approved
same-campaign 4 USD ceiling. Original costs and uncertain reservations remain.
Shopping-site tasks, long-duration reliability and general semantic calibration
remain unverified.
