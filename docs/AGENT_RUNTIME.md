# Personal Agent runtime

The [Actor memory profile](ACTOR_MEMORY.md) belongs to simulated human owners.
It changes no personal Agent tool, model/API selection or transaction authority.
Private memories/agendas are not copied to an assistant workspace; the assistant
receives actual owner dialogue through the existing channel.

The opt-in [confirmation profile](CONFIRMATION_CLOSEOUT.md) offers
`list_confirmations`, `request_confirmation`, `ask_confirmation_owner`,
`reply_confirmation` and `report_confirmation`. The native service preserves
declared terms and conditions and rejects replies without the peer Actor's
actual decision. The graph corrects premature yields before they become final
results, including standby chatter before incoming mail. The requester must
read and acknowledge the exact reply before the native owner report can succeed.
This dedicated profile uses empty final text; reports are already delivered
through its tool. It is separate from seven-day acceptance.

The opt-in [elapsed Actor/Agent profile](CLOCK_RUNTIME.md) adds model/tool
boundaries, released native writes and frozen role bindings for new elapsed
runs. It preserves existing window profiles and historical records.

The [current-cue protocol](CUE_RESPONSE_REPAIR.md) can require the Actor to convey
a trusted new Director instruction before Agent work begins. It does not change
the personal Agent model, provider credentials, tool permissions or its decisions.
The two-event short test verifies request propagation; its final confirmation
exchange remains incomplete and is retained as a separate conduct finding.

The opt-in [timed transfer adapter](CLOCK_TRANSFERS.md) uses
`EnvironmentTools(..., timed_transfers=True)`. Its pending transfer result yields
the activation; a later activation reads the original committed receipt with
`read_transfer_operation`. The flag is preserved in private adapter copies and
session policy hashes. Default legacy tool sets/hashes stay unchanged. Local
graph/HTTP/checkpoint checks use supplied model responses; no new live-provider
or full elapsed Director/model driver verification is claimed.

The local runtime uses Deep Agents 0.7.32, LangChain 1.4.7, LangGraph 1.2.18,
and the LangGraph PostgreSQL checkpointer 3.2.0. The OpenRouter integration is
langchain-openrouter 0.2.9. These dependencies are pinned in the lock file.
The application runs the libraries directly. It does not require a managed
Agent Server or hosted LangSmith account.

## Optional empty-scratch guard

`AgentRuntime.agent(..., guard_empty_scratch=True)` hides `ls`, `glob`, `grep`
and `read_file` while the restored private file mapping is empty. Write and
environment tools remain available. Reads become available after a scratch write,
including after runtime recreation. A provider call to a hidden scratch-read tool
receives a saved `empty_scratch` correction rather than a fabricated result.
This option is session-bound and defaults to false for legacy callers. Filtering
is per request and does not change another Agent's tool set.

New transfer runs enable this option. [Retest evidence](EMPTY_SCRATCH_REPAIRS.md)
separates the fixed empty-file search loop from unresolved conversation-loop
issues. Later v3/v4 policy comparisons remain separate evidence. It does not claim that all
repeated searches or waiting decisions are now correct.

## Optional persistent transfer tasks

`EnvironmentTools(..., track_transfer_tasks=True)` offers `list_transfer_tasks`,
`defer_transfer_task` and `complete_transfer_task`. The runtime preserves this
option when copying the adapter and binds it to the session instruction hash.
The Director run must have enabled tracking before it starts; there is no Agent
endpoint to enable it, open tasks, grant authority or cancel tasks.

In this profile, `ActivationResult` and `agent_tasks` describe one graph execution.
The separate business record survives a `finished` activation. The scheduler
reads open tasks without changing the saved model output. Actual peer mail wakes
an inbox-blocked task for review; owner permission remains a separate requirement.
Completion requires the matching committed payment and notice/report evidence.
The Actor owns typed task opening and cancellation. Read the
[contract and local evidence](TRANSFER_TASK_LIFECYCLE.md) for the exact checks
and recovery guarantees. The later [model results](TRANSFER_TASK_MODEL_RESULTS.md)
retain actual successes, corrections and failures separately.

`guard_transfer_task_yield=True` adds an opt-in v8 check before a completion or
wait tool ends an activation. If the owner's task is still active, the exact tool
response becomes `active_transfer_task_requires_update`. The model must save a
business wait or complete the paid task with real evidence, then retry yielding.
The guard does not select a task, change its state, or fabricate an owner reply.
It preserves the model's final activation result after the task update. Pending
settlement keeps its existing behavior. Call and time limits still apply.

The flag defaults to false, requires task-enabled environment tools, and binds to
the session instruction hash and restored branch policy. Completed writes,
corrections and call counters survive checkpoint resume. A new comparison can
use `--track-transfer-tasks --guard-transfer-task-yield` in the Actor runner.

## Optional previous activation context

`AgentRuntime.agent(..., include_previous_activation_context=True)` adds the
Agent's own preceding activation ID, status, expected events and pending IDs to
its model context when the preceding task was an activation. The fields come from
that session's saved checkpoint, before
the new activation resets its current result. The middleware labels them as the
Agent's prior declaration. It includes no hidden scenario facts or peer state.

This is context for a model decision. New evidence, cancellation or changed
instructions can justify leaving the previous wait. The runtime does not replace
the model's result or infer intent from acknowledgement text. The option defaults
to false, is bound to the session instruction hash, and preserves the existing
canonical activation input. A retry reuses the frozen context from its checkpoint.
New transfer manifests save the flag; old manifests resume with it disabled.
The [v5 diagnostics](OWNER_ACK_V5_RESULTS.md) retain two premature finishes in
three probes. The [v6 context probes](OWNER_ACK_V6_RESULTS.md) retain one in three,
despite correct saved context in every trial. These small component tests do not
establish an improvement or a stable complete model-Actor scenario.

## Actor mail delegation review

`ModelActor(..., reviewer=reviewer, require_mail_intent_review=True)` requires a
reviewer at construction. For nonempty Actor `mail_requests`, the reviewer must
classify all mail as explicit owner-direct correspondence or identify any
assistant-delegated mail. The latter is rejected for repair before Director
effects can be applied, even if the reviewer also says `accepted=true`. The
delegated owner instruction remains dialogue; its duplicate typed effect must be
removed. Ordinary no-mail reviews and the default false mode retain their prior
shape. Valid direct personal mail and reflection remain permitted.

The gate enforces the structured classification, not the truth of an LLM's
semantic judgment. Its contract tests cover both valid direct correspondence and
delegated-effect rejection. New transfer manifests snapshot the flag and full
generation/review prompts. See the [v3 failures](QUIET_ACTOR_V3_RESULTS.md) and
[v4 repair evidence](DELEGATION_V4_RESULTS.md) for the distinction between enabled
review, actual live-path exercise, and remaining waiting/wording problems.

## Run a task

An optional [assistant-only API profile](PERSONAL_AGENT_API.md) selects DeepSeek
V3.2 and a separate key from `.env.personal-agent.local`. Actors and review roles
keep the original configuration. Missing personal credentials fail before any
provider call. Historical scenario runs keep their frozen route and model.

Start PostgreSQL and the API, then run `scripts/seed_demo.py` as described in the
[README](../README.md). Configure `OPENROUTER_API_KEY` in the ignored `.env` file.
The CLI uses the Agent credentials in `.local/demo-credentials.json`.

```sh
uv run python scripts/run_agent.py --agent 1 --task "Read the board and summarize the open threads."
uv run python scripts/run_agent.py --agent 2 --task "Read the board and summarize the open threads."
```

Each process prints the Agent, session, and task IDs before the model starts.
The default session is stable for that Agent. New calls without `--task-id`
create new turns in the same conversation. To use another conversation, supply
`--session` with a UUID. To retry a task, supply its original session, task ID,
and exact task text:

```sh
uv run python scripts/run_agent.py --agent 1 \
  --session 11111111-1111-4111-8111-111111111111 \
  --task-id 22222222-2222-4222-8222-222222222222 \
  --task "Read the board and summarize the open threads."
```

Use the IDs printed by your run. The sample IDs above only show the format.
A completed task returns its saved answer without a new model call. Reusing a
task ID with different text is rejected. An unfinished task must resume before
a new task can start in that session. Agent instructions are fixed per session.
Use a new session if those instructions change.

The CLI reads task text from standard input when `--task` is absent. It accepts
up to 20,000 characters and a loopback HTTP board URL. `--model` overrides the
enabled personal Agent profile, then the legacy `OPENROUTER_MODEL` default.
Without either setting the default is `openai/gpt-4.1-mini`.

## State, identity, and concurrency

`AgentRuntime` owns PostgreSQL connection pools. It applies the application
migrations and the checkpointer's migrations on entry. Schema setup uses a short
startup lock. It does not lock board posting or model execution.

The checkpoint key includes the cluster, Agent, and session IDs. Before each
run, the runtime checks `/api/v1/me` with the supplied Agent credential. A
credential for another Agent or cluster cannot run under that session identity.
Credentials remain in clients, outside graph state and model messages.

A separate connection pool holds session locks. One session can have one active
run. A second run for that session receives a busy error. Independent Agents
can continue concurrently, including fifteen peers in one cluster. The lock pool
is separate from the checkpoint pool so active sessions cannot consume every
connection needed to save their state. The default maximum is eight general
connections and fifteen session-lock connections per runtime process. Model
calls hold no open transaction. The smaller general pool limits database use;
it does not limit the number of concurrent model calls.

The API has a separate pool with a default maximum of twenty connections per
API process. A scenario runner can therefore use up to 23 pooled connections,
plus short startup connections. Multiple runners, API processes, test fixtures,
and other clients share the PostgreSQL server's connection limit. Run the full
test suite separately from several concurrent fifteen-Agent model runs. The
Python `pool_size` option can change the general pool when a deployment needs it.

PostgreSQL stores conversation messages, private virtual scratch files, task
receipts, and model-call counts. These runtime records are separate from the
Director's environment tables, owner Resource workspaces, and logical Clock.
The scratch backend cannot read host files or execute host commands. The runtime
disables the default extra subagent. Each personal Agent is an independent peer.

## Writes and recovery

Graph checkpoints are saved synchronously. A checkpoint contains the model's
tool call IDs. The board adapter derives a request ID from the session, task,
and tool call. Repeating a saved post or reply therefore returns the existing
record. A changed payload with that request ID is rejected by the board.

The task ledger is separate from the graph. If the graph finishes before the
completion receipt is saved, the runtime reads the saved final response and
completes the receipt. It does not run the model or tools again.

After a temporary provider failure, timeout, cancellation, or process restart,
retry the same task with its original IDs and text. Previously committed writes
remain committed. A retry can continue from the last saved graph state. Reading
messages does not acknowledge them; the Agent must call `acknowledge` explicitly.

The default task limit is twelve model calls, including conversation summaries,
and 120 seconds per attempt.
The call counter persists across retries. Provider calls allow at most 1,000
output tokens, a 45-second request timeout, and two retries for temporary errors.
The graph also has a finite step limit. Token and cost metrics count successful
provider responses. Request counts describe logical model calls; internal HTTP
retries can add requests and cost.

A task that exhausts its stored call budget cannot continue under that same
budget. Use a new session for a new task, or deliberately increase
`max_model_calls` through the Python API or `--max-model-calls` on the CLI, then
resume the original task. Both accept a limit up to 100. Inspect its saved board
effects before deciding what new work to request. The CLI does
not reset the stored count or delete pending work.

## Python interface

Use a board HTTP client with one Agent bearer credential and an API base URL
ending in `/api/v1/`. The caller owns the model and HTTP client lifetimes.

```python
from uuid import UUID

from agentrpg.agent_model import UsageTracker, create_model
from agentrpg.agent_tools import BoardTools
from agentrpg.deep_agent import AgentRuntime

usage = UsageTracker()
model = create_model(provider_key, model="openai/gpt-4.1-mini")
try:
    async with AgentRuntime(database_url) as runtime:
        agent = runtime.agent(
            model=model,
            board=BoardTools(board_client, str(cluster_id)),
            agent_id=UUID(agent_id),
            session_id=UUID(session_id),
            instructions="Use the board to complete the owner's task.",
            callbacks=[usage],
        )
        answer = await agent.run(task_text, task_id=UUID(task_id))
finally:
    await model.aclose()
```

The runtime disables automatic LangSmith tracing, including when tracing is
set in the parent environment. The default provider client also disables debug
logging. Local reports contain synthetic verification data. The runtime exposes
safe public errors through `AgentRuntimeError` and its subclasses.

## Verification and scope

[Verification results](VERIFICATION.md) record actual checks. The
[PRD](../PRD.md) maps them to components and connections. The repeatable real model
runner tests two and fifteen Agents, closes and reopens the runtime between
phases, and checks completed task replay. Deterministic graph tests also cover
interruption after a board write commits and before its tool result is saved.

Run-bound Agents can use the board, private owner chat, internal mail, permitted
owner Resources, transport tools, and the simulated item market. Search and fetch
operate on environment records. The Director supplies activations, logical time,
and declared recurring events. PostgreSQL stores the environment state.
The standalone Agent CLI is an operator entry point; scenario runs use the
Director described below. Model reasoning quality and long-duration service
reliability require further task-specific evaluation.

## Director activations

A run binds an `EnvironmentTools` adapter to the same authenticated HTTP client
as the board adapter. `run_activation(ActivationEnvelope(...))` verifies the run,
Agent, and session identity before execution. The packet separates owner messages
from environment observations and has a stable activation ID and source event
IDs. The runtime stores its exact input hash in the task receipt.

The result is `responded`, `waiting`, or `finished`. Waiting is a completed task.
A pending transport or market action stops further model calls in that activation.
The next Director event starts another task in the same conversation. A completed
retry returns the saved result. Stable tool-call IDs prevent duplicate effects.

`require_explicit_finish=True` is an optional, session-bound runtime mode. It
requires a completion tool instead of a plain final response and offers
`wait_for_mail` and `wait_for_owner`. `ActivationResult.wait_for` records expected
external events. The Director suppresses empty-clock polling for explicit waits
and pauses quiescent work with `waiting_for_events`. The legacy default is
unchanged. Notification body evidence survives both checkpoint recovery and later
activations; an explicit `finished` cannot use an empty inbox query or a mail
acknowledgement as proof of reading. [Verification and limits](INBOX_WAIT_REPAIRS.md)
distinguish the real-assistant probe from the pending real-Actor comparison.

The Director delivers a natural final response to the owner when the activation
has not already sent an explicit owner message. It suppresses repeated identical
final text and pending-action status receipts. See `DIRECTOR_RUNTIME.md` for run
commands, owner chat, settlement, private evidence, and recovery.


## Mail, Resources, and cost visibility

`EnvironmentTools` also exposes the Inbox and Resource tool registries. The same
Agent credential selects the owner for every request. An Agent can read and send
private internal mail, explicitly acknowledge received mail, search permitted
records, fetch or update delegated content, create private documents, and submit
consent-bound market requests. Version checks reject stale Resource edits. Stable
write IDs protect effects when graph execution resumes.

The [extension guide](ENVIRONMENT_EXTENSIONS.md) contains the complete access
contract. Actors receive their own private observations and issue typed requests
through the Director. They do not use the assistant's tools.

The scenario runner accepts `--campaign UUID --budget-usd 30`. A bound campaign
tracks assistant, Actor, response-review, and final-review attempts together.
Each request reserves cost before it is sent. Retries need their own reservation;
uncertain attempts retain theirs. Resume preserves the campaign and its spend.
The stored model-call limit still applies to each Agent task.

Open `/runs` on the local API to inspect timeline, private messages, mail,
Resources, transactions, jobs, plot, and campaign cost. Use the administrator
token from `.env` only in the local observer. Agent credentials cannot open that
view. Saved reports remain under `.local/scenario-runs/<run-id>/`.


## Console execution and turn branches

The [Run console](RUN_CONSOLE.md) can start a bounded worker with the saved Agent
configuration. Step completes one full decision window. Continue runs until the
scenario ends or pauses. An operator pause does not cancel a pending provider
call. Completed output is retained, and delivery resumes only on an explicit
execution request. The existing campaign ceiling and task limits still apply.

An operator can queue a private Actor cue or an owner message for an Agent.
The service applies it before an available Actor phase and saves its effective
window. Prompt text cannot replace typed world actions or grant tool authority.

A historical branch creates a separate paused run before a selected window.
The boundary contains both environment records and completed Agent graph state,
including conversation and private scratch files. Pending and applied console
prompt receipts retain their state at that boundary. Later prompts are excluded.
Delta-backed graph values are resolved before they are copied. Child run, cluster, Agent, and session identities
are new; copied records and memory references use the child identities. The
original run remains available. Reports, spending, and provider credentials are
not copied as simulated history. The child remains bound to the same campaign.

Saved `next_cursor` values are cleared in known paged tool messages from
`list_threads`, `read_thread`, and `list_mail`. The result includes
`branch_pagination_reset_required: true` and a note to start the query again.
An Agent must restart paging from the first page in the child run. A saved cursor
cannot address the child's new identity and sequence values. Arbitrary document
JSON retains its content, including fields that happen to be named `next_cursor`.

Only complete boundaries can restore historical Agent state. Missing checkpoints,
unfinished graph work, or older request records without their input prevent that
restoration. An initial-scenario restart starts new Agent history at window 0.
See [run branches](../src/agentrpg/run_branches.py) and the
[control API](API.md#live-console-control-api) for the implemented contract.

## Browser shop profile

See [Browser shop runtime](BROWSER_SHOP_RUNTIME.md) for the optional Chrome tools,
owner purchase tasks, existing-market settlement, checkpoint recovery, repeat
purchase evidence and remaining long-duration limits. Its read-only storefront
is `/shop?run=<id>&observer=1&owner=alice`; the existing console displays its
trajectory. Use `scripts/try_browser_shop.py --resume <credentials.json>` for
execution recovery. The general console model worker does not run this profile.

## Opt-in runtime supervision

[Runtime supervision](RUNTIME_SUPERVISION.md) records classified model/tool
failures, durable retry deadlines and shopping task progress. It reuses graph
checkpoints, stable operations and original counters. Migration 016 is required.
Enable it before the first job with a frozen supervision policy. The browser
runner exposes `--supervise`; the generic console worker does not apply this
policy automatically. Existing runs stay disabled. No paid validation was added.

## Acceptance audit

The [autonomy acceptance tools](AUTONOMY_ACCEPTANCE.md) record hash-only trial
boundaries in the repayment and shopping drivers. Director controls are durable
events. The collector separates world checks, observed operator actions, provider
role coverage and real invocation duration. These measurements do not change
Agent instructions, task IDs, retry limits or authorization. A missing historical
audit remains unknown; a resumed invocation is not another independent trial.

## Per-owner model assignments

The scenario CLI accepts repeated `--owner-model OWNER=MODEL` overrides. It
validates common owner identities, saves the mapping in the run configuration,
and rejects changed mappings on resume. Overrides require an explicit existing
campaign and exact cumulative ceiling. Each assistant keeps its own tools,
credentials and history. The Actor/reviewer model is configured separately.

The bounded model allowlist now includes DeepSeek V3 0324 and Qwen3 30B A3B
Instruct 2507. Their reservations use the full declared input context plus
capped output. Public metadata must satisfy the expected text and tool contract;
price ceilings, disabled provider fallbacks and uncertain-cost holds remain.
No model API or budget is selected by an assistant message.

The [funding comparison](MODEL_COMPARISON.md) preserves a six-slot role-swap plan
and the original campaign. One mixed-model pilot made real tool calls and then
paused at a budget-classified Actor failure. Report recovery is read-only and
cannot launch a worker. A changed source still blocks generation under the old
plan. This generic scenario driver records invocation boundaries but does not
automatically enable the opt-in shopping supervisor.
