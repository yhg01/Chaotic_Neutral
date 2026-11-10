# Timed transfers and committed receipts

Date: 9 November 2026. Profile: `clock-transfer-v1`. Credits in this document
belong to the simulated world. No real bank or payment provider is connected.

The existing `transfer_credits` tool now supports an opt-in elapsed-time path.
It first queues a pending operation. At the declared Clock completion, the
existing transfer service commits the debit, credit, native receipt, bank notice
and Clock result in one PostgreSQL transaction. Recovery retains the original
authorization and operation; it cannot pay that authorization twice.

This reuses the existing transfer ledger and exact owner-grant checks. It does
not implement a second balance store or fabricate a receipt from dialogue.
Legacy runs retain their original immediate-transfer behavior.

## Setup and authority

Migration 020 adds a profile, operations and call-alias records. A new pending
Director run must first bind to the elapsed Clock. Before its first job,
`TimedTransfers.enable` opts it in. The Clock contract must already contain a
positive `transfer_credits` duration. Enabling later or changing the frozen
profile limits is rejected.

Only an Actor job in the opted-in profile can return the typed
`TimedActorOutput.transfer_consent`. Its owner message and exact grant commit
when that saved Actor generation completes. The default `TaskOutput` and Agent
outputs reject this field. The grant refers to the actual owner-message row and
the original Actor job. There is no Agent HTTP route to grant authority.

The grant validator checks the bound run, role, owner, saved output, original
job-derived authorization ID, delivered generation event and current simulated
second. Permission is not inferred from prose, silence or another person's mail.
The unchanged business service checks the exact recipient, amount, memo and
single-use authorization again when the payment becomes due.

## API and Agent tools

| Operation | Contract |
| --- | --- |
| `POST /api/v1/runs/{run_id}/transfers` | Identity still comes from the Agent credential. An opted-in run queues the existing exact transfer arguments and returns `status=pending`, an operation ID and due second. It does not debit immediately. |
| Same POST after settlement | Returns the actual terminal payment/rejection result. It does not create another pending wait or another payment. |
| `GET /api/v1/runs/{run_id}/transfer-operations/{operation_id}` | Returns the sender's own operation: pending, applied or rejected. An applied result includes the actual native receipt. A peer or foreign run cannot read this operation. |
| Existing transfer account and Inbox reads | Expose the authenticated owner's actual account, incoming/sent transfers and permitted bank notices. A recipient receives its own bank notice and incoming receipt, not the sender's private balance or grants. |

`EnvironmentTools(..., timed_transfers=True)` exposes the dedicated transfer
tool subset, including `read_transfer_operation`. The flag is opt-in, survives
the runtime's private adapter copy, and binds to the session instruction hash.
Default tool definitions and legacy session hashes remain unchanged.

The Agent runtime treats a pending transfer as unresolved work and yields the
activation. It does not substitute the pending admission for a completed
payment. A later activation reads the committed receipt. Local tests use the
actual Deep Agents graph, scoped HTTP routes and PostgreSQL checkpoints with
supplied model responses. Completed-activation replay makes no extra model call;
a later receipt-reading activation uses the real operation ID. This verifies
the component path, not a live-model Actor–Agent autonomy experiment.

The lower-level scheduling service preserves its original admission on retry.
The HTTP tool route additionally reads current operation state, so an already
settled request returns a final result. New and repeated call IDs for the same
grant map to the same operation. Reusing a call ID with changed content conflicts.
Deduplication applies to that authorization; a new explicit owner authorization
is a separate instruction, not a retry of the original one.

## Atomic completion

Before the due second, there is no debit, incoming credit, accepted transfer
receipt or success bank notice. When the tool-completion event is reached, the
Director holds the short Clock/run locks and commits all of the following:

1. Clock advancement and completion delivery.
2. Debit and credit in the original `world_accounts` records.
3. The original `world_requests` and `credit_transfers` receipt records.
4. The native bank notice in `inbox_messages` and the world journal.
5. The operation result and foreign-key link to that actual payment.
6. Receipt notifications addressed to the payer/recipient Agent lanes and a
   bank-mail notification for the recipient, all at that simulated second.

No LLM or external service call occurs inside this transaction. The environment
run remains paused for legacy mutations; it is not temporarily switched to
running. Private completion-context checks allow only the matching timed grant,
payment and exact bank notice in this Director transaction. Existing Agent APIs
cannot supply those private context arguments or bypass the due-time check.

The database keeps its real `created_at` timestamps. The receipt separately
stores `simulated_seconds` and its Clock operation ID. Wall time is not rewritten
to simulate elapsed days.

## Failure outcomes

| Condition | Result |
| --- | --- |
| Queue/call receipt storage fails | Operation insertion and lane busy state roll back together. Retry retains the original call identity. |
| Debit succeeds inside the transaction, but bank notice or final operation storage fails | Clock, balances, native receipt, bank notice, notifications and operation status all roll back. The original completion remains pending. |
| Process is killed before commit | Ordinary reads see the original balances and zero payments. Recovery executes the original step once. |
| Process is killed after commit but before its result is received | Ordinary reads already see the complete payment and notice. Recovery returns the saved step and original native receipt; it does not debit again. |
| Multiple callers retry the same grant | One scheduled effect and one native payment. Call aliases retain original payload hashes. |
| Insufficient balance at completion | Terminal rejected operation; no payment or success notice. A different call ID cannot retry the same grant as new work. |
| Paused Clock or completion at/after the exclusive horizon | No early payment. Pending work remains visible; cutoff is not reported as task success. |

Unexpected infrastructure failures propagate and preserve a retryable pending
event. Safe business rejection is recorded separately. Existing one-use-grant
database constraints remain in force. These guarantees cover the local
PostgreSQL simulated-credit effect; they are not claims about external banking.

## Local evidence

Run with a separate PostgreSQL test database in `TEST_DATABASE_URL`:

```sh
uv run python scripts/validate_clock_transfers.py
```

The script creates and removes only its owned random schema. It terminates only
test children it starts. Raw synthetic trajectories are exported before removal
to ignored `.local/clock-transfer/traces/` files. Test run IDs are not development
console URLs. The user database and historical campaign are not migrated/reset.

The control uses a supplied owner generation taking one simulated second and a
frozen seven-second transfer duration. Payment is therefore due at second 8.
These durations are declared test parameters, not model or bank benchmarks.

| Independent world | Before recovery | Final database evidence |
| --- | --- | --- |
| Kill before commit | Alice 100 / Bob 20, zero payments/notices | Alice 90 / Bob 30, one payment and one bank notice at second 8 |
| Kill after commit | Alice 90 / Bob 30, one payment/notice | Same balances, same single native receipt at second 8 |
| Insufficient balance | Alice 5 / Bob 20 | Rejected at second 8; zero payments/notices |
| Horizon at second 5 | Transfer due at second 8 | Pending at cutoff; original balances and zero payments/notices |

Both successful cases use separate worlds. They are not two payments of one
debt in one world. Each also replays the completion eight times and resubmits
the same and new call IDs without creating another payment. World-journal replay
matches the actual persisted world. A fifteen-owner ring test uses the same
Actor-authority and payment paths for all peers and conserves every balance.

The first focused run retains two failed checks: native account UUID/timestamp
objects could not yet be frozen into a JSON activation input. The repair uses
the same JSON encoding as the HTTP representation. It changes no payment,
authority or Clock ordering rule. All initial diagnostics remain preserved.

See [control results](clock-transfer-control-results.json),
[verification results](clock-transfer-validation-results.json) and the
[readable recovery trace](CLOCK_TRANSFER_RECOVERY_TRACE.md).
The final complete regression passes **1,333 tests**, including 20 new timed
transfer cases, with zero failures, errors or skips (119.97 seconds reported by
pytest). The first focused run retains 55 passes and two serialization failures.

## Cost and remaining gates

This round uses supplied responses and makes zero provider requests. The same
cumulative campaign stays capped at USD 10. All earlier confirmed charges and
uncertain exposure remain occupied. Actor, review and personal API configuration
files remain unchanged.
The final same-campaign check records USD 5.3745078972 reported cost,
USD 0.4079752 retained uncertain exposure, zero live reservations and
USD 4.2175169028 remaining under the USD 10 ceiling. The user database remains
at migration 017; migrations 018–020 have only been applied in owned test schemas.

The elapsed transfer tool is locally verified. A live-model Director/graph
driver, full owner task/report/assistant-mail closure, timed shopping and other
world tools, elapsed recurrence, the intended setting/shared-object contract and
medium-chaos calibration remain separate acceptance gates. No seven-day
medium-chaos model experiment is claimed by these controls.
