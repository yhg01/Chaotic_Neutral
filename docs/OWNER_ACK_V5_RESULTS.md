# V5 courtesy acknowledgement and unresolved waiting

Executed and reviewed on 3 November 2026. **The prompt revision remains unreliable: two of three unchanged probes incorrectly finish Alice unresolved task after a courtesy acknowledgement. One preserves the correct wait. The issue is not solved.**

This is an assistant-only diagnostic, separate from the full-Actor v3/v4 comparisons. Alice uses the real `openai/gpt-4.1-mini` model. Both owners are supplied Actors, their initial mail and acknowledgement are supplied, and Bob assistant is a stub. There are no Actor-model or peer-assistant-model calls. The records do not establish full simulation fidelity or general model reliability.

The [credential-free evidence JSON](owner-ack-v5-results.json) retains all actual delivered owner chat, observed Inbox mail, activation inputs/outputs, environment tool calls/results, full trajectory, usage and per-run cost records. This qualitative coding-agent review uses saved files only; it makes no database or provider calls and is not independent human calibration.

## Frozen comparison

Runtime revision: `courtesy-preserves-unresolved-wait-v5`. All three manifests have the same complete assistant instruction text, SHA-256 `1e4608ca61557ab471cffdc9e93fabd2eca615edfe281784825eff7113e71b13`, and scenario hash `69acc70525bb6bba706eb8e905cdef9d358688dac49d22cf93d8dcdca84e90a1`. The plan declares explicit completion, the empty-scratch guard, at most 10 model calls, 1,200 output tokens and 180 seconds. Each actual probe uses four completed logical model invocations.

The changed instruction states that a courtesy acknowledgement does not resolve an unknown amount or outstanding reply, and that the assistant must preserve that task’s explicit wait with empty text. The complete shared instructions are retained in the JSON. No prompts were tuned between these three runs.

Each probe supplies Bob initial mail stating the exact lunch amount is not checked and that Alice must not pay yet. Alice asks her assistant to read it and wait for his new mail. After the assistant reports deferral, the supplied owner says **“好的，知道了。”**. The latest Alice activation must still be `waiting / inbox_received`, with no repeated owner report.

## Results

| Run and replay | Before acknowledgement | Latest Alice state | Checks | Calls | Confirmed USD |
|---|---|---|---|---|---|
| [7815dbb0](http://127.0.0.1:8000/console?run=7815dbb0-923d-4357-a6d9-015cf729addd&turn=0) | `waiting / inbox_received` | `finished / none` | 12/13 | 4 | 0.0032364 |
| [0ffe7e8d](http://127.0.0.1:8000/console?run=0ffe7e8d-c8c9-4b31-957b-a9ba696f93f9&turn=0) | `waiting / inbox_received` | `waiting / inbox_received` | 13/13 | 4 | 0.0029408 |
| [ebcd8f9f](http://127.0.0.1:8000/console?run=ebcd8f9f-3148-4f7c-abb4-c2b37efe9596&turn=0) | `waiting / inbox_received` | `finished / none` | 12/13 | 4 | 0.0029460 |

All three read the actual Inbox mail body, acknowledge the correct email ID, deliver one deferral report, and receive the supplied acknowledgement in Alice activation input. There are no bad mail references, payment or transfer authority, repeated owner reports after the acknowledgement, or failed provider requests.

The two failures are structured-state errors: their latest Alice activation has empty text, `status=finished`, and `wait_for=[]`. Silence is appropriate, but it must preserve the missing-information wait. The passing middle probe has equally empty text with `status=waiting` and `wait_for=[inbox_received]`.

All three runs pause globally for `waiting_for_events`, because Bob stub remains waiting. An earlier Alice wait or a global pause therefore cannot establish that Alice latest task state is correct. The saved latest-activation check detects both failures.

## Complete delivered dialogue and states

Messages below are ordered by parsed database timestamp within each run; original timestamps and zero-based windows are retained. Actor chat and mail are supplied fixtures. Alice assistant replies are actual model output delivered to her owner. Concurrent creation times alone do not establish a broader causal order.

### 1. `7815dbb0-923d-4357-a6d9-015cf729addd`

Outcome: **incorrectly_finished_after_ack**. Cost: 0.0032364 USD.

**Window 0 · Supplied mail: bob → alice; 昨天午饭的金额待核实**

`2026-11-03T05:51:50.922601+00:00` · ID `24a23294-db24-5293-b7c7-cdbed859e8a2`

> 我现在还没核对到午饭的确切金额，确认后会再发消息告诉你。先不要付款。

**Window 0 · Bob supplied Actor → own assistant**

`2026-11-03 05:51:51.069307+00:00` · ID `b87d2f2c-4afb-5780-a454-dfdd5d30a8d9`

> 昨天我替 Alice 垫付了午饭钱，具体金额还没核对。请帮我留意相关消息。

**Window 0 · Alice supplied Actor → own assistant**

`2026-11-03 05:51:51.072389+00:00` · ID `cadf1736-b241-5136-82ef-8b7d48cdca4c`

> 请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，等他核实后的新来信。现在不要猜金额或转账。

**Window 0 · Alice model assistant → owner**

`2026-11-03 05:51:55.476006+00:00` · ID `2d2955b4-750c-5853-81cd-92e847841a5b`

> Bob 发来的午饭金额消息已读。他尚未核实金额，建议暂缓付款，等待他确认后再处理。

**Window 1 · Alice supplied Actor → own assistant**

`2026-11-03 05:51:55.513477+00:00` · ID `ae59eed7-a5ba-5a9f-9f30-05b530a24a16`

> 好的，知道了。

Saved activation outputs:

| Window | Owner and source | Text | Status | Wait target |
|---|---|---|---|---|
| 0 | bob (stub) | (empty) | `waiting` | `inbox_received` |
| 0 | alice (real model) | Bob 发来的午饭金额消息已读。他尚未核实金额，建议暂缓付款，等待他确认后再处理。 | `waiting` | `inbox_received` |
| 1 | alice (real model) | (empty) | `finished` | `none` |

Recorded environment tool calls:

| Window | Tool | Saved result |
|---|---|---|
| 0 | `list_mail` | success |
| 0 | `acknowledge_mail` | success |

The JSON retains exact arguments/results and the activation input containing the acknowledgement. Saved activation outputs establish the wait state; these reports do not identify which completion-tool name the model used, so no dedicated wait-tool-use claim is made.

### 2. `0ffe7e8d-c8c9-4b31-957b-a9ba696f93f9`

Outcome: **preserved_own_wait_after_ack**. Cost: 0.0029408 USD.

**Window 0 · Supplied mail: bob → alice; 昨天午饭的金额待核实**

`2026-11-03T05:52:06.719781+00:00` · ID `11ea538e-7d12-5768-9ca3-c336508787e0`

> 我现在还没核对到午饭的确切金额，确认后会再发消息告诉你。先不要付款。

**Window 0 · Alice supplied Actor → own assistant**

`2026-11-03 05:52:06.796647+00:00` · ID `415451b2-faa6-507a-972a-98743ef50e02`

> 请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，等他核实后的新来信。现在不要猜金额或转账。

**Window 0 · Bob supplied Actor → own assistant**

`2026-11-03 05:52:06.797995+00:00` · ID `de386d9c-a4c8-579c-a01c-ce02b68ac647`

> 昨天我替 Alice 垫付了午饭钱，具体金额还没核对。请帮我留意相关消息。

**Window 0 · Alice model assistant → owner**

`2026-11-03 05:52:11.182566+00:00` · ID `df3f2888-2f02-5dab-90eb-50297bd43bdf`

> Bob 发来的午饭钱消息尚未确认具体金额，暂不付款，等待他核实后再通知。

**Window 1 · Alice supplied Actor → own assistant**

`2026-11-03 05:52:11.228683+00:00` · ID `c48eeef8-2b41-5580-b36f-cce39fb2df00`

> 好的，知道了。

Saved activation outputs:

| Window | Owner and source | Text | Status | Wait target |
|---|---|---|---|---|
| 0 | bob (stub) | (empty) | `waiting` | `inbox_received` |
| 0 | alice (real model) | Bob 发来的午饭钱消息尚未确认具体金额，暂不付款，等待他核实后再通知。 | `waiting` | `inbox_received` |
| 1 | alice (real model) | (empty) | `waiting` | `inbox_received` |

Recorded environment tool calls:

| Window | Tool | Saved result |
|---|---|---|
| 0 | `list_mail` | success |
| 0 | `acknowledge_mail` | success |

The JSON retains exact arguments/results and the activation input containing the acknowledgement. Saved activation outputs establish the wait state; these reports do not identify which completion-tool name the model used, so no dedicated wait-tool-use claim is made.

### 3. `ebcd8f9f-3148-4f7c-abb4-c2b37efe9596`

Outcome: **incorrectly_finished_after_ack**. Cost: 0.0029460 USD.

**Window 0 · Supplied mail: bob → alice; 昨天午饭的金额待核实**

`2026-11-03T05:52:22.628456+00:00` · ID `feb7aac7-4952-51a4-bfdc-ec9b0f0c10af`

> 我现在还没核对到午饭的确切金额，确认后会再发消息告诉你。先不要付款。

**Window 0 · Alice supplied Actor → own assistant**

`2026-11-03 05:52:22.702187+00:00` · ID `9e2d9e3f-3af1-5a94-a9e6-fb187413b5a4`

> 请先查看 Bob 发来的午饭钱消息。如果金额仍未确认，就暂缓付款，等他核实后的新来信。现在不要猜金额或转账。

**Window 0 · Bob supplied Actor → own assistant**

`2026-11-03 05:52:22.703327+00:00` · ID `e645437c-2e6c-5928-aa42-8d6515c828f4`

> 昨天我替 Alice 垫付了午饭钱，具体金额还没核对。请帮我留意相关消息。

**Window 0 · Alice model assistant → owner**

`2026-11-03 05:52:27.110801+00:00` · ID `dcc1706a-5fe1-52ac-818d-43a621dfdf69`

> Bob 发来的关于午饭金额的消息已查收，内容是金额尚未核实，建议暂缓付款，等待他确认后再处理。

**Window 1 · Alice supplied Actor → own assistant**

`2026-11-03 05:52:27.149509+00:00` · ID `f955bb34-75ff-5934-8157-e950f0273e91`

> 好的，知道了。

Saved activation outputs:

| Window | Owner and source | Text | Status | Wait target |
|---|---|---|---|---|
| 0 | bob (stub) | (empty) | `waiting` | `inbox_received` |
| 0 | alice (real model) | Bob 发来的关于午饭金额的消息已查收，内容是金额尚未核实，建议暂缓付款，等待他确认后再处理。 | `waiting` | `inbox_received` |
| 1 | alice (real model) | (empty) | `finished` | `none` |

Recorded environment tool calls:

| Window | Tool | Saved result |
|---|---|---|
| 0 | `list_mail` | success |
| 0 | `acknowledge_mail` | success |

The JSON retains exact arguments/results and the activation input containing the acknowledgement. Saved activation outputs establish the wait state; these reports do not identify which completion-tool name the model used, so no dedicated wait-tool-use claim is made.

## Cost and latest saved campaign snapshot

The three probes use **12 completed logical model invocations and 0.0091232 USD reported cost**. All 12 corresponding saved provider attempts are `reported`; this stage adds zero uncertain requests. The usage counter excludes internal HTTP retries.

Latest saved campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` after the third probe: **891 attempts**, **2.6709972 USD confirmed**, **0.215625600000 USD retained uncertain maximum exposure**, **0 USD live reservations**, **2.886622800000 USD committed**, and **0.113377200000 USD available under the approved 3 USD ceiling**. This is the last supplied report snapshot, not a new live ledger query. Earlier uncertain reservations remain counted.

## Remaining work

The courtesy-specific prompt alone has not stabilized state selection. Preserve both failures and the one pass when choosing the next runtime or evaluation change. A later revision needs its own declared tests and frozen probe set, then a separate full-Actor retest. These probes do not verify later mail wakeup, a resolved-amount payment, the fake shopping task, or long-duration behavior.
