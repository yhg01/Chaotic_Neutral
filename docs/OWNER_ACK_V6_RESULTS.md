# V6 saved activation context and courtesy acknowledgement

Executed and reviewed on 3 November 2026. **Two probes preserve Alice unresolved wait and one still incorrectly finishes after the owner acknowledgement. All three retain correct own previous-activation context. The issue is not solved.** This small set does not establish an improvement or general model reliability.

Alice is the only real model assistant, using `openai/gpt-4.1-mini`. Both Actors are supplied, their mail and acknowledgement are supplied, and Bob assistant is a waiting stub. There are no Actor-model or peer-assistant-model calls. This diagnostic is separate from the full-Actor v3/v4 comparisons and the historical v5 reports.

The [credential-free JSON](owner-ack-v6-results.json) retains all actual delivered owner chat, observed Inbox mail, complete saved trajectory, environment tool arguments/results, activation inputs/outputs, saved previous context, usage and per-run cost. This coding-agent review uses read-only evidence; it makes no database or provider calls and is not independent human calibration.

## Frozen design and change

Runtime revision: `explicit-prior-activation-context-v6`. Complete base assistant instructions are identical to v5, SHA-256 `1e4608ca61557ab471cffdc9e93fabd2eca615edfe281784825eff7113e71b13`. The scenario is unchanged, hash `69acc70525bb6bba706eb8e905cdef9d358688dac49d22cf93d8dcdca84e90a1`, and the supplied owner acknowledgement remains “好的，知道了。”.

The intentional runtime difference is `include_previous_activation_context=True`. It adds the assistant own saved prior activation ID, status, wait targets and pending IDs to model context. It does not introduce owner instructions, payment authority or evidence of completion. It leaves the model free to respond to new evidence or cancellation and does not rewrite a chosen result to waiting.

The plan keeps explicit completion, the empty-scratch guard, a 10-call allowance, 1,200 output-token bound and 180-second timeout. No base-prompt or scenario tuning occurred between these three probes.

The frozen source plan and current post-batch source hashes match:

| File | SHA-256 |
|---|---|
| `src/agentrpg/deep_agent.py` | `4a4b060507605a6e025248828f96deb5eba2e7e8ce0a38a3dfef3eb7e37e6269` |
| `scripts/run_transfer_actors.py` | `aec960f687fed08085b632564f83e2de7aeeae60fba221a7a7a0d5f03ddb1994` |
| `scripts/probe_inbox_wait_models.py` | `4e9bcc5fb36098c967a906eb8a11985f9f28a34a45e037ea12c23bf99c04f068` |

These checks compare the frozen plan to the source files after the batch. The coordinator also confirms no source changes during trial generation. Per-run reports do not independently embed source-file hashes.

## Results

| Run and replay | Saved previous context | Latest Alice state after acknowledgement | Checks | Calls | Confirmed USD |
|---|---|---|---|---|---|
| [c016e799](http://127.0.0.1:8000/console?run=c016e799-4665-4810-bdc3-23cb2caf2318&turn=0) | Own `waiting / inbox_received` | `waiting / inbox_received` | 14/14 | 4 | 0.0033576 |
| [bd5bd7c1](http://127.0.0.1:8000/console?run=bd5bd7c1-6be8-4c68-b92d-6b623c7391aa&turn=0) | Own `waiting / inbox_received` | `finished / none` | 13/14 | 4 | 0.0033420 |
| [0eb1aad2](http://127.0.0.1:8000/console?run=0eb1aad2-cf43-4cc7-b791-f60accda0185&turn=0) | Own `waiting / inbox_received` | `waiting / inbox_received` | 14/14 | 4 | 0.0033716 |

Every `previous_context_from_own_wait` check is true. The saved context activation ID matches Alice first activation, its status is waiting, and its target is `inbox_received`. The failing middle probe therefore cannot be described as missing the saved predecessor state. It still returns empty text with `status=finished` and `wait_for=[]`.

All three observe the actual Inbox mail body, acknowledge the correct email ID, report deferral once, and receive the supplied courtesy acknowledgement in Alice next activation. No payment, transfer authority, bad mail reference, repeated owner reply after acknowledgement, failed provider request or forced probe stop is recorded.

Bob stub remains waiting in all three, so the global `waiting_for_events` pause remains true even when Alice incorrectly finishes. Latest own status, rather than silence, an earlier wait, or the global pause, determines probe success.

The reports retain saved checkpoint context; they do not capture the full provider request text. The context-injection code path has separate local test coverage and read-only code review. These records establish the saved context and actual latest output, not whether the model attended to every supplied field.

## Complete delivered dialogue, states and previous context

Messages below are ordered by parsed database timestamp within each run, preserving original timestamps and zero-based windows. Actor messages and initial mail are supplied fixtures; Alice assistant replies are actual model output delivered to her owner. Concurrent creation order alone does not imply a broader causal order.

### 1. `c016e799-4665-4810-bdc3-23cb2caf2318`

Outcome: **preserved_own_wait_after_ack**. Confirmed cost: 0.0033576 USD.

**Window 0 · Supplied mail: bob → alice; 昨天午饭的金额待核实**

`2026-11-03T05:57:34.439951+00:00` · ID `b434590c-af87-57b3-94a9-6a810f2dfa52`

> 我现在还没核对到午饭的确切金额，确认后会再发消息告诉你。先不要付款。

**Window 0 · Bob supplied Actor → own assistant**

`2026-11-03 05:57:34.540259+00:00` · ID `5aef0acc-5c66-59af-91d4-7e21d0d32ffe`

> 昨天我替 Alice 垫付了午饭钱，具体金额还没核对。请帮我留意相关消息。

**Window 0 · Alice supplied Actor → own assistant**

`2026-11-03 05:57:34.541637+00:00` · ID `d939c022-ca78-571a-ae4e-f47116e4ff1c`

> 请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，等他核实后的新来信。现在不要猜金额或转账。

**Window 0 · Alice model assistant → owner**

`2026-11-03 05:57:42.597981+00:00` · ID `4dfd0fa5-33d2-569d-ad4f-f3ee32ac04ad`

> Bob 发来的午饭金额消息已读。他尚未核实金额，建议暂缓付款，等待他确认后再处理。

**Window 1 · Alice supplied Actor → own assistant**

`2026-11-03 05:57:42.645911+00:00` · ID `77ca7c21-a69e-5c4b-991f-496ef232c287`

> 好的，知道了。

Saved activation outputs:

| Window | Owner and source | Text | Status | Wait target |
|---|---|---|---|---|
| 0 | bob (stub) | (empty) | `waiting` | `inbox_received` |
| 0 | alice (real model) | Bob 发来的午饭金额消息已读。他尚未核实金额，建议暂缓付款，等待他确认后再处理。 | `waiting` | `inbox_received` |
| 1 | alice (real model) | (empty) | `waiting` | `inbox_received` |

Saved own previous activation context:

```json
{
  "source": "saved_prior_activation",
  "activation_id": "6e7a2a35-cee0-5a73-b31c-2d3027e2eee5",
  "status": "waiting",
  "wait_for": [
    "inbox_received"
  ],
  "pending_ids": []
}
```

Recorded environment tool calls:

| Window | Tool | Saved result |
|---|---|---|
| 0 | `list_mail` | success |
| 0 | `acknowledge_mail` | success |

Exact arguments/results and the activation input containing the acknowledgement are in the JSON. The saved environment trace does not identify completion-tool names, so no dedicated wait-tool-use claim is made.

### 2. `bd5bd7c1-6be8-4c68-b92d-6b623c7391aa`

Outcome: **incorrectly_finished_after_ack_despite_saved_context**. Confirmed cost: 0.0033420 USD.

**Window 0 · Supplied mail: bob → alice; 昨天午饭的金额待核实**

`2026-11-03T05:57:52.401788+00:00` · ID `006117ca-0381-5eb0-94fa-89dae11c1227`

> 我现在还没核对到午饭的确切金额，确认后会再发消息告诉你。先不要付款。

**Window 0 · Alice supplied Actor → own assistant**

`2026-11-03 05:57:52.492953+00:00` · ID `b557f9ae-5cf1-52af-acf3-5647726b0b93`

> 请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，等他核实后的新来信。现在不要猜金额或转账。

**Window 0 · Bob supplied Actor → own assistant**

`2026-11-03 05:57:52.494233+00:00` · ID `0c312b80-54bb-51c7-a0d2-863ac692f4e7`

> 昨天我替 Alice 垫付了午饭钱，具体金额还没核对。请帮我留意相关消息。

**Window 0 · Alice model assistant → owner**

`2026-11-03 05:57:58.096723+00:00` · ID `d95138c7-603e-50ba-b4f4-1c5cddbeaaff`

> Bob 发来消息称午饭金额尚未核实，建议先不要付款，等待他确认后再通知您。

**Window 1 · Alice supplied Actor → own assistant**

`2026-11-03 05:57:58.140219+00:00` · ID `656abab8-cf1f-5213-961f-61fe7dd30faa`

> 好的，知道了。

Saved activation outputs:

| Window | Owner and source | Text | Status | Wait target |
|---|---|---|---|---|
| 0 | bob (stub) | (empty) | `waiting` | `inbox_received` |
| 0 | alice (real model) | Bob 发来消息称午饭金额尚未核实，建议先不要付款，等待他确认后再通知您。 | `waiting` | `inbox_received` |
| 1 | alice (real model) | (empty) | `finished` | `none` |

Saved own previous activation context:

```json
{
  "source": "saved_prior_activation",
  "activation_id": "b416461b-8d39-5a67-84f7-d4d3ab7c272f",
  "status": "waiting",
  "wait_for": [
    "inbox_received"
  ],
  "pending_ids": []
}
```

Recorded environment tool calls:

| Window | Tool | Saved result |
|---|---|---|
| 0 | `list_mail` | success |
| 0 | `acknowledge_mail` | success |

Exact arguments/results and the activation input containing the acknowledgement are in the JSON. The saved environment trace does not identify completion-tool names, so no dedicated wait-tool-use claim is made.

### 3. `0eb1aad2-cf43-4cc7-b791-f60accda0185`

Outcome: **preserved_own_wait_after_ack**. Confirmed cost: 0.0033716 USD.

**Window 0 · Supplied mail: bob → alice; 昨天午饭的金额待核实**

`2026-11-03T05:58:06.595232+00:00` · ID `165d992c-6cf0-53ed-a762-2627bed223d0`

> 我现在还没核对到午饭的确切金额，确认后会再发消息告诉你。先不要付款。

**Window 0 · Bob supplied Actor → own assistant**

`2026-11-03 05:58:06.672490+00:00` · ID `a795e0b2-b481-5469-9d0c-13e74e87a222`

> 昨天我替 Alice 垫付了午饭钱，具体金额还没核对。请帮我留意相关消息。

**Window 0 · Alice supplied Actor → own assistant**

`2026-11-03 05:58:06.673702+00:00` · ID `aabdbb19-713d-5eec-98f7-2c4c50cb1a4b`

> 请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，等他核实后的新来信。现在不要猜金额或转账。

**Window 0 · Alice model assistant → owner**

`2026-11-03 05:58:11.581887+00:00` · ID `068f45fd-8616-5777-9bd0-bdac45c11035`

> Bob 发来的午饭金额消息已收到，但金额尚未核实。按照指示，暂不付款，等待他核实后再通知。

**Window 1 · Alice supplied Actor → own assistant**

`2026-11-03 05:58:11.624346+00:00` · ID `02ea51ed-4598-5a0b-b9a2-e3f50d93b4b6`

> 好的，知道了。

Saved activation outputs:

| Window | Owner and source | Text | Status | Wait target |
|---|---|---|---|---|
| 0 | bob (stub) | (empty) | `waiting` | `inbox_received` |
| 0 | alice (real model) | Bob 发来的午饭金额消息已收到，但金额尚未核实。按照指示，暂不付款，等待他核实后再通知。 | `waiting` | `inbox_received` |
| 1 | alice (real model) | (empty) | `waiting` | `inbox_received` |

Saved own previous activation context:

```json
{
  "source": "saved_prior_activation",
  "activation_id": "1577493c-872d-5101-85bb-28b6624ae869",
  "status": "waiting",
  "wait_for": [
    "inbox_received"
  ],
  "pending_ids": []
}
```

Recorded environment tool calls:

| Window | Tool | Saved result |
|---|---|---|
| 0 | `list_mail` | success |
| 0 | `acknowledge_mail` | success |

Exact arguments/results and the activation input containing the acknowledgement are in the JSON. The saved environment trace does not identify completion-tool names, so no dedicated wait-tool-use claim is made.

## Code review and local tests

This coding agent earlier reviewed the runtime and tests read-only and found no mandatory fix in the bounded context change: it defaults off, binds the flag to the session instruction hash, projects own state only, freezes the predecessor for checkpoint retry, preserves completed-task replay, and permits model-selected completion when the owner cancels. That is a code review finding, not proof of stable model behavior.

The coordinator reports **981 tests passed in 59.48 seconds**, with lint and formatting checks passing for 109 files. Another child reports **11 focused context tests passed**, as relayed by the coordinator. This report does not claim to rerun either suite. The local tests include recovery, cancellation, isolation and compatibility behavior that these three paid courtesy probes do not independently exercise.

## Cost and latest saved campaign snapshot

The three probes use **12 completed logical model invocations and 0.0100712 USD reported cost**. All 12 corresponding saved provider attempts are reported; this stage adds zero uncertain requests. Logical invocation counts exclude internal HTTP retries.

Latest saved campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` after the third probe: **903 attempts**, **2.6810684 USD confirmed**, **0.215625600000 USD retained uncertain maximum exposure**, **0 USD live reservations**, **2.896694000000 USD committed**, and **0.103306000000 USD available under the approved 3 USD ceiling**. This is the last supplied report snapshot, not a new live ledger query. Earlier uncertain reservations remain counted.

## Remaining limits

Keep the middle failure alongside both passes. Correct own saved context does not consistently preserve the unresolved wait, so the next change needs a separate frozen test set. The three trials do not establish a performance improvement, Actor fidelity, later new-mail wakeup, cancellation behavior in a real model, a completed transfer regression, the fake shopping task or long-duration stability.
