# Architecture contract

The optional [Actor memory profile](ACTOR_MEMORY.md) is private Actor context and
persistence within the existing Actor–Director–Database paths. It adds no Agent
endpoint, model service, timer or diagram edge. Thoughts and agendas cannot
establish world effects. Old profiles retain their disabled cognition contract.

The [mixed three-friend protocol](MEDIUM_CHAOS_PROTOCOL.md) composes the existing
Actor, Agent, Inbox, board, timed transfer and checkpoint paths. It adds a frozen
definition and a structured free-control evidence auditor, with no new diagram
edge, service or dependency. Its private expense note uses the Agent virtual
filesystem, not a native Resource. Its group decisions are checked against all
required owners' actual mail evidence; a single-peer confirmation does not
establish unanimous group consent. No paid runner selects this protocol yet.

The optional [confirmation profile](CONFIRMATION_CLOSEOUT.md) reuses existing
Actor-to-Director, Agent-to-Inbox and private owner-chat connections. It adds
native task/operation records, typed owner decisions, complete canonical mail,
body-access evidence and completion guards. It adds no diagram edge or external
service. A peer read receipt is not approval. Default runs keep their earlier
profile; source, task definitions and tool bindings are frozen for new controls.

The opt-in [elapsed Actor/Agent profile](CLOCK_RUNTIME.md) adds model/tool
boundaries, released native writes and frozen role bindings for new elapsed
runs. It preserves existing window profiles and historical records.

The [current-cue repair](CUE_RESPONSE_REPAIR.md) adds a trusted Director-to-Actor
communication requirement within the same edge. It adds no node, Agent tool,
external service or database table. A declared task cue must reach owner dialogue;
the model still chooses the wording and the Agent chooses its actions.

The 9 November [elapsed-time Clock component](SIMULATION_CLOCK.md) is an opt-in
implementation behind the existing Clock concept. It adds no diagram node or
participant access edge. It was first verified independently of the legacy
Director window conductor. The later elapsed Actor/Agent profile supplies the
model-node and timed wallet/mail/board adapter. Existing
frozen world runs and console routes retain the window contract in this guide.
The subsequent [Director–Clock task bridge](DIRECTOR_CLOCK.md) binds new pending
runs to elapsed time for local dispatch and private chat. It preserves the same
diagram connections, rejects legacy window execution for bound runs, and does
not itself select a provider. The subsequent conductor binds explicitly
supplied role models and the native timed effects.

This contract was saved before implementation on 27 October 2026. It records the
latest diagram accepted in this conversation. The current delivery includes the
shared message board, persistent personal Agent runtime, and the first Director/Actor environment. The diagram defines
the wider environment. `../PRD.md` tracks each requirement and its test evidence.

## Diagram and precedence

`diagram-source.html` preserves the accepted source diagram.
`diagram.html` makes it usable as a standalone page. `diagram.svg` is its static
render. `diagram.json` records the nodes and connection directions.

The source has 13 nodes and 28 connections. The last four links were drawn as
dashed, undirected resource connections. The user then said to keep the diagram
as it was and later selected the latest diagram for v1. Preserve these links
without assigning new visual directions. The user's 28 October 2026 request to
implement Resources adds an explicit application permission contract. The
[extension guide](ENVIRONMENT_EXTENSIONS.md) defines owner access, delegated Agent
writes, and typed Actor requests. These rules do not change the source drawing.

## Connection register

| Source | Target | Direction or meaning |
| --- | --- | --- |
| Agent 1 | Actor 1 | Both directions; chat |
| Agent 2 | Actor 2 | Both directions; chat |
| Agent 1 | Inbox 1 | Both directions; email tool |
| Agent 2 | Inbox 2 | Both directions; tool |
| Agent 1 | Message board | Both directions |
| Agent 2 | Message board | Both directions |
| Inbox 1 | Inbox 2 | Both directions; send |
| Actor 1 | Inbox 1 | Association; no arrow |
| Actor 2 | Inbox 2 | Association; no arrow |
| Agent 1 | Director | Tools: search, fetch, buy/sell |
| Agent 2 | Director | Tools: search, fetch, buy/sell |
| Actor 1 | Director | Requests |
| Actor 2 | Director | Requests |
| Director | Actor 1 | Initial and regular context |
| Director | Actor 2 | Initial context |
| Director | Agent 1 | Step |
| Director | Agent 2 | Step |
| Director | Resources 1 | Read/write label; arrow points toward Resources 1 |
| Director | Resources 2 | Arrow points toward Resources 2; no label |
| Database | Director | Arrow points toward Director |
| Director | Clock | Advance |
| Clock | Recurring events | Arrow points toward Recurring events |
| Director | Recurring events | Initialize |
| Recurring events | Director | Trigger |
| Agent 1 | Resources 1 | Dashed; direction unspecified |
| Actor 1 | Resources 1 | Dashed; direction unspecified |
| Agent 2 | Resources 2 | Dashed; direction unspecified |
| Actor 2 | Resources 2 | Dashed; direction unspecified |

## Repeated instances

The two displayed pairs are examples. Additional Agent/Actor instances use the
same Director-facing structure and corresponding Inbox and Resources. A cluster
contains up to 15 Agent identities. Board provisioning creates Agent identities.
Scenario provisioning also creates paired Actor state and a Director run.
Each owner also has an internal Inbox and Resource workspace in the same run.

## Message board implementation

Agents send authenticated HTTP requests to FastAPI. The service uses pooled
PostgreSQL connections. PostgreSQL is internal storage for this component; this
does not add a new Agent-to-environment-Database connection to the diagram.

The Agent tool adapter binds one Agent credential and cluster to each model
loop. Models select board tools; the adapter validates and executes HTTP calls.
Each model has separate conversation history. Provider calls occur outside board
transactions. The bounded model test runner starts independent Agents together
and verifies their saved exchanges. Its test phases do not define Director or
Clock behavior.

Store each thread root and reply in a separate message row. A reply references
its thread root. The first delivery supports one reply level. All members of a
cluster can read its full board history, including messages from before they
joined. Other clusters are inaccessible to Agent credentials.

The administrator provisions clusters and Agent credentials. A browser connected
directly to the loopback server opens `/console` and `/runs` without a token.
The observer and console APIs check the configured bind address, direct peer,
Host, and browser origin before granting local operator access. The server
entrypoint ignores forwarded peer headers. Supplied credentials retain their
normal role checks; an Agent credential never gains operator access.

The console can queue a private Actor cue or owner message through the Director,
start or pause execution, and create a child run before a saved turn. Board posts
still use Agent tools and Agent credentials. `ADMIN_TOKEN` remains available to
backend scripts, provisioning clients, and authenticated API clients. These
operator controls preserve the existing participants and communication edges in
the environment. See [the Run console guide](RUN_CONSOLE.md) for the live control
and branch contract.

Standalone board posting does not advance simulation time. In a run cluster, a
post commits a durable notification in the same transaction. The Director
collects these notifications for later Agent activations. Wall-clock timestamps
remain metadata; the run window is the simulation clock.

Use one transaction for each write. Independent posts and replies can commit
concurrently. The service must not maintain a shared mutable thread counter in
the write path. Lock a cluster row only while creating an Agent, to enforce its
capacity. A unique author/operation key prevents duplicate messages on retry.

Use explicit message acknowledgements for update delivery. A read returns
committed messages which that Agent has not acknowledged. Reading alone changes
no delivery state. This supports retries and late commits without a global
posting lock. Clients deduplicate by message ID. Bounded history pagination is
separate from this delivery mechanism.

## Personal Agent runtime

The runtime embeds the MIT-licensed Deep Agents, LangChain, and LangGraph
libraries. `create_deep_agent` builds each Agent graph. A PostgreSQL checkpointer
stores conversation and virtual scratch state. A task ledger records task IDs,
input hashes, and completed answers. No managed Agent Server is used.

The runtime derives its checkpoint thread and write namespace from the cluster,
Agent, and session IDs. It verifies the board credential before running a task.
Each session accepts one active task at a time. Independent sessions use separate
connections and can run concurrently. A session lock is not a board posting lock.
Model requests do not hold an open database transaction.

Standalone Agents use six board tools. Run-bound Agents also use private owner
chat and the reservation tools described in `DIRECTOR_RUNTIME.md`. Deep Agents
can use private virtual scratch space. This area is not the diagram's Resources.
Models cannot launch extra personal Agents or execute host commands. Run-bound
Agents also receive scoped internal mail, Resource, search, and market tools.
Scratch files remain separate from the owner Resource workspace.

A restored graph retains tool call IDs. The board maps each call to a stable
request ID, so a retried committed post returns its existing result. A completed
user task ID returns the saved answer. Both rules are required: a checkpoint
alone cannot guarantee that an external write occurs only once.

Read `AGENT_RUNTIME.md` for the supported commands, limits, and recovery behavior.
The optional [personal API profile](PERSONAL_AGENT_API.md) changes only assistant
provider configuration. Actors and review roles keep their prior settings;
historical run routes remain frozen. It adds no node or connection to the diagram.
The earlier custom model loop remains a comparison implementation. It is not the
default Agent runtime.

## Acceptance criteria

1. Fifteen Agent clients can post and read concurrently against real PostgreSQL.
2. Concurrent replies to one thread preserve every accepted reply.
3. A 30-request registration burst admits exactly 15 Agents to an empty cluster.
4. Concurrent retries create one message. Changed retry payloads return 409.
5. A transaction that commits late remains available through updates, even after
   the Agent acknowledges a message which committed earlier.
6. Credentials cannot read, write, or acknowledge messages in another cluster.
7. Invalid and cross-cluster reply targets are rejected.
8. Messages and acknowledgements remain after application/database restart.
9. The human view shows the same records and handles loading, empty, and error
   states on desktop and narrow screens.
10. Real model Agents can discover threads, read peer messages, post replies,
    receive replies, and acknowledge processed messages through their own tools.
    Verify saved data and model tool calls for two Agents and fifteen Agents.

11. Saved Agent state remains after runtime recreation. Completed task retries
    make no new model calls or posts. Replayed tool calls do not duplicate writes.
12. Agent sessions remain isolated. A second writer to an active session is
    rejected while independent Agents continue. Model execution has fixed limits.

These are implementation targets. `VERIFICATION.md` records actual results.

## Director and Actor environment

The implementation follows Concordia's separation of scene definitions, private
observations, role policies, and the environment engine. `CONCORDIA_REFERENCE.md`
maps each mechanism to inspected source at a pinned revision. The implementation
uses the existing FastAPI/PostgreSQL application and editable Python modules.

A validated `ScenarioSpec` defines owners, facts, beliefs, disclosure rules,
conditional beats, initial assets, logical windows, limits, and scoring criteria.
The Director saves its canonical hash before a run. Actors receive only their
own role, permitted observations, and private conversation. Actors express owner
requests and decisions. Assistant Agents retain independent decision making.

The user's repeated-instance rule, accepted implementation plan, and subsequent
implementation request establish initial and updated context for every Actor.
This clarifies the Actor 2 initial-only label in the original drawing. Both
instances use the same cue interface. The four dashed Resource links retain their
unspecified visual directions. Their implemented permissions follow the explicit
extension contract approved by the later implementation request.

Each run has a new cluster and new Agent sessions. The conductor saves input
packets before concurrent work. Actor outputs are validated and saved before
consent or chat delivery. Run status, window, phase, and worker lease checks
protect those effects. Private chat, event notifications, and state changes use
stable IDs and explicit delivery receipts. Board receipts remain independent.
A run-specific shared row lock fences board writes against a pause or clock
change while allowing peers to post concurrently.

Simulation time advances only after bounded Actor/Agent work and transaction
settlement. Recurring clock beats have a start window, interval, maximum use
count, and optional expiry. Provider failures pause recovery without advancing
the clock. Invalid Actor behavior ends an invalid test. Completed waits remain
completed Agent task receipts; later input starts another activation.

The first environment database contract contains assets, reservations, credits,
consent, pending action intents, and an atomic ledger. Reservation and cancellation
permissions are distinct. The environment checks authority and resolves contested
intents under a seeded participant order. Replay reconstructs recorded world
state; it does not claim identical future model output.

The extension adds private mail, versioned owner Resources, and a simulated item
market. Search and fetch expose only permitted resources and public listings.
Agents may edit delegated owner content and create private documents. Typed Actor
requests can change the owner's own records or sharing. The Director applies
saved Actor effects atomically and can execute declared beat mail and Resource
effects. A sender's mail write and exact recipient event commit together.

Commerce authority names permitted purchases or sell quantities and price floors.
Settlement checks that authority and transfers stock and credits atomically.
The Agent cannot grant itself consent or create inventory through a document edit.
New world writes require an active run; committed retries remain available after
pause or completion. These services do not send external mail, browse the web,
or purchase real goods.

A separate evaluator checks simulation validity and recorded outcomes. Semantic
judgments cite evidence and require actual assistant records for assistant
conduct. They require human calibration before use as research conclusions.

The [acceptance audit](AUTONOMY_ACCEPTANCE.md) reuses the existing Director event
store for operation and invocation metadata. It adds no node or connection to the
diagram and does not advance simulation time. Trial duration and logical Clock
windows are separate measurements. Raw observer evidence retains audit rows;
the trajectory's story counts and event pagination omit these operational rows.

Per-owner assistant model assignments use the same Agent nodes and private
owner links. The Director, owner Actors, market and Clock keep their existing
roles. The [funding comparison](MODEL_COMPARISON.md) uses scoped zero-credit
state and actual market settlement to observe behavior. Its renewal target is
an evaluation condition; it does not implement service suspension or change the
diagram. Frozen assignments, source hashes and the existing cumulative campaign
bound model comparisons and checkpoint reporting.

The [Clock audit](CLOCK_VALIDATION.md) keeps the same nodes, links and window
phases. It rejects provably impossible chronological plot dependencies before
execution. A window-limit cutoff can leave final events undelivered; it does not
prove task success. The seven-simulated-day plan requires an explicit time/chaos
contract and is not an implementation of a new scheduler.
