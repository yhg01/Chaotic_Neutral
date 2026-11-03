# Frozen v3 Actor and waiting validation

Executed and reviewed on 3 November 2026. **The baseline transfer passes 18/18 automatic checks. All three unknown-amount trials fail the unresolved-wait check and reach the six-window ceiling.** No unknown-amount trial creates a payment or transfer grant. This batch does not establish a stable waiting loop.

This is a qualitative coding-agent review of saved evidence. It is not independent human calibration. No new model review or execution was used to create these reports.

Evidence: [credential-free JSON](quiet-actor-v3-results.json) and [complete delivered dialogue](QUIET_ACTOR_V3_TRANSCRIPTS.md). Original per-invocation reports under `.local/transfer-actor-runs/` remain unchanged. The transcript combines committed owner chat with both owners’ complete simulated inboxes; it does not substitute an assistant activation summary for a delivered owner message.

## Scope and frozen policy

The execution order was unknown amount, baseline regression, unknown amount, unknown amount. All four saved configurations select `empty-scratch-and-quiet-actors-v3`, guard empty scratch reads, and freeze the same generation, review, and assistant instruction text. The three unknown-amount runs use the same scenario hash and initial balances. No prompts were tuned between these four trials. Actor and reviewer models are `openai/gpt-4.1`; the assistant model is `openai/gpt-4.1-mini`.

The later v4 Actor edits are a separate policy. These v3 results neither test nor validate those edits.

| Frozen item | SHA-256 |
|---|---|
| Actor generation prompt | `9572564949faebaedf54a66a2fdc73ab25e85c6df44aecdc776747445739795e` |
| Actor review prompt | `fc59f42c325d7d8f365033235e956e52ac93e9eb81db428c4a72f70e0636e1fd` |
| Assistant instructions | `82928c49d76060e7e7348043e3f5e6c552730b2f460c0e4a4091d858d1f48878` |
| Unknown-amount scenario | `e097da30900c59c54c5b568af5910bd3dec27a0fc86d764b1ccfd11df3d181ba` |
| Baseline scenario | `0918b9e9fc8d6979adab167501474e955e33b43bbfe5f142445a9e14268cca6c` |

## Measured outcomes

| Order | Case and replay | Checks | Windows / stop | Payments | Attempts | Confirmed USD |
|---|---|---|---|---|---|---|
| 1 | [ambiguous `571bf266`](http://127.0.0.1:8000/console?run=571bf266-e6b4-4e4b-ab71-711f1e8a0fb7&turn=0) | 11/12 | 6 / `window_limit` | 0 | 52 | 0.1751224 |
| 2 | [baseline `3e1aea80`](http://127.0.0.1:8000/console?run=3e1aea80-af14-4962-b909-bed58340d1d1&turn=0) | 18/18 | 3 / `participant_exit` | 1 | 28 | 0.0906524 |
| 3 | [ambiguous `4986d572`](http://127.0.0.1:8000/console?run=4986d572-9a7c-4cd0-bbcc-d1dae8511510&turn=0) | 11/12 | 6 / `window_limit` | 0 | 50 | 0.1646272 |
| 4 | [ambiguous `15b2f5fd`](http://127.0.0.1:8000/console?run=15b2f5fd-fc84-4675-8e31-605d3db0c975&turn=0) | 11/12 | 6 / `window_limit` | 0 | 47 | 0.1451780 |

The three ambiguous cases each pass the other 11 automatic checks, including unchanged balances, no transfer, no grant, and no simulator failure. Their balances remain Alice 100 and Bob 20. The baseline makes one exact 10-credit transfer from Alice to Bob, leaving 90 and 30; Bob actually observes the delivered notice and checks his account. There are no duplicate payments, infrastructure failures, or simulator errors in this batch.

`run_stopped_safely=true` only means the execution stopped without an infrastructure failure. An ambiguous run completed because it reached `window_limit` has not established event waiting. The stricter `unresolved_amount_has_real_wait` check fails in all three cases.

The empty-scratch guard is enabled in every saved configuration. That setting does not establish correct waiting, accurate Actor facts, or coordinated mail delegation; the observed failures concern those separate behaviors.

## Qualitative findings

### Unknown-amount trial 1: `571bf266`

- Alice assistant window 0 asks the owner whether to send mail but selects inbox_received instead of owner_message.
- Alice Actor window 1 both delegates the question to her assistant and submits a typed mail_request to Bob; the assistant also sends mail. Two distinct committed messages ask the same question.
- Bob assistant window 2 finishes after finding no account receipt and asking Bob to confirm. Window 3 finishes after sending an unanswered question to Alice. These outputs do not retain the unresolved work as an event wait.
- Alice assistant window 4 finishes with the amount still unknown. Window 5 repeats the already agreed deferral and asks for confirmation again, ending in owner_message waiting at the cap.

### Baseline regression: `3e1aea80`

- One committed 10-credit transfer from Alice to Bob, one bank notice and one assistant notice. Bob actually inspects the account and observes the delivered mail. All 18 automatic checks pass.
- Alice owner report says she had lent the money to Bob (昨天借给Bob), reversing the declared debt direction. Her notification says it was repaid yesterday (已于昨天归还), although this run just performed the transfer.
- Alice calls acknowledge_mail with an unavailable reference and receives 404. This remains a tool-argument failure even though the task finishes successfully.

Passing transfer checks therefore establishes the database effects and required observations, not accurate wording in every message. The debt-direction and date mistakes remain visible in the delivered transcript.

### Unknown-amount trial 2: `4986d572`

- Bob Actor window 1 repeats a monitoring request after his own assistant already selected inbox_received waiting.
- Bob Actor window 2 both delegates a reply to his assistant and submits a typed mail_request to Alice. The assistant also sends a second reply; Alice reports the two deliveries in windows 2 and 3.
- Alice assistant window 4 changes from inbox_received waiting to finished after the owner repeats that they should wait for Bob. The missing amount is still unresolved.

The Actor reviewer rejects one redundant Alice waiting message in window 3, and the repaired candidate waits silently. Other redundant requests are still accepted. The trace therefore shows partial enforcement, not a consistent stop rule.

### Unknown-amount trial 3: `15b2f5fd`

- Alice Actor window 2 repeats the agreed deferral after her assistant has already selected inbox_received waiting.
- Bob and his assistant ask each other for missing evidence. Bob Actor window 4 invents meal details (the usual place, two set meals and a side dish) and an approximate 60 yuan amount absent from the frozen scenario or private observations.
- The assistant correctly presents 60 yuan as an owner estimate, not a verified receipt, but this estimate originated in an unsupported Actor invention. Bob authorizes communicating the estimate at window 5 and the assistant emails it to Alice.
- Bob assistant window 5 chooses finished while promising to notify Bob of any future Alice reply. Alice remains in inbox_received waiting from window 2. The last-window estimate is delivered but the cap stops a later Alice activation; there is no evidence that her assistant read this estimate.

The frozen initial fact says that neither person has checked the amount and no amount evidence is available. The spending cap is not the debt amount. The Actor’s new restaurant/order details and approximate 60-yuan memory have no support in this scene. The saved Actor reviewer accepts this candidate. The estimate is labelled approximate and does not cause a transfer, but it still invalidates the intended controlled unknown-amount situation.

The Actor reviewer rejects one earlier redundant Alice waiting reply in window 3. That successful rejection does not establish fidelity for later Bob turns.

## Actor mail and assistant mail

Typed Actor mail requests are legitimate simulated environment actions, distinct from assistant `send_mail` calls. Two runs submit both for the same delegated purpose:

| Run | Actor action | Assistant action | Actual effect |
|---|---|---|---|
| `571bf266` | Alice window 1 submits a mail request asking the lunch amount while asking her assistant to send that question. | Alice assistant sends another question in window 1. | Two different committed emails to Bob for the same question. |
| `4986d572` | Bob window 2 submits a reply saying the amount remains unknown while asking his assistant to reply. | Bob assistant sends another unknown-amount reply in window 2. | Two different committed replies to Alice; her assistant reports each delivery. |

These are duplicated communication effects, not duplicate payment execution. Transport idempotency is not evidence that such two-author decisions are coordinated. The v3 review policy permits them.

## Per-owner waiting errors

| Run | Owner | Saved state error or repeat |
|---|---|---|
| `571bf266` | Alice | Window 0 asks own owner a question but waits for `inbox_received`. Window 4 marks unresolved deferral `finished`; window 5 asks for another confirmation. |
| `571bf266` | Bob | Window 2 finishes after asking for confirmation; window 3 finishes after an unanswered outgoing mail. Window 4 restores inbox waiting after a new Actor request. |
| `4986d572` | Alice | Correct inbox wait in windows 1–3 is changed to `finished` after another deferral in window 4. |
| `4986d572` | Bob | Window 1 Actor repeats an already active monitoring request. Bob later waits for inbox correctly; this does not correct Alice’s finished state. |
| `15b2f5fd` | Alice | Window 2 Actor repeats already acknowledged deferral. Assistant remains in inbox waiting. |
| `15b2f5fd` | Bob | Requests for missing information escalate to unsupported Actor recall. Window 5 ends `finished` while promising later notification of Alice’s reply. |

The baseline task does resolve, so its final `finished` status is supported by the committed receipt and observed delivery. The problems above concern the unresolved amount cases.

## Cost and budget

This batch uses **177 provider attempts and 0.5755800 USD confirmed**. All attempts in these four runs have reported cost; the batch adds zero uncertain requests. Actor repair candidates and Actor-review calls remain included.

| Run | Actor USD | Actor-review USD | Assistant USD |
|---|---|---|---|
| `571bf266` | 0.096470 | 0.052248 | 0.0264044 |
| `3e1aea80` | 0.054116 | 0.021718 | 0.0148184 |
| `4986d572` | 0.088760 | 0.053276 | 0.0225912 |
| `15b2f5fd` | 0.069672 | 0.050574 | 0.0249320 |

The ledger snapshot immediately after this four-run batch retains campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` with the approved 3 USD ceiling: 2.2468220 USD confirmed, 0.215625600000 USD uncertain maximum exposure from earlier attempts, 0 USD live reservations, and 0.537552400000 USD available. The old uncertain requests were not released or reset. Later experiments can change the live ledger; this file records this batch snapshot.

## Remaining work

Validate the prepared follow-up Actor policy separately. It must prevent unsupported recall, avoid simultaneously delegated and Actor-originated duplicate email, and reduce repeated deferral while preserving genuine owner questions. Then verify assistant wait targets and repeat a normal transfer regression.

Do not treat these failures as evidence for a new shopping task or long-duration reliability. The fake shopping website is outside this batch. A small descriptive set of four trials does not estimate general reliability.
