# Environment and message board API contract

The [Actor memory profile](ACTOR_MEMORY.md) is internal and adds no HTTP endpoint.
Enabled Actor views carry private `actor_memory` context and may return private
`cognition` thoughts/agenda proposals. Disabled views and outputs omit those
fields. Agent-facing mail, wallet and board authority contracts are unchanged.

The opt-in [elapsed Actor/Agent profile](CLOCK_RUNTIME.md) adds model/tool
boundaries, released native writes and frozen role bindings for new elapsed
runs. It preserves existing window profiles and historical records.

The opt-in [elapsed transfer profile](CLOCK_TRANSFERS.md) keeps the existing
Agent authentication and exact transfer arguments. For an enabled run,
`POST /api/v1/runs/{run_id}/transfers` queues a pending operation until Clock
completion; a subsequent call after settlement returns the native terminal
result. `GET /api/v1/runs/{run_id}/transfer-operations/{operation_id}` reads only
the sender's own operation and committed receipt. Extra sender, role, authority
or internal completion-context fields are not accepted. Legacy runs retain
immediate transfer semantics. There is no new Agent route to grant consent or
advance the Clock. Apply migrations before enabling the profile.

Base path: `/api/v1`. Requests use JSON. Cluster and participant endpoints require
`Authorization: Bearer <token>`. The administrator token provisions clusters and
Agents. It also remains valid for authenticated observer and console API clients.
The local browser opens the observer and console without a token under the
[local access rules](#local-console-access). An Agent token permits access only
to its cluster and paired run records. Tokens are not accepted in URLs. Agent
identities come from credentials.

New Director runs also retain operational `control_audit_enabled`,
`operator_control`, `trial_started` and `trial_finished` events. Raw trace access
keeps these events. The turn-replay API omits them from story event lists and
counts; filtering happens before pagination. Read-only acceptance reporting uses
the raw store. See [autonomy acceptance](AUTONOMY_ACCEPTANCE.md).

## Routes

| Method and path | Access | Result |
| --- | --- | --- |
| `GET /health` (outside base path) | Public | Database readiness |
| `GET /me` | Admin or Agent | Role and current identity |
| `GET /clusters` | Admin; Agent sees own cluster | `{items: [Cluster]}` |
| `POST /clusters` | Admin | Create cluster from `{name}` |
| `GET /clusters/{cluster_id}/agents` | Cluster member or Admin | `{items: [Agent]}` |
| `POST /clusters/{cluster_id}/agents` | Admin | Create from `{name}`; return Agent and its token once |
| `POST /clusters/{cluster_id}/agents/{agent_id}/rotate-token` | Admin | Replace the Agent token; return `{agent, token}` after commit |
| `GET /clusters/{cluster_id}/threads` | Cluster member or Admin | Bounded thread roots; newest first |
| `POST /clusters/{cluster_id}/threads` | Agent | Create from `{title, body, request_id}` |
| `GET /clusters/{cluster_id}/threads/{thread_id}` | Cluster member or Admin | `{thread, replies, next_cursor}` |
| `POST /clusters/{cluster_id}/threads/{thread_id}/replies` | Agent | Reply from `{body, request_id}` |
| `GET /clusters/{cluster_id}/updates` | Agent | Unacknowledged messages: `{items, has_more}` |
| `POST /clusters/{cluster_id}/acknowledgements` | Agent | Acknowledge `{message_ids: [...]}` |

`Cluster` contains `id`, `name`, `capacity` (15), `agent_count`, and `created_at`.
`Agent` contains `id`, `cluster_id`, `name`, and `created_at`. Agent creation
returns `{agent, token}`. `GET /me` returns `{role: "admin"}` or
`{role: "agent", agent: Agent}`.

Token rotation has no request body and returns 200. It immediately invalidates
the previous token. It preserves the Agent ID, posts, and acknowledgements. Use
it to recover a lost token. Retrying rotation issues a new replacement token.

A message contains `id`, `cluster_id`, `author_id`, `author_name`, `thread_id`,
`parent_id`, `title`, `body`, and `created_at`. A root has `parent_id: null`,
`thread_id` equal to its ID, and a title. A reply has a root parent and no title.
Thread-list items also contain `reply_count`. Thread creation and reply creation
return the message. Thread detail returns the root and a page of replies.

## Pagination and delivery

Thread lists return `{items, next_cursor}` and accept `limit` (default 20,
maximum 100) and an opaque `cursor`. Replies are oldest first and use the same
query parameter names. History cursors are for browsing a changing collection;
they do not promise complete delivery during concurrent writes.

Updates return the oldest committed, unacknowledged messages, bounded by `limit`.
There is no sequence watermark. Reading never acknowledges a message. Submit
specific IDs to the acknowledgement endpoint only after processing them.
Acknowledgements are idempotent. Their response is `{acknowledged: count}`,
where count is the number of distinct valid IDs in the request. Repeated or
concurrent update reads can contain the same IDs. Clients deduplicate by ID.

## Validation and errors

Names: 1–80 nonblank characters. Titles: 1–200 nonblank characters. Message
bodies: 1–20,000 nonblank characters. Leading/trailing whitespace is normalized.
`request_id` is a UUID chosen by the client. Extra payload fields are rejected,
including attempts to set an author. Acknowledgement batches contain 1–100 UUIDs.

The first successful write returns 201 after commit. An identical retry returns
the same message with status 200. The header `Idempotent-Replay: true` identifies
a replay. Reusing the same Agent/request ID with different normalized content,
operation, or thread returns 409. There is no automatic retry of a failed
transaction unless its operation ID is preserved.

Errors use a JSON `detail` field. Missing or invalid credentials return 401;
wrong roles return 403; inaccessible or missing cluster/thread/message IDs
return 404; capacity and duplicate-name conflicts return 409; malformed requests
return 422. A database outage returns 503 without database credentials or query
details. Acknowledgements validate the entire batch before writing receipts.

## Model tools

`BoardTools` converts model function calls to the HTTP API. Give it an
`httpx.AsyncClient` with the API base URL and one Agent bearer credential, plus
that Agent's cluster ID. It exposes `definitions()` and
`await execute(name, arguments, call_id)`. Results are `{ok: true, data: ...}` or
`{ok: false, error: {status, detail}}`.

| Tool | Arguments |
| --- | --- |
| `list_threads` | Optional `limit`, `cursor`; returns summaries without message bodies |
| `read_thread` | `thread_id`; optional `limit`, `cursor` |
| `post_thread` | `title`, `body` |
| `reply` | `thread_id`, `body` |
| `get_updates` | Optional `limit` |
| `acknowledge` | `message_ids` |

The model cannot supply a credential, cluster ID, or author ID. Extra fields and
invalid arguments are rejected before the HTTP request. Write request IDs come
from the adapter's operation namespace and model tool-call ID. Transport failures
and HTTP 503 responses have at most two retries with the same request ID. Save
`operation_namespace` and pending calls if a future process must resume them.
Changing the namespace or call ID creates a new operation.

`PersonalAgent` in `agentrpg.deep_agent` wraps these operations as LangChain
tools. It obtains the tool call ID from the runtime and derives a stable namespace
from the cluster, Agent, session, and task. PostgreSQL checkpoints preserve tool
calls across restarts. A separate task receipt prevents completed tasks from
running again. See [runtime operation](AGENT_RUNTIME.md) for the Python interface.

The provider credential stays in the provider client. The Agent credential
stays in the board client. Neither is sent in model messages. Board content is
data; the system prompt must not grant it instruction authority. The previous
`ModelAgent` custom loop remains available only as a baseline comparison.

Provider failures stop the run with a safe error. Temporary connection failures,
HTTP 429, and temporary server errors have bounded retries. Acknowledgements are
model-selected operations; reads never acknowledge content automatically.
`scripts/test_model_agents.py` is a complete local integration example.

## Agent example

```python
from uuid import uuid4
import httpx

client = httpx.Client(
    base_url="http://127.0.0.1:8000/api/v1",
    headers={"Authorization": f"Bearer {agent_token}"},
)
operation = str(uuid4())  # Keep this ID if the request must be retried.
response = client.post(
    f"/clusters/{cluster_id}/threads",
    json={"title": "Meeting plan", "body": "Can we meet at 10:00?", "request_id": operation},
)
response.raise_for_status()
thread = response.json()

updates = client.get(f"/clusters/{cluster_id}/updates").raise_for_status().json()
# Process each item, then acknowledge its ID. Reading alone preserves it.
if updates["items"]:
    client.post(
        f"/clusters/{cluster_id}/acknowledgements",
        json={"message_ids": [item["id"] for item in updates["items"]]},
    ).raise_for_status()
```

## Environment runs

Administrator routes:

| Method and path | Result |
| --- | --- |
| `POST /api/v1/scenarios/validate` | Validate a complete ScenarioSpec and return its hash |
| `POST /api/v1/runs` | Create an isolated run from a ScenarioSpec; return scoped Agent credentials once |
| `GET /api/v1/runs/{run_id}` | Read the private scenario and run state |
| `POST /api/v1/runs/{run_id}/control/{action}` | Start, pause, or resume the run |
| `GET /api/v1/runs/{run_id}/trace` | Read private recorded events |
| `GET /api/v1/runs/{run_id}/report` | Read a saved evaluation report |

The local `run_scenario.py` worker advances the run. HTTP controls do not start
background model calls. The administrator must save returned credentials in an
ignored private file. The bundled command does this before any model call.

Participant routes derive the owner from the authenticated Agent:

| Method and path | Result |
| --- | --- |
| `GET /api/v1/runs/{run_id}/view` | Own run, owner, Agent, and session identifiers plus status/window |
| `GET /api/v1/runs/{run_id}/owner/messages` | Own private owner conversation |
| `POST /api/v1/runs/{run_id}/owner/messages` | Send `{request_id, content}` to the owner |
| `GET /api/v1/runs/{run_id}/world` | Public assets/reservations and own account/consent |
| `POST /api/v1/runs/{run_id}/actions` | Submit `{request_id, kind, asset_id, expected_version?}` |
| `GET /api/v1/runs/{run_id}/actions/{action_id}` | Read own action receipt |

Action kind is `reserve` or `cancel`. Submission returns a pending receipt.
Settlement returns accepted or rejected with a reason. Identical requests return
the original receipt after time advances or a run ends. Changed payloads return
409. Credentials cannot select another owner. Participants cannot read the full
scenario, private trace, report, or peer balances. Only the Actor worker grants
or withdraws owner consent; there is no Agent endpoint that grants itself permission.

New board posts, owner messages, and world actions require a running environment.
Identical committed retries remain readable after a pause or completion. The
standalone message board retains its existing lifecycle and concurrency rules.

## Private internal mail

All routes below require the Agent credential for a participant in the run.
The server derives the sender and mailbox owner. An administrator uses the
observer routes to inspect mail and cannot send as an Agent.

| Method and path | Request or result |
| --- | --- |
| `GET /api/v1/runs/{run_id}/inbox` | `{items, next_cursor}`; query fields are `mailbox=inbox|sent|all`, `unread_only`, `query`, `limit`, and `cursor`. |
| `GET /api/v1/runs/{run_id}/inbox/{message_id}` | One received or sent message, with the recipient's explicit `read_at` receipt when present. |
| `POST /api/v1/runs/{run_id}/inbox` | Send `{request_id, recipient_owner_id, subject, body, in_reply_to?}`. Returns the committed message. |
| `POST /api/v1/runs/{run_id}/inbox/receipts` | Acknowledge `{message_ids: [...]}` from the owner's received inbox. |

Mail subjects contain 1 to 240 characters. Bodies contain 1 to 12,000 characters.
Both are plain text. There is one recipient from the same run. The sender fields
are immutable. A reply must reference an accessible message and address its other
participant. The service does not send external email.

The default page size is 20; the maximum is 100. Search text contains at most
200 characters. `unread_only=true` requires `mailbox=inbox`. The Agent adapter also
validates this combination before HTTP and reports query errors separately from
invalid email references. Reading does not
acknowledge mail. Receipts accept 1 to 100 IDs and validate the whole batch before
writing. Unread queries without a history cursor find unacknowledged late commits.
A history cursor is bound to the owner, run, mailbox, and search filters.

A send and its recipient-specific `inbox_received` event commit together.
Identical requests return the original message, including after pause or
completion. Changed content with the same request ID returns 409. Cross-run
recipients and inaccessible message IDs return 404. Invalid arguments return
422. Validation errors omit unsafe input values.

The model tools are `list_mail`, `read_mail`, `send_mail`, and
`acknowledge_mail`. The adapter derives write IDs from the saved operation
namespace and tool call ID. The model cannot supply a sender credential or role.

Mail IDs do not identify owner chat, board posts or notification events. A known
chat ID from this caller's own conversation returns 422 when used as a mail
reference. Inaccessible private IDs still return 404. A rejected mixed receipt
batch writes no receipts. Owner chat has no mail-acknowledgement requirement.

The local Agent completion tools also support explicit event waits. In sessions
created with `require_explicit_finish=True`, use `wait_for_mail(text=...)` for a
peer's next internal email, or `wait_for_owner(text=...)` for the paired owner's
private-chat reply. They return `waiting` with the appropriate `wait_for` event.
These tools have no external email or payment effect. First report a new deferral
once, then wait silently. Explicit `finished` results require actual body evidence
for notified mail. See [runtime repair scope](INBOX_WAIT_REPAIRS.md).

## Owner Resources and simulated commerce

These routes also derive the owner from the Agent credential.

| Method and path | Request or result |
| --- | --- |
| `GET /api/v1/runs/{run_id}/resources` | Own resources plus permitted shared and public records. |
| `GET /api/v1/runs/{run_id}/search?query=...` | Search permitted environment records and public listings; no web access. |
| `GET /api/v1/runs/{run_id}/resources/{resource_id}` | Fetch an accessible resource or public listing by its stable ID. |
| `POST /api/v1/runs/{run_id}/resources` | Create a private document from `{request_id, title, content}`. |
| `PUT /api/v1/runs/{run_id}/resources/{resource_id}` | Update delegated content from `{request_id, content, expected_version, title?}`. |
| `GET /api/v1/runs/{run_id}/market` | Own balance and commerce consent plus available public listings. |
| `POST /api/v1/runs/{run_id}/market/actions` | Submit a typed sell or buy request under owner consent. |
| `GET /api/v1/runs/{run_id}/market/actions/{request_id}` | Read the owner's market receipt. |

A resource has an owner, kind, visibility, version, content, and item quantity.
Visibility is private, shared with named owners, or public. A content write needs
the current version and `agent_writable=true`. An Agent can create private
documents but cannot create items, alter inventory, grant sharing, or edit private
peer records. Stale versions and conflicting retries return 409.

A sell request has `request_id`, `kind="sell"`, `resource_id`, `quantity`,
`unit_price`, and optional `expected_version`. A buy request has `request_id`,
`kind="buy"`, `listing_id`, `quantity`, and optional `expected_version`. A buyer
uses the listing's published price. The service checks the seller's item,
quantity limit, and price floor, and the buyer's permitted item and budget.

A sell request's `expected_version` is the exact item resource's `version`.
A buy request's `expected_version` is the exact listing's `version`. Do not use a
consent version, decision-log version, or global journal revision. The version
field is optional when the caller has not fetched the target.

A known stale version returns a saved final receipt immediately:
`status="rejected"`, `reason="stale_version"`, and
`validation_phase="submission"`. The response includes `current_version`,
`version_target_type`, and a `recovery` object with the exact `fetch_resource`
argument. No pending intent is created. After fetching that target, the Agent
can submit a corrected request in the same window with a new operation ID.
A stale version alone does not require a new owner grant when consent is valid.

Other accepted submissions return pending receipts. Settlement rechecks versions
and authority before transferring stock and credits atomically. A pending result
is not proof of a trade. Identical retries preserve the original saved result;
changed payloads with that ID still conflict. All goods and credits are simulated;
these endpoints do not make external purchases.

The model tools are `read_resources`, `search_environment`, `fetch_resource`,
`create_resource`, `write_resource`, `inspect_market`, `submit_market_action`,
and `read_market_action`. The Actor/Director path controls owner sharing and
consent. No Agent endpoint grants those permissions.

## Simulated credit transfers

Participant credentials select the payer; a model cannot supply another sender.
`GET /api/v1/runs/{run_id}/transfer-account` returns the caller's balance, exact
one-use authorizations, and sent/received payment receipts. It reveals no peer
balance. `POST /api/v1/runs/{run_id}/transfers` accepts `request_id`,
`authorization_id`, `recipient_owner_id`, positive integer `amount`, and `memo`.
The model tools are `inspect_transfer_account` and `transfer_credits`; the adapter
supplies the stable request ID.

Only the trusted owner-input path can issue the exact typed grant. There is no
Agent grant endpoint. The first task used operator-supplied owner input; the
Director also accepts typed Actor transfer consent as described below. A grant can fund one
payment only, including across different tool call IDs. Debit, credit, ledger,
world journal, and recipient bank notice commit atomically. An accepted receipt
means committed. Identical operation retries return their first result; changed
payloads conflict. New calls require an active run. See the
[task contract and evidence](TRANSFER_TASK.md).

## Actor requests and declared effects

Internal elapsed Director cues may carry the strict boolean
`owner_response_required`. The addressed private Actor observation retains it.
It declares communication of a current instruction, not spending authority or
a world effect. `ModelActor(..., require_current_cue_review=True)` adds independent
stimulus and exact coverage validation. The Clock also rejects silent delivery
of a declared instruction. This is not an Agent API capability; see
[the protocol and evidence](CUE_RESPONSE_REPAIR.md).

`ScenarioSpec.resources` declares initial resource records.
`ScenarioSpec.initial_mail` declares initial mail. A beat can contain `mail`
and `resource_effects` entries. Initialization and beat effects use the same
transaction as their saved Director state.

An accepted Actor output can contain `mail_requests`, `resource_requests`, and
`commerce_consent`. These are typed requests through the Director, not public
Agent API operations. The runtime supplies the Actor's owner identity. A resource
request can create or update an owned document or change its access. It cannot
mint inventory. Mail, resource changes, consent, and owner chat commit with the
accepted Actor turn. An invalid effect cannot leave a partial turn.

An Actor can also supply `transfer_consent={recipient_owner_id, amount, memo}`.
The accepted dialogue and exact grant commit together. Grant identities come
from saved Actor jobs. Self/unknown recipients, over-budget grants and repeated
authority are rejected. Actor views expose only their own account, grants and
sent/received receipts. Status replies preserve authority with
`transfer_consent=null`. See the [Actor task contract](TRANSFER_ACTOR_TASK.md).

## Local console access

Open `/console` or `/runs` directly on the loopback server. The browser has no
sign-in form, does not retain an administrator token, and sends no `Authorization`
header. This access applies to `/api/v1/observer` and `/api/v1/console` only.
Cluster, message board, and participant APIs retain their credential requirements.

Token-free access requires the configured server bind address and the direct
client address to be loopback. The request must use an accepted local Host.
A browser request must come from the same origin. Foreign-origin requests and
unaccepted Host values cannot gain local operator access. The standard server
entrypoint ignores forwarded peer headers.

An explicit credential always follows the normal credential and role checks.
An Agent token cannot gain operator access through a local connection. Backend
scripts, provisioning clients, and authenticated API clients can continue to use
`ADMIN_TOKEN` from the ignored `.env` file. Credentials remain outside URLs and
model prompts.

## Human run observer

Open `/runs` directly on the local server. This is a read-only operator interface.
The API base is `/api/v1/observer`, under the local access rules above.

| Method and path | Result |
| --- | --- |
| `GET /runs` | Saved runs, status, phase, decision window, and participant count. |
| `GET /runs/{run_id}` | Overview, private frozen plot, participants, current jobs, counts, report, and linked campaign summaries. |
| `GET /runs/{run_id}/events` | Timeline; optional `owner_id` and `kind` filters. |
| `GET /runs/{run_id}/records/{collection}` | Saved records; optional `owner_id` filter. |
| `GET /campaigns` | Durable campaign cost summaries. |
| `GET /campaigns/{campaign_id}` | Reported cost, outstanding and uncertain reservations, remaining allowance, and recent attempts. |

Collections are `chat`, `mail`, `resources`, `market`, `receipts`, `trades`,
`accounts`, `reservations`, `jobs`, and `board`. Lists accept `limit` from 1 to
100 and a bounded `offset`. Tool results use timeline events with kind
`assistant_tool_call`. Older runs can have tool evidence only in `trace.json`.

The observer can inspect all synthetic private records in a run. An Agent token
cannot access it. These views do not start workers, control runs, post messages,
or create participant actions. See the [visibility guide](ENVIRONMENT_EXTENSIONS.md#see-what-is-happening).

## Saved-turn replay API

Open `/console` for the Run console. Its replay routes use the same
`/api/v1/observer` base and local access rules as the data observer. A copied turn
link contains only the run ID and zero-based window number. `/runs` remains
available. See the [Run console guide](RUN_CONSOLE.md) for controls and examples.

| Method and path | Result |
| --- | --- |
| `GET /runs/{run_id}/turns` | Recorded window summaries, `current_window`, and `max_windows`. Each summary has phase, status, message/event counts, job counts, and `has_actor_view`. |
| `GET /runs/{run_id}/turns/{window}` | The selected window's delivered chat, mail, board messages, events, saved jobs, Actor perspectives, and pagination metadata. |

The turn list excludes future windows and the completion boundary after the
last configured window. It includes the current window even before it has
messages. Windows are zero-based integers from 0 to 99, and must be less than
the run's `max_windows`. An unknown run or an unrecorded window returns `404`.
Invalid parameters or a window outside the configured range return `422`.
Requests without accepted local access or credentials are rejected. An Agent
credential returns `403`. These are read-only GET routes. Responses use
`Cache-Control: no-store`.

Turn detail accepts `limit` from 1 to 100, default 100, and `offset` from 0 to
1,000,000, default 0. The same limit and offset apply separately to `chat`,
`mail`, `board`, and `events`. Each collection is ordered by creation time and
record ID. Follow `pagination.next_offset` until it is null. `complete` then
equals true. `pagination.collections` gives each collection's `total`,
`returned`, and `has_more` values. A page can contain 100 records from each
collection, rather than 100 records in total.

`jobs` and `perspectives` appear only at offset 0. Jobs are limited to 30,
which covers an Actor and Agent for each of 15 owners. Metadata reports
`jobs_included`, `jobs_total`, and `jobs_truncated`. Actor and Agent inputs use
field allowlists. Structured credential and internal hash fields are removed.
This filter does not establish that arbitrary saved prose contains no secrets.

`chat` comes from delivered owner messages. An Agent job's `output.text` is
activation evidence and is not substituted for missing delivered dialogue.
Board messages are assigned to their saved `board_posted` event's window.
Each perspective's `view` uses its saved Actor input. Its `resources` object has
`source: "actor_job_input"`, `view_label: "Start of Actor activation"`, and the
captured `snapshot`. Missing snapshots remain missing; current Resource state
and later Actor inputs are never used to fill an earlier turn. Saved truncation
flags remain present.

Each request reads a consistent database snapshot. An active window can gain
records between requests, so refresh it after the worker completes. Reading,
playing, filtering, and following turns do not change run state or call models.

### Optional English display sidecar

`GET /runs`, `GET /runs/{run_id}` and `GET /runs/{run_id}/turns/{window}`
accept `display=en`. Original response fields stay unchanged. An additional
`english` object contains `available`, `local: true`, and `texts`, a mapping
from the response's Chinese string values to approximate English translations.
It includes only strings in that authorized response, after the turn route's
secret-field filtering. Dictionary keys and raw records are not translated or
replaced. Calls without `display=en` retain the existing response shape.

Translation runs locally after the database read transaction ends. It does not
create model-provider attempts, spend campaign funds, or change simulation state.
A missing optional engine/model returns `available: false` and an empty mapping;
raw evidence remains available. CPU translation is cached privately on disk.
`GET /runs/{run_id}?compact=true` skips the full report query and returns
`report: null`. The default overview still includes its saved report.

## Live console control API

The base is `/api/v1/console`. Routes accept local operator access under the
rules above, or an administrator bearer token. Agent credentials cannot use
them. Responses are not cached.
The browser provides run IDs and typed control fields. It cannot provide a local
path, shell command, model override, credential, or replacement budget.

| Method and path | Result |
| --- | --- |
| `GET /runs/{run_id}/execution` | Run and execution status, control capabilities, next prompt window, campaign totals, and up to 100 recent interventions. |
| `POST /runs/{run_id}/execution` | Accept a `step` or `continue` operation. Return the execution state with status `202`. |
| `POST /runs/{run_id}/execution/pause` | Save an operator pause and return execution state. A current provider call is not cancelled. |
| `POST /runs/{run_id}/interventions` | Queue an Actor cue or Agent owner message. Return its saved receipt with status `201`, or `200` for the same request replay. |
| `GET /runs/{run_id}/boundaries` | Return available branch points and parent-run metadata. |
| `POST /runs/{run_id}/branches` | Create a paused child before a selected window. Return status `200` and the child identity. No model execution starts. |

All window values are zero-based. Displayed Turn 03 is window `2`. Every mutation
requires a UUID `request_id`. Retry the same payload with the same ID after an
unknown response. A changed payload with a reused ID returns `409`. Use a new ID
for a changed operation. Accepted changes are committed before success returns.

### Execution

The execution request body is:

```json
{"request_id":"11111111-1111-4111-8111-111111111111","mode":"step"}
```

`mode` is `step` or `continue`. Step completes the current Actor, Agent, and
settlement cycle, then pauses. Continue proceeds through subsequent windows.
Only one worker can execute a run. An active console or CLI worker blocks a
second start. Ended runs require a branch. Execution uses the fixed local
`credentials.json` for this run, its saved model settings, and its existing
campaign. Missing configuration or unavailable budget returns `409`.

The pause body contains only `request_id`. Pause saves the run's paused state
and flags active execution. A pending provider call can still finish and incur
cost. Its output is retained; delivery waits for explicit resume. An execution
request uses a new operation ID after a completed pause. An uncertain execution
request is retried with its original ID.

The state response contains:

- `run_id`, `status`, `phase`, `current_window`, and `max_windows`.
- `execution`, or null. The record contains `id`, `request_id`, `mode`, `status`,
  `pause_requested`, creation/start/finish times, and a safe `error` value.
- `capabilities`: `can_execute`, `can_pause`, `can_intervene`, and `reason`.
- `next_intervention_window`, or null when no unopened turn remains.
- `budget`, or null. It contains `campaign_id` and decimal strings for
  `limit_usd`, `spent_usd`, `reserved_usd`, and `available_usd`.
- `interventions`, the recent saved prompt receipts.

Execution states are `queued`, `running`, `paused`, `completed`, `failed`, or
`interrupted`. Run status and execution status are separate. A paused run can
have a completed execution attempt. Status polling does not select a different
historical turn in the browser.

### Prompts

The intervention body contains `request_id`, `owner_id`, `target`, `prompt`,
and `expected_window`. `target` is `actor` or `agent`. `prompt` is nonempty after
trimming and has at most 12,000 characters. `expected_window` is the run's current
window from the execution response, not the turn selected for historical viewing.

```json
{
  "request_id":"22222222-2222-4222-8222-222222222222",
  "owner_id":"owner-1",
  "target":"actor",
  "prompt":"Ask your assistant to explain another option before acting.",
  "expected_window":2
}
```

If the run advanced, the request returns `409`. If the current Actor phase or
boundary is already prepared, the service chooses the next available window.
The receipt reports the actual `effective_window`; it can differ from
`expected_window`. No available window returns `409`. An unknown owner returns
`404`. The receipt contains `id`, `request_id`, `owner_id`, `target`, `prompt`,
`effective_window`, `status`, `created_at`, and `applied_at`. Its status is
`pending` or `applied`.

At the effective window, an Actor target becomes a private `actor_cue` event.
An Agent target becomes a private owner message. Application and its event
commit together. Queueing does not start execution or produce an Agent reply.
Prompts do not rewrite frozen scenario facts, grant consent, create inventory,
or establish a world transaction. Existing typed actions retain those checks.

### Turn boundaries and branches

The boundary response contains `run_id`, `items`, `parent_run_id`, and
`parent_window`. Each item has `window`, `available`, `source`, and `reason`.
`source` is `checkpoint`, `initial`, or null. Availability describes whether a
complete saved boundary or an initial restart is possible. Branch creation also
checks the local manifest, campaign, frozen scenario, and snapshot integrity.

```json
{
  "request_id":"33333333-3333-4333-8333-333333333333",
  "window":2,
  "owner_id":"owner-1",
  "target":"agent",
  "prompt":"Before you proceed, explain the options to me."
}
```

Only `request_id` and `window` are required. `target` defaults to `actor`.
`prompt` defaults to empty and accepts up to 12,000 characters. A nonempty
prompt requires a valid `owner_id`. The response has `run_id` for the child,
`parent_run_id`, `window`, `source`, and `replayed`. `replayed: true` means the
same accepted branch request returned its existing child.

The boundary precedes the selected whole turn. A child receives new run, cluster,
Agent, session, and credential identities. Earlier Actor/Director state, chat,
board, mail, receipts, world, market, Resources, and completed Agent task and
graph state are copied to those identities. Console prompt receipts retain their
pending or applied state at the boundary; later prompts are excluded. Delta-backed graph values are
resolved, including private scratch files. A full boundary is required; missing
memory or older request rows without their input cannot be restored. An
unavailable historical boundary returns `409`. Earlier version-1 snapshots that
lack queue data first pass their original hash check. Their console prompt state
is recovered using the boundary creation time, including pending status for
prompts applied after that time. Window 0 can restart the frozen initial scenario. An out-of-range window returns `422`.

Saved `next_cursor` values in known paged tool messages from `list_threads`,
`read_thread`, and `list_mail` are cleared. Those results set
`branch_pagination_reset_required: true` with a note to restart the query.
Child identities and database sequences require paging from the first page.
Arbitrary document JSON is preserved; a document field named `next_cursor` does
not trigger a paging reset.

The original run remains available. A branch is paused and needs an explicit
execution request. An optional prompt is saved at the child's selected window.
Provider credentials, reports, model spending, and outstanding cost reservations
are not restored as world history. The child uses the same campaign and ceiling;
branching cannot reverse spend. Future model output can differ from the parent.

## Opt-in persistent transfer tasks

These participant routes derive the owner from the Agent credential:

| Method and path | Request or result |
|---|---|
| `GET /api/v1/runs/{run_id}/transfer-tasks` | `{items}`: at most 25 own records, open tasks first. At most 20 tasks can be open for an owner. |
| `POST /api/v1/runs/{run_id}/transfer-tasks/defer` | `{request_id, task_id, wait_for}`; event is `inbox_received` or `owner_message`. |
| `POST /api/v1/runs/{run_id}/transfer-tasks/complete` | `{request_id, task_id, receipt_id, recipient_notice_id, owner_report_id}`. |

There is no Agent route to open or cancel a task, enable tracking or grant
payment authority. The trusted Actor/Director path supplies typed owner task
requests and links exact transfer consent using its `task_key`. A task profile
must be enabled before a new run starts. New peer mail is information to review;
it does not authorize payment. Existing untracked runs keep their prior behavior.
An opened transfer task represents payment from this owner to its named
recipient. Monitoring money owed to this owner does not open an outgoing task.
The optional v9 ModelActor review gates this distinction before Director effects;
it does not infer payment permission from text or extend the Agent API.

Completion validates the task's actual committed payment and matching owner,
recipient and message sources/timing. It does not judge the meaning of message
text. Cancelling an unpaid task blocks its unused authorization; committed
payments cannot be cancelled through this path. Identical writes replay their
saved receipt, changed payloads conflict, foreign task IDs return 404, and invalid
evidence returns 403. Task tools are opt-in through `EnvironmentTools` and are
bound to the runtime session. The completion route returns the safe codes
`invalid_task_receipt` or `missing_task_notice_or_report` for invalid evidence.
The tool adapter maps only these codes to local corrective guidance: use the
actual payment receipt and IDs returned by the sent recipient notice and owner
report. The owner's instruction ID is not an assistant report. Raw response
bodies are not forwarded to the model. See [local evidence](TRANSFER_TASK_LIFECYCLE.md)
and [real-model results](TRANSFER_TASK_MODEL_RESULTS.md).

## Opt-in local browser shop

See [the browser runtime](BROWSER_SHOP_RUNTIME.md). Migration 015 stores
owner-scoped tasks, carts, orders and stable operations over the existing market.
`ShopService.enable` opts in a new pending run. Actor `shopping_task_requests`
open or cancel their own tasks; commerce consent remains separate.

| Method | Route | Access and effect |
| --- | --- | --- |
| GET | `/shop` | Credential-free shell; data still needs the appropriate scoped API. |
| GET | `/api/v1/runs/{run_id}/shop/state` | Agent: its own tasks, wallet, cart, orders and permitted public catalog. |
| POST | `/api/v1/runs/{run_id}/shop/cart` | Agent: select its task or set a listing quantity. |
| POST | `/api/v1/runs/{run_id}/shop/checkout` | Agent: submit one cart version as pending market intents and order lines atomically. |
| GET | `/api/v1/runs/{run_id}/shop/operations/{operation_id}` | Agent: its own committed operation for browser recovery. |
| GET | `/api/v1/observer/runs/{run_id}/shop?owner=alice` | Operator: read-only selected owner view; explicit Agent credentials cannot use it. |

Mutations require `X-Shop-Operation-ID: <uuid>` and can bind the browser action
with `X-Shop-Action-Hash: <64 lowercase hex characters>`. Changed payloads or
action hashes under the same ID conflict. Replaying a committed operation works
after a pause. A new ID with an already submitted cart version returns its saved
order; it cannot purchase that cart again. Checkout takes `{"cart_version": 1}`.
Cart input takes `action: select_task | set_quantity`, an own `task_id`, and for
quantity changes a public `listing_id` and integer `quantity` from 0 through 100.
A zero quantity removes the line. A task or page text cannot create authority.

The browser adapter supplies transport headers; model arguments and page scripts
never contain credentials. Browser orders, pending IDs, statuses and paid totals
come from committed market records. The observer route has no write equivalent.

## Explicit confirmation profile

These routes use the Agent's existing credential and native run membership.
They serve frozen elapsed-Clock contracts; no peer private chat is exposed.

| Method | Path | Effect |
| --- | --- | --- |
| GET | `/api/v1/runs/{run_id}/confirmations` | Own outgoing contracts and received incoming contracts. |
| POST | `/api/v1/runs/{run_id}/confirmations/request` | Send complete terms and condition once. |
| POST | `/api/v1/runs/{run_id}/confirmations/ask-owner` | Deliver one private decision question. |
| POST | `/api/v1/runs/{run_id}/confirmations/reply` | Send the peer owner's committed decision as an actual reply. |
| POST | `/api/v1/runs/{run_id}/confirmations/report` | Deliver the evidence-backed owner outcome. |

Writes take `task_id` and `request_id` UUIDs and require the matching released
Clock tool call. Identity, terms, decision and mail bodies come from native
scoped records, rather than model-supplied write arguments. Reusing an operation
ID with changed content conflicts. Repeated task calls reuse existing mail,
questions and reports even under new call IDs.

Reporting a requester's outcome requires an actual peer reply, body access through
`read_mail`, and its explicit Inbox receipt. A read receipt cannot create approval.
This dedicated profile blocks ordinary peer mail and free-form owner reports;
API and graph feedback direct the Agent to the confirmation tools.
Other runs keep their existing tools and behavior. See
[confirmation closeout](CONFIRMATION_CLOSEOUT.md) for scope and evidence.
