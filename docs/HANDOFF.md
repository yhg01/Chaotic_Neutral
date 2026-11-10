# Clock, medium-chaos preparation and Actor memory handoff

Updated: 10 November 2026. This handoff covers the work accumulated after upstream
`main` commit `098b142`, including the latest Actor/personal Agent short test.
The review branch is `codex/clock-memory-handoff`. It does not request a merge or
authorize another model experiment.

## Current position

The elapsed Clock, durable Actor/Agent scheduling and timed simulated transfers
have working local and real-model evidence. One simplified two-friend world
completed seven simulated days from its original checkpoint. The three-friend,
four-event medium-chaos protocol is frozen and passes free native controls, but
its real-model mixed calibration and seven-day acceptance remain open.

Actor memory and day agendas are opt-in. The isolated Actor model probe passes
its three checks: expiry filtering, replanning and store/client recreation.
The subsequent full Actor/Agent short test closes the real file-update workflow,
but strict planning fidelity fails. Keep this distinction in reports.

The current runtime fingerprint is source-v30:
`0a238c1737a7cd578f39f0f94c605f098d9999c6f11835fcbfd131672b6abfb8`.
The latest two paid probes use this same runtime source. Publication adds this
handoff and shareable protocol/transcript records; it does not change that code.

## Ownership and execution flow

1. The Director delivers a private cue to a simulated human-owner Actor.
2. The Actor expresses a request or consent. Its optional `cognition` contains
   its own private thoughts and agenda; those fields are not assistant tools.
3. The paired personal Agent receives actual owner dialogue in a saved activation.
   It chooses tools through the existing Deep Agents/LangGraph runtime.
4. The Clock accounts for model output and tool time. Code commits simulated
   effects and receipts. A generated success sentence cannot establish an effect.
5. Actual assistant feedback enters a subsequent Actor view. Durable IDs,
   checkpoints and native evidence support recovery and audit.

The observer browser is a replay/inspection surface. It is not the Director or
an autonomous participant. Pushing the repository does not publish the local
server, database, credentials or saved run IDs.

## Changes after upstream main

| Area | Implementation and purpose |
| --- | --- |
| Role API selection | Separate assistant and Actor/reviewer OpenRouter profiles. Resume retains the saved route and model. Missing keys fail before generation. |
| Campaign preservation | Historical opening balances, conservative reservations, retry accounting and retained uncertain charges in one campaign. |
| Elapsed Clock | Explicit simulated seconds, ordered events, timed waits, parallel lanes and durable pause/replay. The legacy window runtime remains available. |
| Director bridge | Saved own-role inputs and outputs, dispatch identities, completion timing, scoped delivery and explicit handling of unknown responses. |
| Timed transfers | Agent calls queue a native operation; debit, credit, notice and receipt take effect at completion. Recovery controls prevent duplicate payment. |
| Actor/Agent conductor | Real graph model/tool boundaries, persistent sessions, paired-role scheduling, native owner messages and result export. |
| Cue and confirmation handling | Current-cue classification/coverage, explicit per-task confirmation contracts, real reply/read/report closure and recorded schema-rejection recovery. |
| Medium-chaos preparation | Three friends, four event types, frozen requests/deadlines and evidence-based acceptance. The shared object is the existing native message board. |
| Actor memory and planning | Owner/run-scoped lexical retrieval, persisted access times, expiry filtering, immutable context snapshots, optional thoughts and revisable UTC day agendas. |
| Evidence | Free control scripts/tests, historical real-model reports, unsuccessful attempts, protocol definitions and the latest short-test findings. |

The original transfer and browser-shopping pipelines were already merged through
PR #1. They remain part of the project; this branch extends that upstream base.
The diagrams retain their existing nodes and connections.

## Read the evidence in this order

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| [Clock](SIMULATION_CLOCK.md), [Director bridge](DIRECTOR_CLOCK.md), [timed transfers](CLOCK_TRANSFERS.md) | Native timing, ordered delivery, durable jobs and once-only financial effects under supplied controls. | General model naturalness or autonomous social success. |
| [Simplified seven-day result](DAILY_CONFIRMATION_SEVEN_DAY.md) | Same world reaches 604800 seconds; 7/7 requests have grounded closeout, 14 real mails, four confirmations and three refusals. | Seven successful changes, zero-intervention autonomy or mixed medium-chaos success. There was one effective change, a schema intervention and a funding pause/resume. |
| [Mixed protocol](MEDIUM_CHAOS_PROTOCOL.md), [free results](medium-chaos-free-validation-results.json) | Three friends, four event types and 14 requests; free native controls and negative-evidence checks. | A completed real-model mixed experiment or calibrated chaos intensity. |
| [Actor memory controls](ACTOR_MEMORY.md), [free results](actor-memory-validation-results.json) | Retrieval validity/privacy, persisted state, recovery and valid agenda mechanics. Prior full source-v30 regression: 1489 passes. | Semantic retrieval, automatic agenda wakeups or improved model task success. |
| [Isolated Actor probe](ACTOR_MEMORY_MODEL_PROBE.md) | Three real GPT-4.1 turns: 14:00 Birch becomes 16:00 Willow; expired notice is filtered; recreated clients/store retain the current agenda. | A memory-versus-no-memory comparison or a complete assistant task. |
| [Actor/Agent short test](ACTOR_AGENT_MEMORY_PROBE.md) | Two exact native request deliveries, actual file write/edit/readback, real feedback consumed by the Actor and a quiet pair afterward. | Strict planning fidelity: three findings remain, described below. |

The earlier failed seven-day world is retained separately in
[SEVEN_DAY_SIMPLIFIED_RESULTS.md](SEVEN_DAY_SIMPLIFIED_RESULTS.md). Do not replace
it with the later successful closeout or combine attempts into one success rate.
[PRD.md](../PRD.md) records component, connection and integration evidence;
E-60, E-61 and E-62 cover the latest memory work.

## Latest short test and the next fix

Run `e4340fc6-dd9b-4ea2-bc59-9019629a2baa` uses one active Alice Actor/Agent pair;
Bob's pair is configured but idle. At second 124, the initial 14:00–15:00 Birch
plan is saved and its report consumed. After the second cue at second 1800, the
same `/plans/afternoon.md` virtual file becomes 16:00–17:00 Willow. The Actor
consumes the real changed-file report and the pair becomes quiet at second 1941.
Nineteen native functional checks pass. Strict acceptance is **false**:

1. The initial accepted Actor output omits requested structured `cognition`.
2. Both Actor utterances delegate the Actor-private agenda requirement to the
   assistant. Cue framing mixes private planning with spoken owner instructions.
3. Both assistant reports add an unsupported claim about their own cognition
   agenda. The file changes are verified; that additional state claim is not.

The current reviewer accepts these outputs. Review must inspect the required
structured fields and the Actor/Agent boundary, not only cue words in dialogue.
The next work should first reproduce these exact failures with free supplied
responses, separate private planning requirements from owner speech, and reject
missing structured plans or unsupported completion claims. Preserve the original
failed evidence. Do not silently fill in model output or rewrite old messages.

Shareable inputs and original dialogue are included:

- [Actor-only protocol](experiments/actor-memory-probe-protocol.json).
- [Actor/Agent protocol](experiments/actor-agent-memory-probe-protocol.json).
- [Original short-test transcript](experiments/actor-agent-memory-short-transcript.txt).
- [Structured findings and costs](actor-agent-memory-probe-results.json).

These are operator evidence. Never provide a complete protocol/full plot to an
Actor or Agent. The full native trajectories remain private local artifacts.

## Budget and model/API state

The persistent campaign is `635aa42e-386f-43dc-b679-b73cd0cd276f`.

| State at handoff | USD |
| --- | ---: |
| Authorized cumulative limit | 25.000000000000 |
| Occupied, including historical uncertainty | 20.152809779600 |
| Remaining | 4.847190220400 |
| Retained historical uncertain charges, already included above | 2.823346400000 |
| Latest Actor-only probe | 0.084232000000 |
| Latest Actor/Agent probe | 0.128069880000 |

Both latest probes have reported costs for all requests. Do not release old
uncertain reservations, reset this campaign or create a new one to bypass its
ceiling. Provider account credit is separate from application authorization.

| Role | Model | Local configuration |
| --- | --- | --- |
| Personal Agents | `deepseek/deepseek-v3.2` | `.env.personal-agent.local` |
| Actors and independent Actor reviewers | `openai/gpt-4.1` | `.env.actor-reviewer.local` |
| Database, administrator and legacy settings | Existing values | `.env` |

Only blank examples are committed. Copy examples only when the destination does
not already exist, fill keys locally, restrict file access and keep them out of
commands, reports and commits. No model call, budget increase or seven-day start
is authorized by this handoff or PR.

## Local setup and free verification

Use Python 3.12+, the locked `uv` environment and PostgreSQL. The runtime and
tests require PostgreSQL; do not substitute SQLite. Prefer a local disk rather
than a cloud-evictable checkout. See [README setup](../README.md#local-setup).

```sh
uv sync --locked
# Set DATABASE_URL and ADMIN_TOKEN in a private .env file.
uv run python -m agentrpg.migrate
uv run python -m agentrpg
```

Apply migrations through the existing checked migration command. Never edit an
applied migration or reset the development database. This branch adds:

| Migration | Purpose |
| --- | --- |
| 017 | Historical campaign opening balances |
| 018 | Explicit-second Clock state and event queue |
| 019 | Director/Clock jobs, deliveries and recovery receipts |
| 020 | Timed transfer operations and native receipts |
| 021 | Model/tool-boundary conductor state |
| 022 | Scoped confirmation contracts and receipt/read evidence |
| 023 | Daily confirmation task activation |
| 024 | Retained Actor rejection and carried-token recovery evidence |
| 025 | Private Actor memories, immutable snapshots and agenda versions |

Free checks use a separate test database and supplied model responses:

```sh
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python scripts/test_local.py
```

`TEST_DATABASE_URL`, when supplied, must identify a distinct database with `test`
in its name. Fixtures create and remove only their owned schemas. No live model
experiment is part of those checks. The PR's fresh full regression passes **1489 tests in 366.20 seconds**, with
zero failures, errors or skips. Its isolated instance was removed and the native
campaign stayed unchanged. See [pr-handoff-validation.json](pr-handoff-validation.json).

Focused free coverage is available in `test_simulation_clock.py`,
`test_director_clock.py`, `test_clock_transfers.py`, `test_clock_runtime.py`,
`test_confirmations.py`, `test_medium_chaos.py` and `test_actor_memory.py` under
`tests/`. `scripts/prepare_medium_chaos.py --output <new-path>` writes a protocol
without credentials or generation; it does not launch the experiment.

## Operational gaps and stop conditions

- `scripts/run_clock_actors.py` still declares the historical USD20 ceiling.
  The later USD25 resumes/probes used private operator drivers. Do not assume
  this CLI can resume the current USD25 campaign without reconciling its frozen
  configuration and budget checks. Do not change a ceiling merely to make it run.
- Memory is opt-in through `ActorMemoryStore` and `RememberingActor`; the public
  paid CLI does not enable it by default. The exact latest paid probe drivers
  remain under ignored `.local/`; their hashes and public input definitions are
  recorded. A clean clone has free controls, not those private operators/run IDs.
- The memory retriever is lexical and local. There is no embedding service,
  Director-wide log RAG, automatic day-plan wakeup or activity-completion engine.
  Agendas remain intentions. The elapsed profile does not support every legacy
  Resource/commerce effect as a timed operation.
- The original pre-recovery PostgreSQL data/checkpoints were not recovered.
  Old trajectory exports and historical opening costs remain; they are not a
  substitute for an executable old graph checkpoint.
- Unknown model output stays blocked. Resume only from matching known saved
  output or an explicit supported recovery receipt; never regenerate blindly.
- BOLD OS integration, solicitor/fake-HMRC settings, competing communities and
  two-bakery experiments are not completed by this work.
- The native shared board is the current centralised object. No colleague-supplied
  replacement setting/object has been integrated. Human calibration and real
  mixed-model rehearsal remain prerequisites for medium-chaos claims.

## Preserved artifacts and review scope

Source, migrations, tests, public configuration examples, reports and shareable
protocol/transcript records belong in this PR. Provider keys, administrator and
participant credentials, PostgreSQL dumps, raw graph state and `.local/` archives
remain private. The separate Generative Agents study checkout is also excluded;
no Smallville code, persona state or new dependency was imported.

The latest run preserved all pre-existing rows in 87 native tables, all 985
trajectories present at its preservation snapshot, and the three API files.
Before/after backups each restored and matched all 87 tables. The private archive
chain retains prior costs, unsuccessful attempts and source snapshots 1–30.
Public reports contain evidence hashes and local locations, not a portable copy
of those runtime databases. A fresh clone cannot open their localhost run links.

After the local fidelity fix, reassess a bounded same-configuration paid retest
against the current ledger and explicit execution scope. Only then consider
mixed-scene calibration and a separately authorized seven-day experiment. Do
not treat a merged PR, a reached Clock horizon or a correct file as proof of
long-term autonomous reliability.
