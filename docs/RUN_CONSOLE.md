# Run console

The Run console lets a local operator inspect saved turns, run the next turn,
and change a run through a prompt. Open <http://127.0.0.1:8000/console> after the
local server starts. **Replay** reads saved PostgreSQL evidence. **Live controls**
can start model work. The bottom **Play** control always replays saved turns.

## Start and select a run

1. Follow [Local setup](../README.md#local-setup) to start PostgreSQL and the API.
2. Open `/console` directly on the loopback server. No token or sign-in is needed.
3. Select a saved run. Use **Refresh saved runs** or **Load more** when needed.
4. Select a turn with the slider, a numbered marker, or the previous/next buttons.
5. Select a person in **Cast**. Use **Show selected pair only** to focus the
   conversation and event log on that owner and assistant. Clear it to show all
   pairs in either a two-owner or a larger run.

The cast uses numeric owner-ID order, so `owner-2` precedes `owner-10`. Agent
labels use the names saved with the run. In each conversation panel, **Owner**
and **Agent** show message counts and jump to that speaker's first saved message
in the turn. A control is disabled when that speaker has no saved message.

For the dramatic scenario, open
[After the verdict, first turn](http://127.0.0.1:8000/console?run=7b17d4bc-0019-46b9-92c9-88fc28a1f65c&turn=0).
Its [results](AFTER_THE_VERDICT_RESULTS.md) and
[full transcript](AFTER_THE_VERDICT_TRANSCRIPT.md) explain the observed behavior.
For the fifteen-owner control, select run
`cb6f3da3-b828-4c59-ba10-3a79d6386f8a`.

Reloading the page keeps access to the local server without a sign-in step.
The browser does not store or send an administrator token. **Copy link to this
turn** copies a run ID and a zero-based window number. The link opens against
the local server on the computer where it is used. The first displayed turn is
**Turn 01**; its URL and API window value is `0`.

Token-free access requires a loopback server, a direct loopback connection, and
a matching browser origin. Requests from other origins or with an unaccepted
Host do not gain local operator access. `ADMIN_TOKEN` remains in `.env` for
backend scripts and provisioning. Agent API credentials and participant access
rules are unchanged. See the [API access contract](API.md#local-console-access).

## Read a turn

| View or control | What it shows |
| --- | --- |
| Conversation | Delivered owner and assistant messages from the selected turn only. An absent reply is shown as absent. |
| Cast | All owner–Agent pairs. The selected person controls the inspector. The focus checkbox also filters conversation panels and logs. |
| Persona | The Director cue, goals, beliefs, known facts, initial request, decision boundaries, and observations from saved Actor input, when present. Values are not generated again. |
| Agent | Saved activation status, result, attempts, tool records, and an input detail view. Activation text and delivered chat are separate records. |
| Resources | The Resource view captured at the start of the selected Actor activation. It includes saved visibility, version, edit permission, and content. |
| Event log | Saved chat, mail, board, tool, Director, and error records for this turn. Select a channel or search the loaded records. |
| Record details | Select a log row, a message's inspect button, or a tool button to open its saved evidence. Text is displayed as text. |

The event log is ordered by saved timestamps and record IDs. Search applies to
the selected turn and current channel. With focus enabled, mail can appear for
either the selected sender or recipient. Run-wide events without an owner do
not appear in that focused view. Clear focus to inspect the complete turn log.

Resources show what the Actor could see when its activation started. A change
later in the turn appears in the tool or Director records. It does not replace
the earlier snapshot. If an older run has no saved Actor input or Resource view,
the console reports that gap. Saved truncation flags are shown. The console does
not reconstruct omitted content from current state or future turns.

## Replay and follow progress

**Play** moves to the next saved turn after the selected delay: Fast is one
second, Normal is 2.5 seconds, and Slow is five seconds, plus loading time.
Playback stops at the final recorded turn. Selecting Play there starts again
from the first turn. It pauses when the page is hidden. Arrow keys move between
turns, and Space controls playback when focus is outside interactive controls.

**Follow latest** reads new saved data every five seconds while the page is
visible. It does not start a worker. Starting live work from the current turn
enables this mode. Manual turn selection disables it. Live status continues to
refresh while you inspect an earlier turn. The refresh button reloads the selected
turn. An active turn can still gain records; refresh after it completes before
treating its contents as final.

The console loads records in pages of up to 100 per collection. It loads up to
20 pages at a time. If more records remain, select **Load more records from this
turn** in the event log. A partial page set is marked; missing reply text is not
treated as complete evidence while more pages remain.

## Run and pause

Select **Live controls**. The left panel shows the live cursor, execution status,
and campaign budget. The viewed turn can differ from the live cursor.

| Control | Effect |
| --- | --- |
| Step one turn | Run one complete Actor, Agent, and settlement cycle, then pause. If the turn is partly complete, finish that turn. |
| Continue | Run from the live cursor until the scenario ends or execution pauses. |
| Pause run | Save a pause request. A provider call already in progress can finish and incur cost. Its output is retained; delivery waits for explicit resume. |
| Replay | Hide the live control panel and inspect saved evidence. Changing the view does not pause a running worker. |

Live execution uses the selected run's saved model settings and private local
credential file. The browser cannot choose a command or filesystem path. A
missing manifest, exhausted campaign, active worker, or ended run disables
execution with a stated reason. The API starts one local worker per run.

The budget display uses the existing shared campaign. All participating roles,
retries, and resumed work use that ceiling. Outstanding or uncertain requests
keep their reservations. Inspect **Model costs** in `/runs` for the ledger.

## Insert a prompt

1. Select a person in **Cast**.
2. Choose **Actor · Director cue** or **Agent · owner message**.
3. Enter the prompt. The field accepts up to 12,000 characters.
4. Select **Queue for Turn NN** to add it to the current run.
5. Read the receipt. It shows the effective turn. Use Step or Continue if paused.

An Actor cue changes the private context given to that simulated owner. An Agent
prompt becomes a message from its owner. The prompt applies before the next
available Actor phase. If work for the current turn is already prepared, the
receipt names a later turn. A queued prompt is not an immediate Agent reply.
The receipt changes to **Prompt applied** when the saved status changes.

Prompts do not rewrite the frozen scenario or create facts, inventory, credit,
or consent by assertion. Actors and Agents still use their existing interfaces.
Typed actions and environment checks establish world changes.

If the run advances before a prompt is accepted, the service rejects the stale
request. The console retains the text and refreshes the target turn. If a response
is lost, **Retry same request** uses the original operation ID. This prevents a
second write after an accepted request loses its response. Changed prompt text
starts a new operation. A new prompt does not cancel an uncertain earlier one.

## Branch before a selected turn

For example, to change the direction at **Turn 03**:

1. Select Turn 03, then select a person.
2. Enter an optional Actor cue or Agent owner message.
3. Select **Branch before Turn 03**.
4. The console opens a new paused run. Select Step or Continue to run Turn 03 again.

The boundary is **before** the selected turn. Turn 03 uses API window `2`.
The child retains earlier state and excludes the original Turn 03 outcome and
later outcomes. The original run is preserved. The link below the child title
returns to its parent and branch point. Creating a branch does not run a model.

A complete boundary includes Actor and Director records, board and mail history,
delivery receipts, Resources, world state, market state, and completed Agent task
receipts. Pending and applied console prompt receipts retain their state at the
boundary. Prompts added after that boundary are excluded. It also includes Agent graph memory and private scratch files. The
implementation resolves LangGraph delta-backed values before copying the graph.
A text transcript alone is not enough to restore these components.

The child receives separate run, cluster, Agent, session, and credential identities.
References in copied records and Agent memory are mapped to those identities.
Known paged tool messages from `list_threads`, `read_thread`, and `list_mail`
have their continuation cursor cleared and set
`branch_pagination_reset_required: true`. The Agent must restart that query from
its first page because child identity and sequence values differ. Arbitrary
document JSON retains its content; cursor expiry applies to tool paging results.

Model spend and cost reservations are outside the restored state. The child uses
the parent's campaign and ceiling. Branching cannot reverse spend or create new
budget. Further model output can differ, even without an added prompt. See the
[LangGraph time travel guide](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
for the underlying checkpoint model.

Older runs do not have full snapshots for each historical turn. The console
disables an unavailable branch and gives a reason. It does not reconstruct
missing Agent memory from current state. **Restart initial scenario** creates a
paused child at Turn 01 from the frozen initial scenario. Initial restart does
not retain the old run's later memory or world changes. Incomplete checkpoints
and older request records without their original input also prevent restoration.

## Limits and other views

The console is a local operator view of synthetic private records. Selecting
one person changes the display filter. It does not change access rights or show
what another Agent was allowed to read. Local access applies only to observer
and console routes; it does not grant an Agent broader permissions.

Open **Data observer** at `/runs` for full reports, campaign costs, current tables,
and the frozen plot. The console's **Errors** channel shows saved failures and
rejected tool results. It does not evaluate the quality of assistant advice.
Use the saved report and actual dialogue together when reviewing conduct.

The [replay API](API.md#saved-turn-replay-api) and
[live control API](API.md#live-console-control-api) define the routes. The diagram
still has the same thirteen nodes and twenty-eight connections. These console
operations are operator controls and do not add simulated participants.

## Verification

E-17 records the earlier replay delivery: 783 available PostgreSQL tests and
22 browser checks. The two cloud-evicted modules were excluded:
`tests/test_evaluation.py` and `tests/test_scenario_author.py`.

For the earlier live control extension, **31 browser checks passed**. These include the
22 replay checks and nine live-control checks. The tests cover Step, Continue,
Pause, prompt targets, effective turns, applied receipts, lost-response retries,
changed requests, stale responses, branch creation, initial restart, parent links,
and expired authentication. Desktop and mobile screenshots were inspected.
There were no script errors or horizontal page overflow. Control writes used
intercepted test responses. No real control write or model call was made by this
browser test. This result does not establish backend execution or model quality.

A real API and model test restarted the initial scenario, ran three turns, then
created a child before Turn 03. The child completed one turn and paused. The
selected owner's prompt and Agent reply were saved; the peer did not receive
the prompt. The original and baseline records remained unchanged. The actual
child was inspected at 1600×1000 and 390×844 with mutations blocked. Its Turn 03,
parent link, and live controls were correct, with no script errors or overflow. The test used
0.333548 USD from the existing campaign, which reached 21.7639864 USD of 30 USD.

The final available Python suite passed 845 tests in 72.06 seconds with real
PostgreSQL after all corrections. The same two unavailable modules remain
excluded. Ruff checks, formatting for 90 files, and Node syntax checks passed.
See the
[live validation record](CONSOLE_LIVE_VALIDATION.md) for exact run IDs, checks,
costs, and limits. This test did not complete a new eight-turn story or assess
conduct. E-17 and E-18 remain historical.

E-19 in the [PRD](../PRD.md#evidence-register) records the removal of the local
sign-in step. Its focused PostgreSQL suite passed 134 tests, including 23 new
local access checks. All 34 browser checks passed. Both pages opened without an
Authorization header; desktop and mobile views were inspected. No script error,
real control write, or model call occurred in those browser tests. Live HTTP and
the in-app observer also confirmed direct local access. Foreign-origin requests
were rejected. Ruff, formatting, and JavaScript syntax checks passed. This change
used no paid model calls.
The [browser runner](../scripts/test_console_ui.mjs) writes
[local evidence](../.local/console-tests/results.json) and screenshots under
`.local/console-tests/`. It blocks real console writes unless a test intercepts
them. These local artifacts are ignored by Git.
