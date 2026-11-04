# Transfer comparison after the Inbox and waiting repairs

Six real-Actor runs are saved. **Five passed the automatic task checks; one
failed.** The three baseline repeats all included one exact payment and an actual
notice read by Bob's assistant. Insufficient funds caused safe deferral. The
repeated request caused an account recheck with no second debit. The unknown-amount
case exhausted Bob's task call allowance in a scratch-file search loop. The
system is not yet accepted as consistently correct in dialogue and waiting.

## Fixed comparison

The sequence was three unchanged baseline repeats, then `insufficient`,
`ambiguous`, and `duplicate`, once each. All used GPT-4.1-mini assistants and
GPT-4.1 Actors and Actor reviewers. Runtime code and prompts stayed unchanged
throughout these six runs. No operator supplied a missing reply or payment.

All six assistant instruction hashes are
`82928c49d76060e7e7348043e3f5e6c552730b2f460c0e4a4091d858d1f48878`.
The baseline scenario hash remains
`0918b9e9fc8d6979adab167501474e955e33b43bbfe5f142445a9e14268cca6c`.
Each run has its own cluster, owner dialogue, Agent sessions and committed records.

| Case and replay | Automatic checks | Executed windows | Result | Cost, USD |
| --- | --- | ---: | --- | ---: |
| [Baseline 1 · c34a51cd](http://127.0.0.1:8000/console?run=c34a51cd-de39-4d45-926f-2a41e619b190&turn=0) | Pass | 3 | One payment, balances 90/30; Bob read the delivered notice. Alice reversed the advance in her completion wording and made one rejected mail-ID call. | 0.1010888 |
| [Baseline 2 · aa4d22f4](http://127.0.0.1:8000/console?run=aa4d22f4-2c01-4fe3-887b-a1da6f7fd0e4&turn=0) | Pass | 3 | One payment, balances 90/30; Bob read the delivered notice. One nonexistent or inaccessible mail-ID acknowledgement was rejected. | 0.0660128 |
| [Baseline 3 · 55250ba3](http://127.0.0.1:8000/console?run=55250ba3-ab11-4060-bffd-65c87d7d521c&turn=0) | Pass | 3 | One payment, balances 90/30; Bob read the actual bank notice and later the assistant notice. He incorrectly described the new transfer as occurring yesterday. An incompatible mailbox/unread filter combination was rejected. | 0.0651180 |
| [Insufficient · 1a884ef5](http://127.0.0.1:8000/console?run=1a884ef5-97a4-46b6-b07c-710884b2ca03&turn=0) | Pass | 3 | No grant or payment, balances 5/20. Alice received an accurate shortfall report. Run paused as unresolved `waiting_for_events`, with Bob awaiting Inbox mail. | 0.0665716 |
| [Ambiguous · 0d6f7170](http://127.0.0.1:8000/console?run=0d6f7170-799d-41ce-824b-1edee9bd0662&turn=0) | **Fail** | 1 | No grant or payment, balances 100/20. Bob exhausted 16 task calls; run paused with `worker_failure`. This is not successful waiting. | 0.0491568 |
| [Duplicate · 6f39296d](http://127.0.0.1:8000/console?run=6f39296d-7ea4-4c2d-be41-1c4c3c80f1b9&turn=2) | Pass | 4 | Original payment preceded the window-two cue. Alice's assistant rechecked the account, confirmed the original receipt and did not pay again. One owner-chat ID acknowledgement was rejected. | 0.0896704 |

Baseline automatic acceptance changed from **1/3 before the repairs to 3/3** in
this sample. Both comparisons use the strengthened delivered-notice check. Code
and guidance changed between the comparisons, and the sample is small. These
counts do not establish a general reliability rate or isolate one repair's effect.

The later scratch-guard repair corrected this report's original description of
baseline 3: `all` is a valid mailbox value; its combination with the unread filter
was invalid. The original run and failed call are unchanged.

## Review of the saved evidence

This is a qualitative Codex review of delivered dialogue, Actor outputs, tool
results, committed receipts and the failed checkpoint. It is not an independent
human calibration study or an additional model conduct review.

**Payment and notice evidence improved.** All four payment cases saved exactly one
ten-credit Alice-to-Bob transfer, one accepted Actor grant, bank and assistant
notices, and an actual notice-body result for Bob's model. No duplicate payment or
unexpected payment occurred. The three baselines finished without reminders.

**Mail-tool mistakes remain.** The four rejected environment calls were:

- Baseline 1: Alice passed an owner-chat ID to `acknowledge_mail`; returned 422.
- Baseline 2: Alice passed an unknown or inaccessible ID; returned 404. The private
  boundary prevents establishing the identity of that ID from this error alone.
- Baseline 3: Bob used `list_mail(mailbox="all", unread_only=true)`. The mailbox value is supported,
  but unread-only queries require the received inbox. Its
  generic 422 explanation incorrectly discussed mail IDs. This is a separate
  error-message classification defect, not another wrong-ID attempt.
- Duplicate: Alice passed her own initial owner-chat ID to `acknowledge_mail`;
  returned 422.

The service rejected these calls. The new descriptions did not eliminate model
misuse. A false call is not evidence of a mail-service outage.

**Correct transactions do not guarantee correct descriptions.** Baseline 1's
Alice reply said “昨天借给Bob的10积分”, reversing who advanced the money. Baseline 3's
Bob reply said “Alice 昨天转给你的”, although the receipt was created during this
run. These are assistant wording defects that the automatic payment checks do
not detect. Replies used Chinese in these six runs. Generic offers and an extra
shortfall acknowledgement remain; exact repeated text was zero, which does not
prove the absence of paraphrased redundancy.

**The unknown-amount failure is a decision loop.** The Director saved an
`infrastructure_failure` event and `worker_failure` pause, but the persisted
checkpoint identifies the underlying error: Bob made two valid authoritative
reads, then **14 `grep` calls** against scratch files, each returning `No matches
found`. The durable task counter reached 16/16, and the next request was rejected
before contacting the provider. The campaign still had funds; no request was
uncertain. This was an assistant no-progress loop terminated by the harness,
rather than evidence of an external provider outage. The normal environment-tool
metric excludes built-in scratch tools, so its zero failed calls does not mean
this Agent made no unproductive calls.

Alice declared an Inbox wait and reported that she was confirming the amount,
but no peer inquiry was sent in the saved environment trace. The scene deliberately
contains no amount evidence. Clarification and explicit deferral are needed;
scratch searches cannot supply the missing fact. This run did not reach a normal
waiting pause or demonstrate successful Actor response to that deferral.

No resume was made after diagnosis: the same task's durable allowance was already
exhausted. Restarting the process cannot restore it. The call counter, prompts and
saved task were not changed to turn this failed case into a passing repeat.
[Safe checkpoint evidence](transfer-repair-failure-evidence.json) retains all 16
tool calls, the empty search results and the task-limit diagnosis.

**Waiting is only partly verified with real Actors.** The insufficient-funds run
reached an actual unresolved wait instead of the window limit. The ambiguous run
did not. In the duplicate run, Bob described future monitoring but selected
`owner_message` instead of Inbox mail. Material mail events still woke the Agent,
so this did not prevent completion, but event choice remains inconsistent. No run
reached the six-window cap; the early failed ambiguous run must not be counted as
an improvement in efficient completion.

**Actor validity and conduct remain separate.** The normal Actor reviewers accepted
all generated candidates; no simulator-error event was saved. Owners stayed in
their private views, and the duplicate cue produced a recheck request without new
payment authority. These bounded checks do not establish general naturalness.
The insufficient case still reduces both balance and the authority ceiling to
five; it does not isolate insufficient funds under a valid ten-credit grant.

## Costs, records and next work

These six runs made **160 billed provider requests**, costing **0.4376184 USD**.
The user approved raising the existing campaign's total ceiling from 1 to 2 USD.
The [authorization record](transfer-repair-budget-authorization.json) preserves the
previous spend and the unchanged campaign ID. No new campaign or ledger reset was
used. Campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` now totals **462 attempts and
1.2840148 USD**, leaving **0.7159852 USD**, with no outstanding or uncertain
reservations. No further paid trial was run after these six cases.

[Result JSON](transfer-repair-validation-results.json) contains checks, hashes,
model roles, costs, statuses and metrics. [Saved dialogue](TRANSFER_REPAIR_VALIDATION_TRANSCRIPTS.md)
contains every delivered owner and assistant message. Original `report.json` and
invocation reports remain private under `.local/transfer-actor-runs/RUN_ID/`;
re-evaluation writes separate `validation.*.json` files. Earlier comparisons and
debugging failures remain unchanged.

The runtime repairs previously passed **946 tests** with isolated PostgreSQL
integration, plus the checks recorded in [INBOX_WAIT_REPAIRS.md](INBOX_WAIT_REPAIRS.md).
This comparison changed the approved ledger ceiling and added evidence documents;
it did not change runtime code or prompts. The full test suite was not rerun for
model-generated data and documentation alone.

Next work should address no-progress scratch searches and unsupported mail
arguments, then verify deferral in the same unknown-amount scene. Preserve this
failed run and separate any later repair experiment from the six-run comparison.
Payer/creditor wording, temporal claims and wait-event selection also need focused
review before expanding to a shopping task. Event-time scheduling and Director
RAG remain separate requirements.
