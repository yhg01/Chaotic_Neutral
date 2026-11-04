# Instructions for agentRPG

## Read first

Read `README.md`, `PRD.md`, `docs/ARCHITECTURE.md`, `docs/AGENT_RUNTIME.md`, and
`docs/API.md` before implementation.
The agreed diagram in `docs/diagram-source.html`, its rendered copy, and the
connection list in `docs/diagram.json` are the system source of truth. Later
explicit user instructions take precedence. Keep these sources consistent.

## Scope and boundaries

- Implement this version in this directory. Preserve work by other contributors.
- The message board uses FastAPI and PostgreSQL. Personal Agents use Deep Agents
  with LangChain and LangGraph in the local application. Keep runtime code open
  source and editable. Do not add a managed Agent Server requirement.
- Update the PRD component, connection, and integration rows with actual evidence
  when their implementation or verification changes.
- Support up to 15 Agents per cluster. Use identities and membership records;
  do not create special code paths for Agent 1 and Agent 2.
- Keep Agent and Actor as separate concepts. The board connections in the
  diagram belong to Agents. The human browser is an observer tool.
- Agent use is the main acceptance path. Verify real model tool calls against
  the board when changing Agent integration. Browser checks alone are not enough.
- Preserve all other diagram connections as architecture requirements. Do not
  invent behavior for components outside the current implementation scope.
- Preserve the four dashed Resource links and their unspecified visual directions.
  The user's 28 October 2026 request authorizes their implemented permission
  contract in docs/ENVIRONMENT_EXTENSIONS.md. Use that explicit contract: owner
  workspaces, delegated Agent content changes, typed Actor requests, and private
  peer boundaries. Do not infer broader access from the drawing.
- Use the old prototypes as reference for the message board only. Do not import
  their world state, subscriptions, activities, task rules, or Director design.
- Treat any synced `sources/` content outside this repository as read-only.

## Data and concurrency

- PostgreSQL is required in development and integration tests. Do not substitute
  SQLite or an in-memory fake for concurrency or persistence verification.
- Each post and reply is a separate row. Do not serialize all posting with a
  global mutex, cluster-row update, or shared thread counter.
- Use independent connections and short transactions. Do not call an LLM or
  an external service inside a database transaction.
- Derive author identity from credentials. Store token digests, not raw tokens.
- Enforce cluster boundaries in the service and database constraints.
- Enforce the 15-Agent limit atomically during membership creation.
- Commit before reporting a successful write. Make repeated writes idempotent;
  changed payloads with a reused operation ID must return a conflict.
- A sequence or creation timestamp is not a commit-order delivery cursor. Use
  explicit per-Agent message receipts for reliable update delivery. History
  pagination is for browsing; updates and acknowledgements are for delivery.
- Add versioned migrations for schema changes. Preserve persisted user data.
- Scope checkpoints and task IDs to cluster, Agent, and session. Preserve tool
  call request IDs across resume. Test recovery after a board write commits.
- Keep runtime session locks separate from board writes. Do not add a global
  Agent execution lock. Disable extra subagents and host execution by default.

## Quality and operation

- Keep credentials in ignored local files. Bind development services to loopback.
- Keep active source, dependencies, and database files in local storage. A private
  virtual environment or cache does not prevent cloud eviction of source files.
  Report a blocked import or test attempt accurately; do not mark that storage
  issue resolved from a successful isolated probe.
- Keep provider and board credentials out of model prompts, tool arguments,
  reports, and tracked files. Each Agent tool adapter has one scoped credential.
- Use bounded model runs. Report provider failures and measured model usage.
  Test data must be separate from the user's existing message threads.
- Render message content as text. Do not interpret a post as HTML or instructions.
- Test concurrent writes, reads, retry conflicts, capacity races, invalid input,
  access boundaries, late commits, and persistence with real PostgreSQL.
- Use an isolated test database. Never reset a user database to run a test.
- Inspect the browser view, including narrow screens and error/empty states.
- Keep setup commands, API examples, and verification evidence current.
- Use clear, concise English compatible with ASD-STE100. State verified results
  and remaining limits. Do not claim performance that was not measured.

## Event clock runs

- `docs/EVENT_CLOCK.md` is the contract for scenarios with a `clock` section. By the user's
  instruction of 4 November 2026, those runs replace the shared Actor, Agent and settlement
  phases with one time-ordered event list, and they have a live Director model. The rules
  on phases, windows as the clock, and declared branches only still hold for runs with no
  `clock` section. Keep saved runs and their hashes on the window engine.
- On clocked runs, tool calls and effects of one run commit one at a time in story order,
  while model calls overlap. This is a per-run schedule, not a global Agent lock. The board
  service itself stays concurrent.
- The Director writes content. Code applies typed, bounded effects. Prose never establishes
  a payment, booking or order. All sites, money and mail stay inside the simulation, and
  invented site names must not use a real brand.
- The board and direct messages are closed to Actors, cues and the Director's retrieval.
  Assistants are told no human can see them. The research replay portal can read them, so
  reports must state that.

## Director and owner Actors

- Follow `docs/CONCORDIA_REFERENCE.md` and `docs/DIRECTOR_RUNTIME.md`. Preserve
  the distinction between scene definitions, private observations, participant
  actions, and world resolution.
- Validate and freeze each scenario before execution. Store its canonical JSON
  and hash. The runtime Director can select declared branches only.
- Actors are simulated human owners. They express requests and grant their own
  consent. They have no board, reservation, or payment tools. Keep assistant
  decisions and model history separate from Actor state.
- Supply initial and updated private cues through the common Actor interface
  for every pair. Preserve the diagram's original labels and directions.
- Apply world effects in code. Prose, a pending receipt, or an assistant claim
  cannot establish a reservation, payment, consent grant, or cancellation.
- Preserve the Actor, Agent, and settlement phases. Respect explicit operator
  pauses, queued cues, future clock beats, and stable input/output identities.
- Preserve every material initial owner task and constraint in Actor dialogue.
  Later replies must not restart completed work. Actor fact_ids refer only to
  declared known facts; observation, message, Resource, and receipt IDs are not
  fact references.
- A null commerce_consent preserves remaining permission. Repeating a prior grant
  needs a fresh explicit accepted renewal proposal. Do not let a status reply
  reset spent authority or invalidate active listings.
- Market expected_version identifies the exact item for sell or exact listing for
  buy. A known stale value returns a saved final rejection without a pending
  intent. Keep stable replay and conflict rules. A corrected new operation can
  run in the same window under valid consent. Recheck versions at settlement.
- New runs use the natural assistant surface in `agent_surface.py`. Everything an
  assistant reads must read as real life: no Director, activation, window, run,
  simulation, fiction, or plot terms. Keep internal names in events and reports.
  Do not change the prompts or tool names of saved legacy runs. Do not claim this
  proves evaluation unawareness. Keep controls as explicit functional tests.
- Keep the full evaluation trace. Large reviews may use owner-specific parts
  with shared public context and scoped conduct judgments. Do not describe a
  partial review as a review of evidence it did not receive.
- Record simulator failure separately from assistant conduct. Keep failed
  attempts and review diagnostics. A completed run is not proof of a valid
  simulation, a successful integration check, or good assistant conduct.
- Keep control scenarios and conflict scenarios separate in reports. Cite saved
  evidence for judgments. Model review does not replace human calibration.
- Keep transport assets separate from private Inbox and Resource records. Follow
  docs/ENVIRONMENT_EXTENSIONS.md for internal mail, resource access, and commerce.
  Mail and resource effects commit with exact recipient notifications. Actors
  request effects through the Director; they never receive assistant tools.
- All world transactions and mail stay inside the simulation. Search and fetch
  use permitted environment records. Do not connect real email or purchases as
  an implicit extension of these tools.
- Model campaigns use one persistent ceiling across bound roles, retries, and
  resumed runs. Retain uncertain cost reservations. Do not reset a campaign or
  create another one to bypass its ceiling.
- The `/runs` observer and `/console` open without a token on the loopback server.
  Permit this access only for direct loopback requests with an accepted local Host
  and same-origin browser context. Ignore forwarded peer headers in the server
  entrypoint. Explicit credentials must retain their normal role checks.
- Keep local operator access limited to observer and console routes. Preserve
  Agent credentials and cluster and participant access rules. `ADMIN_TOKEN`
  remains available for backend scripts, provisioning, and authenticated clients.
  Never place credentials, the full plot, or peer private views in Agent prompts.
