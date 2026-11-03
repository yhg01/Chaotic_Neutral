# Inbox evidence and waiting repairs

Implementation and local verification are complete. A bounded real-assistant
debugging probe passed after the interface repairs. The subsequent
[six-run real-Actor comparison](TRANSFER_REPAIR_VALIDATION_RESULTS.md) is now saved:
all three baselines passed automatic task checks, as did insufficient funds and
the duplicate request. The unknown-amount case failed at a durable task call limit.
Incorrect mail arguments, wording and event selection remain open.

## Implemented behavior

**Mailbox references.** Tool schemas distinguish Inbox email IDs, private owner-chat
IDs, notification event IDs and board IDs. Owner chat needs no mail receipt. When
a caller supplies its own chat ID to a read or acknowledgement endpoint, the
service returns 422 with a reference explanation. Foreign private IDs still return
404 without disclosure. Mixed acknowledgement batches fail before any receipt is
written. Tool errors identify invalid references rather than calling them outages.

**Actual notice evidence.** The Agent graph records notified mail IDs. An explicit
`finished` result is rejected until successful `list_mail` or `read_mail` results
contain those email bodies. An empty query, chat text, a notification alone or an
acknowledgement does not establish that evidence. Unobserved IDs survive both
checkpoint recovery and later activations. The runtime asks the model to correct
its read or explicitly defer; it does not insert a mail read or fabricate a result.
This completion check does not judge arbitrary owner-facing prose.

**Explicit waits.** `ActivationResult.wait_for` names an expected event. The Director
does not dispatch another model call for an empty clock tick while such a wait is
active. New substantive events still interrupt it. If all work is finished or
explicitly waiting, with no undelivered events, pending settlement, queued prompts
or reachable future beat, the run pauses with `result.reason=waiting_for_events`.
It remains unresolved and resumable, rather than being declared successful.
Future declared cues still run; operator pauses are preserved. This is a logical
window mechanism, not the separate proposed wall-clock/event-time scheduler.

For a paused run, resume before injecting new simulated mail through the existing
mail service, or queue an operator cue and resume. This repair does not add a
background listener or broaden the permissions of writes while paused.

**Clear completion tools.** New transfer comparison sessions opt into
`require_explicit_finish=True`. This requests provider tool selection and rejects
plain final responses that omit the activation result. It adds two simple tools:

| Tool | Result |
| --- | --- |
| `wait_for_mail(text=...)` | `waiting`, with `wait_for=["inbox_received"]`; another person's email. |
| `wait_for_owner(text=...)` | `waiting`, with `wait_for=["owner_message"]`; the paired owner's private-chat reply. |

Each can deliver a brief first deferral or question. Once the owner knows the
deferral, empty text avoids another acknowledgement. The model chooses whether
and how to wait. The mode is bound to the Agent session. The default remains the
legacy plain-response behavior, and saved canonical activation prompts retain
their original format.

**Actor context.** Actors now receive their own assistant's declared wait event.
Generation and response-review instructions reject repeated monitoring or waiting
acknowledgements when nothing changed. Genuine questions, new evidence, changed
needs, feelings and explicit Director cues can still require a reply. These prompt
changes have local contract coverage. The subsequent comparison includes real
Actors; it does not establish general naturalness or correct waiting in every case.

## Verification and retained debugging attempts

The full suite passed **946 tests in 106.07 seconds**, with real PostgreSQL
integration in an isolated test database. Checks cover reference privacy, atomic
batch rejection, actual body evidence, an unread notification carried into a later
activation, checkpoint replay without another model call, explicit completion
mode, event-driven resumption and future cues without empty-tick polling. Ruff,
formatting for 132 files and whitespace checks passed. The loopback API was
restarted to load the runtime repairs; prior trajectories remain saved.

All six small debugging probes used **one GPT-4.1-mini assistant**, supplied
Actors and a supplied peer assistant. They are not full model-Actor runs or six
unchanged repetitions. Contracts and guidance changed between probes. Every probe
read the actual mail without a bad mail-reference call or payment.

| Probe | Retained issue or result |
| --- | --- |
| `5a9a6370` | Correct silent wait, but no first report to the owner. Its original narrow checks passed; qualitative re-evaluation failed it. |
| `62cd399f` | Added a report, but used an ordinary reply and later emitted redundant idle status messages. |
| `89ae4706` | Explicit completion was required, but `responded` still failed to declare the outstanding wait. |
| `fdd09126` | Declared waiting, but selected the paired-owner chat event for a peer's email. |
| `b0286f71` | Repeated the same event-category mistake after further description changes. |
| `e3138220` | Passed the strengthened checks after the dedicated waiting tools: actual mail body, no bad references or payment, one owner report, one real assistant activation, and an explicit Inbox wait. |

The final checkpoint records `finish_activation` with `status=waiting` and
`wait_for=["inbox_received"]`. The dedicated wait tools were available, but the
model chose the general completion tool with the correct event. Its delivered reply was:

> Bob 发来的关于午饭金额的邮件已查收，内容是金额尚未核实，确认后会再通知。暂时不付款，等待他的新消息。

After that reply, the supplied Actor chose `wait`. The Director paused without
calling the real assistant again. [Replay this probe](http://127.0.0.1:8000/console?run=e3138220-dc87-4ff8-acd7-7691f16e32e4&turn=0).
[Probe results](inbox-wait-repair-probes.json) retain original checks, stricter
review checks, actual completion tools from checkpoints and usage. Full private
reports and credentials remain separate under `.local/inbox-wait-probes/`.

## Probe costs and subsequent acceptance

The six debugging probes made **26 provider requests** and cost **0.0226288 USD**.
At the end of these probes, campaign `635aa42e-386f-43dc-b679-b73cd0cd276f`
totaled **302 provider attempts and 0.8463964 USD of its 1 USD ceiling**, leaving
**0.1536036 USD** with zero outstanding or uncertain reservations. During the probes,
no ceiling was raised or new campaign created to bypass it. The final probe is one successful integration example after
retained failures; it does not establish repeatability or Actor naturalness.

The user then approved a total campaign ceiling of 2 USD. The trusted ledger
update preserved all prior spending. Three unchanged baseline repeats and one
each of `insufficient`, `ambiguous` and `duplicate` used GPT-4.1 Actors and reviewers
with GPT-4.1-mini assistants. These six runs cost **0.4376184 USD** over 160 requests.
The campaign now totals **1.2840148 USD over 462 attempts**, with **0.7159852 USD**
remaining and no outstanding or uncertain reservations.

The [new comparison](TRANSFER_REPAIR_VALIDATION_RESULTS.md) retains all six runs,
including the unknown-amount failure. Its checkpoint shows 14 repeated empty
scratch searches after two valid reads, exhausting Bob's 16-call task allowance.
No task counter was reset and no prompt was tuned between repeats. The prior
probe success does not override this real-Actor failure.

The subsequent [empty-scratch repair](EMPTY_SCRATCH_REPAIRS.md) eliminates the
empty-file searches in three real-Actor runs. Two reach normal waiting; the third
still repeats deferral to the window limit and fails the strengthened rubric.
That report records the later uncertain-cost reservations and untested v3 Actor
guidance. The budget values above are historical snapshots, not current headroom.

`run_transfer_actors.py --budget-usd` states the requested ceiling but must match
the ledger. It cannot increase an existing campaign. Budget approval and a trusted
ledger update are required before a comparison above the saved ceiling. All new
comparison runs must reuse this campaign; failures and resumes stay charged to it.
