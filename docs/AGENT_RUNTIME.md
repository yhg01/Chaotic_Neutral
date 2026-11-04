# Personal Agent runtime

The local runtime uses Deep Agents 0.7.32, LangChain 1.4.7, LangGraph 1.2.18,
and the LangGraph PostgreSQL checkpointer 3.2.0. The OpenRouter integration is
langchain-openrouter 0.2.9. These dependencies are pinned in the lock file.
The application runs the libraries directly. It does not require a managed
Agent Server or hosted LangSmith account.

## Run a task

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
up to 20,000 characters and a loopback HTTP board URL. `--model` overrides
`OPENROUTER_MODEL`. The default model is `openai/gpt-4.1-mini`.

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

The Director delivers a natural final response to the owner when the activation
has not already sent an explicit owner message. It suppresses repeated identical
final text and pending-action status receipts. See `DIRECTOR_RUNTIME.md` for run
commands, owner chat, settlement, private evidence, and recovery.


## Natural assistant surface

New runs use the natural surface in [agent_surface.py](../src/agentrpg/agent_surface.py).
The assistant's input reads as an ordinary working day. Its system prompt, tool
names, descriptions, results, errors, and notifications contain no Director,
activation, window, run, simulation, or plot terms. For example, `finish_activation`
appears as `end_turn` and `submit_world_action` appears as `submit_booking`. Run
fields and timestamps are removed from the model's copy of each tool result.

Saved events, reports, observer views, and HTTP routes keep internal names and
original results. `configuration.agent_surface` in the run manifest selects the
surface. A manifest without this field stays `legacy`, so older runs and their
branches keep their original prompts. Use `--agent-surface legacy` on a new run to
compare the two. This reduces explicit test language; it does not prove that a
model is unaware of an evaluation. UUIDs and fast message timing remain visible.

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
