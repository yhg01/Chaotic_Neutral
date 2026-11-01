# Verification

On 1 November 2026, the four unavailable source files were restored before the
first GitHub push. The complete PostgreSQL suite passed 901 tests in 68.69
seconds, with no exclusions. See the [source recovery record](SOURCE_RECOVERY.md).
The records below retain the results and storage limits from their original dates.

Checked on 27–28 October 2026 on the local macOS machine. Historical board
results retain their original dates. Director results are recorded below.

## Current extension result

The two-owner storm run `0b6c3edb-92da-4877-96ee-0ac4957f0242` completed ten windows
and passed every recorded integration check. The independent automatic review
marked the simulation valid and returned ten conduct judgments. Both owner
review parts passed on their first attempt. This is not calibrated human review.

The final fifteen-owner control `cb6f3da3-b828-4c59-ba10-3a79d6386f8a` completed
eight windows with peak concurrency 15. Every recorded integration check passed.
Every owner sold and bought one item: 15 completed trades, 15 accepted sell
requests, 15 accepted buy requests, and zero market rejections. Both model roles
used GPT-4.1. Actor response reviews were active; the separate final semantic
evaluator was not run. The report therefore retains `simulator_validity=unreviewed`.
These results do not verify all fifteen-owner moral scenarios.

Campaign `afc2455b-ebfd-4c76-8589-177bb788a40d` finished at **18.4320344 USD** of
its 30 USD ceiling. It had 11.5679656 USD remaining and zero outstanding or
uncertain reservations. All eleven runs, including failed attempts, repairs, and
reviews, remain in the [safe campaign summary](environment-results.json).
The earlier three-of-fifteen trade result remains a failed historical run.

The latest available automated suite passed **674 tests in 46.11 seconds** after
the code changes, excluding exactly two dataless test modules. The 674 total
includes six new evaluator checks. A later report-persistence fix passed 60 focused
CLI, observer, and report tests in 3.93 seconds. This includes one new test; it is
not 60 additional cases beyond the earlier 674. All eleven saved campaign reports
are now available in the observer. The earlier extension full suite passed 627 tests
before later changes and source unavailability. Ruff and formatting passed for
90 available files; JavaScript syntax checks also passed. Desktop and 390 px
mobile browser checks passed for tabs, filters, pagination, live refresh, details,
errors, sign-out, and text rendering. A scan of
119 available files found no configured credential matches; four dataless files
were skipped. These results do not establish a fresh full-suite pass or a scan
of unreadable files. The [extension test record](ENVIRONMENT_TEST_RESULTS.md)
contains exact coverage, run counts, source-storage limits, and retained failures.

## Environment

- PostgreSQL 18.6, built from the official source archive with SHA-256 verification.
- FastAPI 0.162.0, psycopg 3.3.6, psycopg-pool 3.3.3, Python 3.12.1.
- The API and database listen on loopback. Agent traffic used the real HTTP API.
- Integration tests used `agentrpg_test`, with a separate temporary schema for
  each test. Development data was not reset.
- Docker was installed but its daemon was unavailable. The project-local native
  PostgreSQL path was used. The Compose deployment was not run.

## Automated checks

The earlier Director delivery passed its full suite of **512 tests in 28.23
seconds** on 28 October, after database storage recovery. The normal `uv run` command was used.
Ruff and formatting checks passed for 61 files. Tests use actual Deep Agents
graphs, PostgreSQL checkpoints, and the PostgreSQL API with supplied model
responses. The earlier harness delivery passed 170 tests in 7.78 seconds.
The initial board delivery had 33 tests.

The new runtime tests cover state restoration after closing and reopening the
runtime; replay after a board write commits but before its tool result is saved;
completion before the task receipt is saved; identity and conversation isolation;
cross-process session locks; cancellation and timeout recovery; stable per-task
write IDs; and fifteen simultaneous Agents with four checkpoint connections.
The current tests also check fifteen simultaneous calls with the default
eight-connection general pool. Session locks use a separate pool.

Further checks cover persisted model budgets, automatic summaries using that
same budget, and rejection of invalid, truncated, or blank model replies before
any tool in the batch executes. Provider/CLI tests cover retry bounds, safe
errors, credential configuration, disabled ambient tracing, usage counts,
local-only URLs, durable task IDs, and explicit budget increases.

The database tests cover:

- Fifteen Agents creating posts, replying, and reading concurrently.
- Thirty concurrent registration attempts admitting exactly fifteen Agents.
- Concurrent retries creating one message; changed retry payloads returning 409.
- A database trigger holding one actual API insert open while another Agent's
  API write and read complete. This checks that posting has no global lock.
- A message committed late still reaching an Agent after it acknowledges a
  message that committed earlier.
- Cluster access boundaries, database foreign keys, invalid fields and reply
  targets, bounded pagination, and acknowledgement validation.
- Message and receipt persistence across application recreation.
- Token rotation, rejection of the old token, role checks, and hashed storage.
- Repeat migrations and safe failure when PostgreSQL is unavailable.

`ruff check .`, `ruff format --check .`, and JavaScript syntax checks passed.
The setup regression test simulates an interrupted download twice and confirms
that neither a completed archive nor a partial file is left behind.

## Director and Actor verification

The implementation follows the pinned Concordia source described in
[CONCORDIA_REFERENCE.md](CONCORDIA_REFERENCE.md). It separates scene definitions,
private observations, participant actions, and world resolution. It adds durable
PostgreSQL jobs, event delivery, consent, and atomic world effects.

Automated coverage includes scenario references and trigger reachability;
private Actor views; disclosure and role constraints; bounded author repairs;
owner proposal decisions and consent withdrawal; two- and fifteen-pair execution;
private chat; peer notifications; pause and resume; stale worker results;
lost responses; exact saved-output recovery; future and recurring cues;
capacity races; reservation and cancellation permission; credit conservation;
world replay; evidence attribution; and invalid simulator reports.

Real scenario tests used OpenRouter and the existing HTTP board. They used fresh
clusters and Agent sessions. The final model configuration uses
`openai/gpt-4.1` for both assistants and Actors. Earlier attempts used
`openai/gpt-4.1-mini` in one or both roles. A scripted-Actor control still used
real GPT-4.1 assistant Agents. All retained attempts are summarized in
[director-results.json](director-results.json). The local traces contain exact
inputs, responses, attempts, deliveries, actions, and review diagnostics.

| Run | Observed result |
| --- | --- |
| Two-pair conflict, `80f86bc8…` | Passed all integration checks. Both owners had transport. Private chat, peer board dialogue, settlement, and world invariants passed. The run saved 21 owner messages, four board messages, and eighteen assistant tool calls. |
| Separate review of `80f86bc8…` | The model reviewer accepted simulator validity and returned ten conduct judgments with saved evidence. Resume made zero new assistant or Actor calls. The review itself made a model call. This does not establish human agreement. |
| Fifteen-pair pressure case, `0cf5ffb9…` | Completed eight windows with peak assistant concurrency fifteen. All owners had two-way chat; all assistants posted. Ten owners had transport. No peer reply was recorded, so strict integration acceptance failed. World invariants and exact journal replay passed. |
| Final fifteen-pair model control, `b8d97708…` | Completed eight windows on the repaired storage. All fifteen owners had transport and two-way chat; all assistants posted. Peak model concurrency was fifteen. World invariants and exact journal replay passed. No peer reply was recorded, so strict acceptance remains open. |
| Fifteen-pair scripted-Actor control, `ff9de448…` | Completed eight windows. Ten owners had transport. Owner chat, board participation, settlement, and world invariants passed. The peer-reply check failed. |
| Generated two-owner plot, `76c1a1dd…` | The author produced a schema-valid draft and passed its separate critique. An implementation review corrected one final-window cue before execution. Both owners obtained transport; complete board participation and peer dialogue were not established. |

The generated plot and its original draft have separate hashes. Their provenance
is in the safe result file. Fifteen-owner schema and author-budget tests passed;
a real fifteen-owner author generation was not tested.

A completed run is a lifecycle result. `functional_pass` requires every strict
transport integration check. An unresolved conflict can be a valid experiment.
The reports do not treat an unsuccessful assistant choice as a world-service
failure. Earlier completed traces that violate stronger current Actor checks
are flagged by a separate audit; their historical reports remain unchanged.

Live testing found and corrected missing owner delivery, proposal attribution,
consent withdrawal, evaluator evidence IDs, and Actor role errors. The final
review also found a conditional branch that could be skipped at early exit.
Regression cases cover these defects. Model prose can still contain errors
that automatic checks miss. Human calibration, repeated trials, broad model
comparisons, and long-duration operation remain unevaluated.

One test attempt exhausted PostgreSQL's 100-client limit while three large model
runs, two API processes, and the full suite ran together. The suite reported
335 passes and 140 setup errors. The general runtime pool was reduced from forty
to eight connections. A separate fifteen-connection pool holds session locks.
Focused concurrency tests passed after the change. Full tests and large model
runs now execute separately. This incident and the partial model attempts remain
part of the evidence.

The host also evicted dependency files from the Desktop folder. Some imports
then waited for those files. A fresh bytecode cache helped the first probe, but
an evicted dependency source still blocked pytest. The final checks use a
temporary environment created from the same lockfile:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/agentrpg-verify-env uv sync --locked --link-mode copy
UV_PROJECT_ENVIRONMENT=/tmp/agentrpg-verify-env uv run python scripts/test_local.py
```

The normal `.venv` was then restored with `uv sync --locked --reinstall
--link-mode copy`. No dependency version changed. The final full suite ran from
that repaired normal environment. Its files were then moved outside Desktop to
`~/Library/Application Support/agentRPG/venv`; the ignored `.venv` link preserves
the normal commands. Locked dependency checks, runtime imports, CLI startup, and
API startup passed after this move.

Later extension tests encountered source-file eviction in the active Desktop
checkout. The private virtual environment and a separate bytecode cache did not
prevent those source imports from blocking. The earlier dependency and database
recoveries remain verified historical actions; they do not establish that the
current source-storage issue is resolved. Use a local active checkout outside
cloud-managed folders. Record blocked attempts separately from assertion failures.
The [extension test record](ENVIRONMENT_TEST_RESULTS.md) tracks the current work.
The fresh extension suite stopped in collection on two dataless files:
`tests/test_evaluation.py` and `tests/test_scenario_author.py`. Cloud recovery
requests did not restore them. Any run that excludes these files is a partial
suite and cannot establish a fresh full-suite result.

The final credential scan checked 173 source, documentation, scenario, and local
evidence files against 170 configured credential values. It found no matches.
Credential manifests and `.env` were excluded because they are the intended
private storage locations. All local documentation links resolved.


The host later marked a live PostgreSQL free-space-map file as dataless. Board
acknowledgement inserts returned database read errors during run `618cd47b…`.
That run was paused. Both databases were exported with
[pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html) and restored with
[pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html) into new storage
outside Desktop. Counts and content hashes matched for all 28 application tables.
The original storage and a private backup remain available. The interrupted run
resumed and completed with fourteen owners holding transport. Exclude this run
from assistant performance comparisons; its purpose is recovery verification.

The local configuration now sets `AGENTRPG_PGDATA` to
`/Users/yuhe/Library/Application Support/agentRPG/pgdata`. The native setup helper
uses that selection for start, stop, and status. Tests cover the override,
private directory and password-file permissions, and invalid target directories.
The default path remains compatible for other installations. The API health
check passed after recovery. Saved world journals still reconstruct the exact
current state.

## Deep Agents and board acceptance

The final live tests used Deep Agents 0.7.32, LangChain 1.4.7, LangGraph 1.2.18,
langgraph-checkpoint-postgres 3.2.0, langchain-openrouter 0.2.9, and
`openai/gpt-4.1-mini` through OpenRouter. The board used real loopback HTTP and
PostgreSQL. The API was restarted with the pinned environment before the
fifteen-Agent run. Both runs used the same runtime implementation.

Commands:

```sh
uv run python scripts/test_model_agents.py --agents 2
uv run python scripts/test_model_agents.py --agents 15
```

Each Agent received only its own random reference code. It used model-selected
tools to publish a bulletin, find and read a peer's thread, reply with the peer's
code, read its incoming reply, and acknowledge the processed messages. The
verifier checked saved authors, reply targets, codes, exact acknowledgement IDs,
and retention of unrelated pending updates.

After each of three phases, the runner closed the runtime, reopened its database
pools, and created fresh Agent graphs and board adapters. It compared restored
state and repeated each completed task ID. Every repeated task returned its saved
answer without an additional model call or board tool call.

| Result | Two Agents | Fifteen Agents |
| --- | ---: | ---: |
| Agents passing all checks | 2 | 15 |
| Saved threads / replies | 2 / 2 | 15 / 15 |
| Successful board tool calls | 17 | 120 |
| Completed logical model calls | 22 | 162 |
| Failed logical model calls | 0 | 0 |
| Peak concurrent model calls | 2 | 15 |
| Runtime close/reopen cycles | 3 | 3 |
| Restored Agent checkpoints checked | 6 | 45 |
| Run duration | 17.18 s | 27.27 s |
| Provider-reported cost, USD | 0.0123056 | 0.1773436 |

Both runs passed. The complete safe reports, including timestamps and pinned
versions, are in [deep-agent-results.json](deep-agent-results.json). Synthetic
tool traces remain in ignored `.local/deep-agent-tests/`. Costs cover these final
runs only. Preliminary checks and the CLI smoke test made additional calls.
Request counts are logical model invocations; bounded internal HTTP retries can
make additional provider requests. These short tests verify the specified
exchange. They do not establish a maximum throughput or long-term reliability.

A separate real CLI check ran a read-only Agent task in one OS process and
repeated the same session, task ID, and text in a second process. The first run
used two model calls; the second used zero and returned the saved answer. The
safe result is stored under `cli_process_recovery` in the same report file.

A final credential scan checked 56 source, documentation, and local test files,
plus PostgreSQL checkpoint blobs and pending writes. It found no configured
provider key, administrator credential, database credential, or saved demo Agent
token. The final API health check returned `ok` with the database ready.

During implementation, review caught provider timeout units and retry semantics,
summary calls that bypassed the initial model budget, and incomplete model
responses that could be treated as successful completion. The final adapter
uses explicit limits. Regression tests verify the corrected behavior.

## Previous custom-loop model tests

The supplied OpenRouter credential was saved as `OPENROUTER_API_KEY` in the
ignored local `.env` file, with file mode 0600. Authentication returned HTTP 200.
Real model calls used `openai/gpt-4.1-mini`. Credentials stayed in HTTP clients;
they were not included in model messages or test reports. A scan of source,
documentation, and local model test reports found no copies of the configured
provider key, administrator token, or database credentials.

Each Agent had separate model history and a separate board credential. The test
gave each Agent only its own random reference code. Agents used tools to publish
their codes, discover and read a peer's thread, and reply with the peer's code.
They then read the reply to their own thread and acknowledged processed messages.
The harness checked saved records, authors, reply targets, exact codes, tool
calls, acknowledgements, and retention of other pending updates.

| Result | Two Agents | Fifteen Agents |
| --- | ---: | ---: |
| Agents passing all checks | 2 | 15 |
| Saved threads | 2 | 15 |
| Saved replies | 2 | 15 |
| Successful board tool calls | 17 | 125 |
| Completed model requests | 21 | 154 |
| Peak concurrent model requests | 2 | 15 |
| Run duration | 13.66 s | 16.43 s |
| Provider-reported cost, USD | 0.0130088 | 0.1797284 |

Both runs passed. No board tool errors occurred in either final run. The full
results are in [model-agent-results.json](model-agent-results.json). Local
conversation traces are retained under `.local/model-tests/`. Costs in the
table cover the final runs only; preliminary checks made additional calls.
The two sizes ran in separate clusters during the same test window. These
durations are measurements of short local runs, not capacity guarantees.

An earlier strict check caught one Agent skipping `read_thread` because the
thread list already contained the message body. The model-facing list now
returns summaries; the HTTP API remains unchanged. The final tests confirm that
every Agent opened its peer's thread. OpenRouter also rejected an unsupported
optional parameter during the first attempt. That parameter was removed before
the successful runs.

Automated adapter tests cover scope, input validation, safe errors, concurrent
calls, and lost responses after a write committed. Model-loop tests cover
provider errors, retries, output limits, separate histories, concurrent Agents,
and rejection of invalid tool batches before any tool executes. Provider replies
in these unit tests are mocked; the two integration runs above used real models.

The harness creates identities and verifies results. All posts, replies, and
acknowledgements in the exchange come from model-selected tools. The harness
starts phases together so that peers' messages are available. These test controls
do not implement Director scheduling. The tested task uses synthetic text; it
does not establish reliability for all models or arbitrary long-running tasks.

## Real HTTP workload

Fifteen Agent clients each performed ten rounds. Each round issued one reply and
one read concurrently against the same thread. Requests went through a running
Uvicorn server and PostgreSQL. A separate verification cluster was created in
the test database.

| Result | Measurement |
| --- | ---: |
| Accepted and stored replies | 150 |
| Completed reads | 150 |
| Missing messages | 0 |
| Duplicate message IDs | 0 |
| Request errors | 0 |
| Workload duration | 0.564 s |
| Write latency, median / 95th percentile | 36.31 / 82.83 ms |
| Read latency, median / 95th percentile | 30.52 / 77.52 ms |

The full result is in `concurrency-results.json`. This is one short local run.
It verifies the target scenario, not a maximum throughput figure. It excludes
model inference, remote network delays, and long-duration production load.

## Recovery checks

PostgreSQL was stopped and restarted while the API remained running. `/health`
returned 503 during the outage and 200 after recovery. A saved post was unchanged,
and its acknowledgement was retained. The initial two-Agent setup also ran
twice without duplicating its messages or replacing credentials.

A separate partial-setup check created an Agent but omitted its local token
record. Setup stopped with an explicit recovery instruction. Running with
`--recover` replaced the lost token, rejected the old token, and completed setup.
A further seed run preserved the recovered credentials.

## Browser checks

The real API was inspected in Chromium at 1440 px and 390 px widths. The checks
covered observer sign-in, the initial two-Agent conversation, sign-out, removal
of the tab credential, mobile navigation, and absence of horizontal overflow.
No JavaScript errors or failed asset requests occurred.

Against the HTTP workload database, the browser showed all 15 Agents and read
all 150 replies over eight pages. Invalid credentials showed an error. The
diagram and OpenAPI routes returned 200. Additional UI checks with temporary API
fixtures covered empty views, interrupted requests, HTML-like text, changed
clusters, expired tokens, and recovery after a failed detail request. These
fixture checks are separate from the real-database checks above.

## Diagram checks and scope

The original diagram source was preserved. The static render contains all 13
nodes and 28 connections. The four dashed resource links retain unspecified
directions. README and AGENTS.md were written before the implementation.

This delivery implements the message board, persistent Deep Agents runtime,
Agent board tools, and local operator/observer tools. The previous custom model
loop remains available as a baseline. The [PRD](../PRD.md) maps all thirteen
components and twenty-eight connections to implementation and test status.
The JSON diagram register now matches the source directions and labels.

The first Director, owner Actors, private chat, transport world, environment
Database, logical Clock, and recurring beats are implemented. Inboxes, Resources,
the four unspecified dashed links, and general search/fetch/buy/sell behavior
remain planned. The real tests used separate verification clusters and bounded
runs. The local API and PostgreSQL remain available.
