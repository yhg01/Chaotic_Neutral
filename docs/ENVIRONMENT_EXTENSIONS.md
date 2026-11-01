# Inboxes, Resources, and environment transactions

The environment now includes private internal mail, owner workspaces, resource
search, and a market for simulated items. The same code serves each pair in a
run with up to 15 Agents. These operations use the existing FastAPI service and
PostgreSQL database.

The user asked to implement these parts on 28 October 2026. That request defines
a new implementation contract for the four dashed Resource associations. The
source diagram still has 13 nodes and 28 connections. Its dashed links retain
their original appearance and unspecified visual directions. The permissions
below are explicit application rules; they do not change those arrows or labels.

## Access and authority

| Participant | Mail | Resources | Transactions |
| --- | --- | --- | --- |
| Personal Agent | Read its owner's received and sent mail; send to owners in the same run; reply; explicitly mark received mail as read. | Read its owner's records and public or explicitly shared records. Create private documents. Update its owner's records only when `agent_writable` permits it. | Submit transport or market actions under recorded owner consent. Read its own receipts. |
| Owner Actor | Receive a bounded view of its own inbox. Ask the Director to send mail with its own sender identity. | Receive its private resource view. Submit typed requests to create or update documents or change access to its own records. | Grant or withdraw its own transport and commerce authority. It has no transaction tools. |
| Director | Seed declared mail and apply declared beat mail. Save accepted Actor mail requests with the Actor turn. | Initialize records and apply declared resource effects or accepted Actor requests. | Validate requests and settle actions in code. Narrative text cannot establish an effect. |
| Human observer | Inspect all synthetic run records through the administrator view. | Inspect private and shared records for verification. | Inspect submissions, receipts, stock, balances, and cost records. |

Agent identity comes from its credential. Models cannot choose another owner or
sender. An Agent cannot grant itself spending permission or change resource
sharing. Private peer records remain inaccessible. An observer view must not be
copied into an Agent prompt because it contains information that peers cannot see.

## Mail behavior

Mail uses plain text, stable message IDs, and one recipient per message. A reply
must reference a message in the sender's own mailbox and address its other
participant. Director notices have no owner reply address. Send a separate
message to a run participant when a response is needed. Sender attribution
records whether the source was an Agent, Actor, or Director. No real email is sent.

A committed send also saves an `inbox_received` event for its exact recipient.
Identical retries return the same message. A changed payload with the same
request ID returns a conflict. New mail requires a running environment;
committed retries remain available after pause or completion.

Reading does not mark mail as read. The Agent must explicitly acknowledge a
received message after processing it. The sender can then see the read receipt.
Unread queries use these receipts, so a late transaction is not skipped by a
sequence cursor. History pages use a separate bounded cursor.

Actor inbox observations contain at most 20 messages and 24,000 serialized
characters. Each body excerpt contains at most 4,000 characters. Shortened
bodies carry `body_truncated=true`. This affects the Actor observation only;
the Agent read API and stored mail retain the complete body.

## Resource and market behavior

Resources belong to a run and owner. A record is a document or an item with an
integer quantity. Visibility is `private`, `shared` with named owners, or
`public`. Initial records declare whether the assistant may change their content.
An Agent's newly created documents are private. Agents cannot create item stock,
change quantities through document edits, or grant access to a private record.

Writes require the current resource version. A stale version returns a conflict.
Stable request IDs protect committed retries. Resource search and fetch operate
on permitted simulation records and public listings. They do not browse the web
or retrieve host files. Agent scratch files remain separate from these resources.

A seller needs explicit authority for the item, maximum quantity, and minimum
unit price. A buyer needs an approved item and spending budget. A sell request
creates a listing only after settlement. A buy request uses a real listing.
A buy grant limits item IDs and the remaining credit budget. It has no separate
unit limit. An instruction to buy exactly one unit remains an Agent instruction.
The functional control checks that result against the transfer ledger. Transport
and commerce grants each have their own allowance. They share the same wallet;
`max_spend` limits each grant, rather than all spending across renewed grants.
Settlement checks authority, stock, price, versions, and available credits.
It transfers stock and credits atomically. An Actor uses
`commerce_consent=null` to preserve remaining authority during a status reply.
A repeated grant requires a fresh explicit accepted renewal proposal. It must
not silently restore a spent budget or invalidate an active listing. An empty
consent object withdraws authority. A pending receipt proves submission
only. It does not prove a sale or purchase.

For a sell request, `expected_version` refers to the exact item resource. For a
buy request, it refers to the exact listing. Consent, log, and global versions
are different counters. If the supplied version is already stale, submission
returns a saved rejected receipt with the current version and exact fetch target.
It creates no pending intent. The Agent can fetch the target and submit a new
operation ID in the same window. Existing valid consent remains sufficient.
Settlement still rechecks the version if state changes after submission.

All credits and purchases are simulated. No real goods are ordered or paid for.
Provider model calls are the only paid external activity in the test campaign.

## Run the storm plot with a shared ceiling

[The plot guide](STORM_PLOT.md) explains the two-owner conflict. The fixed JSON
file is `scenarios/community_storm_exchange.json`. Start PostgreSQL and the API,
then run from the repository:

```sh
CAMPAIGN_ID=$(uv run python -c 'from uuid import uuid4; print(uuid4())')
uv run python scripts/run_scenario.py \
  --scenario scenarios/community_storm_exchange.json \
  --actors model --model openai/gpt-4.1 --actor-model openai/gpt-4.1 \
  --campaign "$CAMPAIGN_ID" --budget-usd 30 --semantic-review
```

Keep the same campaign ID when several runs share one ceiling. A new campaign ID
creates a separate budget. The 30 USD value is a maximum, not a target spend.
The run manifest saves the campaign binding. Resume from that manifest to retain
its ceiling, model choices, identities, sessions, and completed work:

```sh
uv run python scripts/run_scenario.py \
  --resume .local/scenario-runs/RUN_ID/credentials.json
```

The campaign records all participating model roles, including assistants,
Actors, response reviewers, and the optional final evaluator. The author command
can use the same `--campaign` and `--budget-usd` flags to include authoring,
repairs, and critique in that ledger. Calls without a campaign binding are outside
its ceiling. Models without supported request formats and verified prices cannot
start a budgeted request. Provider attempts
and retries reserve cost before a request is sent. Concurrent attempts share the
same durable ledger. A reservation is released or replaced only when the result
supports that accounting. An uncertain attempt retains its full reservation.
The system stops new calls when it cannot reserve their maximum cost. Resume
does not reset spending. The ceiling applies only to calls bound to the campaign.

## See what is happening

Open <http://127.0.0.1:8000/runs> after the local API starts. No token or sign-in
is needed when the browser connects directly to the loopback server. Select the
run printed by the scenario command. Use refresh or the page's automatic refresh
control while the worker runs. The observer reads records; opening it does not
start model work. The [local access contract](API.md#local-console-access) keeps
Agent credentials and participant permissions separate from this operator view.

| View or record | What to look for |
| --- | --- |
| Run overview | Status, decision window, phase, active jobs, and saved failures. |
| Timeline | Director cues, mail notifications, resource changes, owner decisions, and settlement results in their recorded order. |
| Owner chat | What each Actor requested, what its assistant proposed, and what the owner approved or rejected. |
| Board | Public posts and actual replies to another assistant's thread. |
| Mail | Sender, recipient, private body, reply reference, and explicit read receipts. |
| Resources | Current private, shared, and public records, quantities, permissions, and versions. |
| Market and transaction records | Listings, pending or final receipts, stock transfers, and balances. |
| Jobs and tool evidence | Saved inputs, Actor outputs, assistant results, and recorded tool calls. |
| Plot | Frozen scenario, private facts, beats, and scenario hash. |
| Report | Saved acceptance checks and results for successful and failed runs. |
| Campaign cost | Reported cost, outstanding reservations, uncertain cost, remaining allowance, and attempts by role. |

A run marked `completed` can still contain a failed acceptance check or an
unresolved owner request. Read the receipts and report before treating a claim as
a completed action. Model review is separate from exact database checks.

The runner saves private evidence under `.local/scenario-runs/RUN_ID/`:

| File | Purpose |
| --- | --- |
| `scenario.json` | The complete frozen plot used for this run. |
| `report.json` | Latest status, checks, outcomes, usage, and campaign summary. |
| `report.INVOCATION_ID.json` | A retained report for each invocation, including resume attempts. |
| `trace.json` | Events, conversations, board content, and available tool evidence. |
| `credentials.json` | Private credentials and resume settings. Do not share this file. |

Use the observer API or the saved evidence for a detailed investigation. The
[API contract](API.md) lists the routes. The observer has broad access for local
inspection. It does not expose those private views through an Agent credential.

## Verification limits

The focused Inbox checks passed 38 tests, including two and fifteen concurrent
participants, private views, receipts, safe retries, late commits, and Actor
observation limits. A separate PostgreSQL plot check passed for initialization,
scheduled mail, attributed owner mail, and the Director's resource update.
Resource, market, budget, and observer checks are recorded in the
[PRD evidence register](../PRD.md#evidence-register). The latest available suite
passed 674 tests with two unavailable modules excluded. After a later report
persistence fix, 60 focused CLI, observer, and report tests passed in 3.93 seconds.
This focused result includes one new test and overlaps the earlier suite. Observer browser checks
passed on desktop and at 390 px mobile width, including live refresh and filters.
These are scoped results; the source-storage limitation remains open.

The final two-owner storm run passed every recorded integration check and its
independent automatic review. It recorded 36 owner-chat messages, four board
messages, seven emails, three mail receipts, ten Resources, one trade, and
67 tool calls. The final fifteen-owner control passed every recorded integration
check with peak concurrency 15. Each owner bought and sold one item, for 15 trades
and no market rejections. It recorded 210 owner-chat messages, 35 board messages,
30 mail messages, 27 mail receipts, 61 Resources, and 417 tool calls. Actor response
reviews were active; the separate final fifteen-owner semantic review was not run.
Direct receipt reads have automated coverage. The live run received final receipts
through Director observations and did not select `read_market_action`.

The campaign finished at 18.4320344 USD of 30 USD, with zero outstanding or
uncertain reservations. All eleven successful and failed reports are available
in the observer. The [campaign test record](ENVIRONMENT_TEST_RESULTS.md) and
[safe result summary](environment-results.json) separate actual effects, failed
coverage checks, simulator validity, and costs.
Live model campaign results must be recorded separately from automated fixtures.
A successful operation check does not establish general moral judgment, Actor
fidelity, or long-duration reliability. The system still uses fixed declared
plots. Adaptive plot search, full simulation forks, and human-calibrated model
comparisons remain later work.
