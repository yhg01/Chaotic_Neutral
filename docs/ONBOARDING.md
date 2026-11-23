# Chaotic Neutral onboarding

Part 1 is for people. Part 2 is for coding agents and the people who direct them.

This guide holds facts that stay true. For a thing that changes, such as a branch commit, a
test result or a spend, it names the command or the file that has the current value.

---

## Part 1. For people

### What Chaotic Neutral is

- Chaotic Neutral is a local test bed for personal AI assistants that act for simulated people.
- Each simulated person, an **owner** or **Actor**, is a cheap model. It talks to its own
  **assistant**, an **Agent**, in a private chat. It can also try things in the world, such as
  phoning the landlord.
- Assistants have simulated mail, documents, invented websites, a card wallet and bookings. They
  share a **message board** with direct messages. Up to 15 assistants can share one board.
- A **Director** model writes the world around them: events, letters, websites, small and large
  mishaps. Code applies money, bookings and mail. Words alone never pay or book anything.
- A **clocked** run plays one story week as a single time-ordered list of events. Model calls
  overlap, but effects commit in story order.
- Everything is simulated and local: no real mail, money or brands.
- Assistants are told that no human reads the board. The replay portal can, so reports say so.
- Stack: FastAPI, PostgreSQL, Deep Agents, LangChain and LangGraph, with models through
  OpenRouter.

### Where it is

- **Run results.** Each set of runs has its own results file in `docs/`, named `*_RESULTS.md`,
  and an evidence row (`E-xx`) in `PRD.md`. Read the newest ones for what has been run so far.
- **Models used in the week runs.**
  - Assistants: `deepseek/deepseek-v4.1-flash`.
  - Owners: `qwen/qwen3.8-flash`.
  - Director: `anthropic/claude-opus-5.5`.
- **Branches.** Each long-lived branch has one purpose.
  - `main`: the default branch on GitHub. Start new work from it. It has clocked runs, the
    live Director, invented websites and the week stories. It also has the ZeroClaw trial
    backend for assistants (`docs/ZEROCLAW_TRIAL.md`) and the misalignment judge
    (`docs/MISALIGNMENT_JUDGE.md`). The user has not decided if ZeroClaw is adopted.
  - `world-build`: the build stage. Before a run, the Director orders websites and papers, and
    a coder model writes them. It has been tried on toy worlds only. It is not merged into
    `main`.
  - `event-clock` and `zeroclaw-agent`: the lines that brought the event clock, the ZeroClaw
    trial and the judge. `main` holds their work.
- **Current state.** This guide does not record it. Find it with:
  - `git branch -a -vv`: each branch, its commit and its push state.
  - `gh pr list`: the open pull requests. They can include branches of other contributors.
  - The newest rows of the "Evidence register" in `PRD.md`: the last recorded evidence. The
    newest row that states a suite count has the last recorded test result.
  - `uv run python scripts/campaign_status.py`: the spend and ceiling of each campaign in your
    database.
- **Open problems.**
  - The week runs have no control scenario, and there is only one run per drama level. So no
    effect of drama can be claimed.
  - No human has checked the findings. One model read the run records.
  - In the five-lives runs, 52 to 62% of tool calls re-read owner messages or the update feed.
  - Replies that reach the output limit can still drop tasks. The cause is not known.
  - Branching from a clocked run is not built.
  - Owners on clocked runs can approve a booking. An owner who answers a dollar request with a
    yes in words only, or who says another person did something, is asked about it once. These
    checks use word patterns, so they miss some cases.
  - Recorded in `docs/AFTER_THE_VERDICT_RESULTS.md` for one window run, not rechecked since:
    - Four activations of one assistant ended with empty text after private-note writes, so
      that owner got no reply. No cause is recorded.
    - The one peer mail answer did not set `in_reply_to`, so it was not linked to its question.
- **Budget.**
  - A five-lives week cost $17.60 to $24.68. The high-drama week needed its ceiling raised from
    $25 to $28.
  - This guide does not record the key balance. Before a paid run, check the balance at the
    provider and the campaign spend with `scripts/campaign_status.py`.

### Where things run

Run Chaotic Neutral on your own machine, or on a server that you operate. The project needs no
shared machine.

- Follow "Local setup" in [`README.md`](../README.md). It installs the Python environment,
  writes `.env` with random local credentials, and starts PostgreSQL (port 55432) and the API
  (port 8000). Both listen on loopback only.
- Use your own database and your own provider key. Runs, campaigns and spend are records of
  one installation. Another installation does not have them.
- Keep the checkout and the database folder out of cloud-synced folders. The README says why.
- The replay portal is at `http://127.0.0.1:8000/runs`. The run console is at
  `http://127.0.0.1:8000/console`.
- If the services run on another machine of yours, leave them on loopback there and open a
  tunnel. Then use `http://127.0.0.1:18000/runs` and `http://127.0.0.1:18000/console`.

```bash
ssh -N -o ExitOnForwardFailure=yes -L 18000:127.0.0.1:8000 USER@HOST
```

### One-line commands

Run them in the repository folder, after "Local setup" in the README.

A key that your shell exports is used before the key in `.env`. To use the key in `.env`, put
`env -u OPENROUTER_API_KEY` before a command that calls a model.

| To do this | Run this |
| --- | --- |
| Check that the API and database are up | `curl -s 127.0.0.1:8000/health` |
| Start PostgreSQL | `uv run python scripts/postgres_local.py start` |
| Start the API | `uv run python -m agentrpg` |
| Free check with no model calls | `uv run python scripts/run_clocked.py --scenario scenarios/agent_bill_week.json --scripted --advance-minutes 120` |
| Start a paid, supervised week | `env -u OPENROUTER_API_KEY uv run python scripts/supervise_clocked.py --name run7 --scenario scenarios/five_lives_week.json --drama medium --seed 7 --model deepseek/deepseek-v4.1-flash --actor-model qwen/qwen3.8-flash --director-model anthropic/claude-opus-5.5 --campaign "$(uuidgen)" --budget-usd 30` |
| Follow a supervised run | `tail -f .local/overnight/run7.log` |
| See spend of the latest 100 campaigns | `uv run python scripts/campaign_status.py` (add `--campaign UUID` for one) |
| Pause a run (or press **Pause run** in `/console`) | `curl -s -X POST 127.0.0.1:8000/api/v1/console/runs/RUN_ID/execution/pause -H 'Content-Type: application/json' -d "{\"request_id\":\"$(uuidgen)\"}"` |
| Resume a supervised run | `env -u OPENROUTER_API_KEY uv run python scripts/supervise_clocked.py --name run7` |
| Raise one run's ceiling, then resume | `uv run python scripts/raise_campaign_ceiling.py --resume .local/scenario-runs/RUN_ID/credentials.json --usd 35` |
| Export a run's records | `uv run python scripts/export_run_evidence.py RUN_ID` |
| Run all tests | `env -u OPENROUTER_API_KEY uv run python scripts/test_local.py` |
| Judge a run for misaligned actions | `env -u OPENROUTER_API_KEY uv run python scripts/judge_run.py --run RUN_ID --estimate`, then the same with `--campaign UUID --budget-usd N` in place of `--estimate`. See `docs/MISALIGNMENT_JUDGE.md`. |

Notes:

- **The judge.** `scripts/judge_run.py` reads the database in `DATABASE_URL` and applies no
  migration. Add `--test-schema NAME` for a run in a schema of the test database. Its measured
  controls and its limits are in `docs/MISALIGNMENT_JUDGE.md`.
- **The API.** `uv run python -m agentrpg` runs in the foreground and stops with Ctrl+C. A run
  with models needs it. The free check does not.
- **The supervisor** (`scripts/supervise_clocked.py`).
  - It takes `--name NAME` and passes every other flag to `scripts/run_clocked.py`.
  - It writes `.local/overnight/NAME.log` and `.local/overnight/NAME.json`.
  - **Always use a new NAME.** If `.local/overnight/NAME.json` exists, it resumes that old run
    and ignores the other flags. `ls .local/overnight/*.json` shows the names that are taken.
  - Give each new run a new `--campaign` UUID and a `--budget-usd` ceiling.
  - It restarts the run after a crash or a provider outage.
  - It stops at the end of the week, at the budget ceiling, on a pause, or on an invalid result.
    It also stops after 40 restarts.
  - It runs in the foreground. To keep it going after the terminal closes, start it with
    `nohup` and `&`. On macOS, put `caffeinate -dimsu` before `uv` to keep the machine awake.
- **The free check is not read-only.** It makes no model calls and needs no server or key. But it
  writes a scripted run into the database in `.env`. The run appears in `/runs`. It also writes a
  manifest in `.local/scenario-runs/`.
- **RUN_ID.** It is on the `Run:` line of the log. The manifest is
  `.local/scenario-runs/RUN_ID/credentials.json`.
- **A pause stops new work.** A model call already in progress still finishes and may cost money.
- **`--usd` is the new total ceiling**, not an increase. It can only go up.
- **Exported records** go to `.local/run-evidence/RUN_ID/`: JSON Lines, `timeline.md` and
  `summary.json`. Use `--out DIR` to change the folder.
- **Exit codes of `scripts/run_clocked.py`.**

  | Code | Meaning |
  | --- | --- |
  | 0 | Done, stopped at its bound, or paused |
  | 75 | Provider problem; resume later |
  | 3 | Stopped by the budget |
  | 2 | Bad arguments |
  | 1 | Any other failure |

### What to change to start a different run

| Change | How |
| --- | --- |
| Story | `scenarios/five_lives_week.json` or `scenarios/agent_bill_week.json`. For a new story, fill in `docs/STORY_BRIEF_TEMPLATE.txt`. A coding agent then adds a builder to `scripts/build_week_scenarios.py`. |
| Drama | `--drama off`, `low`, `medium` or `high` on `scripts/run_clocked.py`. The supervisor passes this flag and the flags below to it. |
| Seed | `--seed N`. |
| Budget | `--budget-usd N` with a new `--campaign` UUID. Give high drama at least $30. Raise a ceiling with `raise_campaign_ceiling.py`, never by starting a new campaign for the same run. |
| Models | `--model` (assistants), `--actor-model` (owners) and `--director-model` (Director). Without flags, the assistant model is `OPENROUTER_MODEL` in `.env`, or else `openai/gpt-4.1-mini`. The owner model is `OPENROUTER_ACTOR_MODEL`, or else the assistant model. The Director follows the owner model. Always set all three. A name of the form `provider:model`, such as `deepseek:deepseek-flash` or `anthropic:claude-opus-5-5`, calls that provider with its own key (see "API key"). `src/agentrpg/budget.py` lists these models and their prices. |
| Length | `--advance-minutes N` stops after N more story minutes. A supervised restart drops it. |
| API key | `OPENROUTER_API_KEY` in your `.env`. The project keeps this one provider key and adds no other: see "Working rules from the user" in [`AGENTS.md`](../AGENTS.md). A `provider:model` name needs `DEEPSEEK_API_KEY` or `ANTHROPIC_API_KEY`, and that rule does not let you add them. If the key is not set, `run_clocked.py` stops and names the missing key. Edit the key yourself. Never paste a key into a chat, a document or git. |

---

## Part 2. For coding agents

### Overview

- Read Part 1 first.
- The project is called Chaotic Neutral. The code and folders keep the old name `agentrpg`:
  the package `src/agentrpg/`, `python -m agentrpg`, `AGENTRPG_*` settings, the database names,
  and the `agentRPG` folders. Saved result files also keep it. Use Chaotic Neutral in prose, docs
  and screens. Do not rename code, settings or folders unless the user asks.
- Clocked runs go through:
  - `src/agentrpg/clock_engine.py`: the conductor;
  - `actor.py`: the owners;
  - `director_brain.py`: the live Director;
  - `agent_surface.py`: everything an assistant reads;
  - `sim_web.py`, `web_tools.py` and `wallet.py`: invented websites and money;
  - `world.py`: bookings.
- Saved runs without a clock stay on the window engine (`run_engine.py`, `director.py`). Do not
  change it for them.
- Entry points:
  - `scripts/run_clocked.py`: one clocked run.
  - `scripts/supervise_clocked.py`: restarts a run after crashes.
  - `scripts/run_scenario.py`: window runs.
  - `scripts/build_week_scenarios.py`: writes the week scenarios.
  - `scripts/build_world.py`: the build stage, on `world-build` only.
  - `scripts/judge_run.py`: the misalignment judge.
- Tests use scripted models from `scripts/try_clock_control.py` (`ScriptModel`, `ScriptedOwner`,
  `clocked_run`). They use real PostgreSQL and make no provider calls.

### Where it is

- **Branches.**
  - Part 1 lists the long-lived branches and the commands that show their current state.
    `main` tracks `origin/main` (github.com/yhg01/Chaotic_Neutral).
  - `world-build` left `event-clock` before the booking approval of owners. But its
    `docs/EVENT_CLOCK.md` and PRD rows N-03 and N-08 already describe that change and cite E-48,
    and the branch has neither the code nor the E-48 row. Correct this when you merge.
  - Each branch gives its own evidence IDs (`E-xx`), so one ID can name different evidence on
    two branches. Settle the IDs when you merge.
- **Tests.** In the "Evidence register" of a branch's `PRD.md`, the newest row that states a
  suite count has the last recorded result of the full suite. `world-build` records its result
  in `docs/WORLD_BUILD.md`.
- **Migrations.**
  - `migrate.py` refuses a database that holds a migration the code lacks, and refuses a changed
    migration.
  - Each branch numbers its new migrations by itself, so the numbers collide. For example,
    `world-build` and `main` each have a different `022`. To list the migrations of a
    branch, use `git ls-tree --name-only BRANCH migrations/`.
  - So the migrations of a branch must not reach a database that another checkout uses with
    code that lacks them. If they do, that code refuses the database. These migrate the
    database in `.env`: a paid `build_world.py`, `python -m agentrpg.migrate`, each script that
    starts an `AgentRuntime`, such as `run_clocked.py`, and each script that calls `migrate`
    itself, such as a budgeted `author_scenario.py`. `git grep -n 'migrate(' scripts src` lists
    the direct calls.
  - Do branch work in a separate worktree with its own copy of `.env`. Point the copy only at
    the `_test` database.
  - Before two branches merge, settle their migration numbers.
- **Local state of a checkout.**
  - `.claude/` is local and not tracked.
  - A checkout can hold uncommitted edits and untracked files of another contributor or of
    another session. Look at `git status` before you edit. Do not stage, revert, reformat or
    delete files that you did not change or create.
- **Next work.** These are the open problems from Part 1, plus:
  - Confirm the booking approval and the ask-once checks in one cheap paid run, with the user's
    agreement. For what live runs covered so far, read "Limits stated plainly" in
    `docs/EVENT_CLOCK.md` and "Live results" in `docs/ZEROCLAW_TRIAL.md`.
  - Add control scenarios and repeat runs for the week stories.
  - Add a human-reviewed reference set.
  - Count decisions, not saved records.

### Rules the user has asked for

**Where to run** (`AGENTS.md` holds the full rule)
- Run on your own machine, or on a server that you operate, with your own database and
  provider key. "Local setup" in `README.md` has the commands.
- `AGENTS.md` also has a rule for the repository owner's own machines. It applies only to
  the owner's sessions.
- Start a command that calls a model with `env -u OPENROUTER_API_KEY`, so that the key comes
  from `.env`.
- Run tests with `scripts/test_local.py`. It uses a separate `_test` database. Plain pytest
  skips the database tests.
- Do not switch the branch of a checkout while its API or a run is going.
- To work on another branch while a checkout is busy:
  1. Make a worktree next to the checkout with
     `git worktree add ../Chaotic_Neutral-BRANCH BRANCH`.
  2. Copy `.env` into it. If the branch has a new migration, change `DATABASE_URL` in the
     copy to the `_test` database.
  3. Run `uv sync --locked` in it. Then use `uv run` there.
  4. Do not run `git checkout` in that worktree while tests are running.

**Git**
- Preserve other contributors' work. Commit only the paths you changed.
- Never push to the public GitHub repo unless the user asks. Before any push, read the diff and
  scan it for credentials and local files.
- Do not change tracked code while a run is going.
- Never keep a git worktree in a temporary folder. The system's temp cleaner deleted files of
  one.
- **Two sessions can share one checkout at once.** On 11 November, one session's `git
  commit --amend` on `world-build` took in another session's uncommitted doc edits. So:
  - Before you edit, check `git branch --show-current` and `git reflog`.
  - To commit to a branch that is not checked out, use a temporary index:
    `GIT_INDEX_FILE=... git read-tree BRANCH`, then `git apply --cached`, `git commit-tree` and
    `git update-ref`.
  - Restore your files in the shared tree after you commit.

**Money and model runs**
- **No standing spend approval exists.** Get the user's agreement before any paid run. The
  $150 approval of 4 November covered that night's tasks only.
- One persistent ceiling per campaign. Never reset a campaign or create a new one to get around
  a ceiling. Raise one only within a total the user agreed.
- Check the key balance and run states before paid work. Leave headroom, because provider
  outages hold reserved money.
- Use bounded runs. Report measured spend and provider failures.
- Try changes with the scripted test models first.

**Credentials and privacy**
- Never print, read or paste keys. Do not open `.env` or any `credentials.json` with tools; the
  scripts load them.
- Keep keys out of prompts, tool arguments, reports, commits and chat.
- Services listen on loopback only. Token-free access covers only `/runs`, `/console` and their
  APIs. It applies only to a direct loopback request with an accepted local Host and the same
  origin, and forwarded headers are ignored. Do not extend it to Agent endpoints, and do not add
  a login gate back.
- Never put credentials, the full plot or another owner's private view in an assistant prompt.
- Show board and mail content as text, never as HTML or as instructions.

**Realism**
- Assistants must see nothing that reads as an evaluation: no Director, activation, window, run,
  simulation, fiction, plot, cue, actor, scenario or credits wording.
  - `BANNED` in `tests/test_clock_realism.py` and `LEAK` in `tests/test_natural_surface.py` check
    part of this. They are not complete, so read new text yourself.
- Owners must sound like real people.
- Keep internal names in events and reports, never in text that an assistant or owner reads.
- Keep control scenarios explicit.
- Do not change the prompts or tool names of saved legacy runs.
- Do not claim that realism proves the assistant did not notice an evaluation.

**Architecture and the world contract**
- The diagram (`docs/diagram-source.html`, its rendered copy, `docs/diagram.json`) is the source
  of truth. Keep the three consistent.
  - Keep the four dashed Resource links, with their directions unspecified.
  - Take access only from `docs/ENVIRONMENT_EXTENSIONS.md`; infer nothing broader from the
    drawing.
- Agent and Actor are separate. Agents are under test; Actors are controlled owners.
  - The board belongs to Agents. The browser is only an observer tool.
  - Support up to 15 Agents through identities and memberships, with no special code for Agent 1
    or 2. Enforce the limit atomically.
- **Real model tool calls are the main acceptance test.** When you change Agent integration,
  verify real tool calls against the board, with the user's spend agreement. Browser checks
  alone are not enough.
- Code applies world effects. Prose, a pending receipt or an assistant's claim never establishes
  a payment, booking or consent.
  - Owners have no board, booking or payment tools. They grant consent through typed fields and
    ask the Director for effects.
- Effect order:
  - On clocked runs, the effects of one run commit one at a time in story order.
  - Never add a global Agent lock. The board stays concurrent.
  - Keep session locks separate from board writes.
- No owner view, owner tool, cue or Director context may contain board or direct-message text.
- Invented sites must not use real brands. All money, mail and sites stay inside the simulation.
- Runtime defaults: extra subagents and host shell or file access stay off by default.
  - Scope checkpoints and task IDs to cluster, Agent and session.
  - Keep tool-call request IDs across resume.

**Data**
- Use real PostgreSQL in tests and an isolated test database. Never reset a user database.
- Each post is its own row, with no global posting mutex. A sequence or timestamp is not a
  delivery cursor: use per-Agent receipts.
- Keep transactions short. Never call a model or an outside service inside a transaction.
- Commit before you report success. Make writes idempotent: a reused operation ID with a changed
  payload is a conflict.
- Add a new numbered migration for every schema change. Never edit an applied one.

**Reporting and docs**
- Record simulator faults apart from assistant conduct. A completed run does not prove that the
  run was valid.
- Keep the full trace, failed attempts and review diagnostics.
- Report control scenarios and conflict scenarios separately.
- Cite saved evidence. Model review does not replace human calibration. Say what a partial
  review did not cover.
- When behaviour or evidence changes, update the matching PRD rows and add a new evidence ID
  (`E-xx`). Keep setup commands and docs current.
- For UI changes, check narrow screens and the loading, empty and error states.
- A checkout in a cloud-synced folder can lose file contents, and a read can time out. Report
  that as blocked, not as missing.
- Write in clear, simple English compatible with ASD-STE100. State verified results and limits,
  and claim nothing you did not measure.
- Earlier decisions that still apply:
  - Concordia is a reference, not a dependency.
  - Deep Agents runs in the local app, with no managed Agent Server.
  - Do not bump the pinned library versions as a side task.

### Files to read

| File | Why |
| --- | --- |
| `AGENTS.md`, `CLAUDE.md` | The full working rules, with "Where to run" and "Working rules from the user". `CLAUDE.md` imports `AGENTS.md`. |
| `README.md`, `PRD.md`, `docs/ARCHITECTURE.md`, `docs/AGENT_RUNTIME.md`, `docs/API.md` | Required reading in `AGENTS.md`. `PRD.md` holds the component, connection and evidence rows you must update. |
| `docs/diagram-source.html`, `docs/diagram.html`, `docs/diagram.svg`, `docs/diagram.json` | The system source of truth: 13 nodes and 28 connections. Later explicit user instructions win. |
| `docs/EVENT_CLOCK.md` | The contract for clocked runs: rules R1 to R16, run flags, models and limits. |
| `docs/DIRECTOR_RUNTIME.md`, `docs/CONCORDIA_REFERENCE.md` | Director and owner design. |
| `docs/ENVIRONMENT_EXTENSIONS.md` | Mail, document and market permissions, and the dashed-link contract. |
| `docs/FIVE_LIVES_RESULTS.md`, `docs/BILL_WEEK_RESULTS.md`, `docs/FIVE_LIVES_WEEK_PLOT.md` | Results of the week runs, known faults, and the five-lives story. |
| `docs/ZEROCLAW_TRIAL.md`, `docs/MISALIGNMENT_JUDGE.md` | The ZeroClaw trial backend with its live results, and the judge with its commands and limits. |
| `docs/WORLD_BUILD.md` (`world-build` only) | Build stage rules B1 to B14 and commands. |
| `docs/RUN_CONSOLE.md` | Console, replay portal and the token-free loopback rules. |
| `src/agentrpg/agent_surface.py` | Everything an assistant reads. Any change affects realism. |
| `tests/test_clock_realism.py`, `tests/test_natural_surface.py` | The realism word checks. |
| `src/agentrpg/migrate.py` | How migrations are applied and refused. |
| `.local/run-evidence/` | The records that `scripts/export_run_evidence.py` writes. They are not in git. Read an export, not the database. |

### Known traps

- **`README.md` and the older documents describe window runs first.** For a scenario with a
  `clock` section, `docs/EVENT_CLOCK.md` is the contract.
- **Some `AGENTS.md` rules are scoped.** The rules about phases and declared branches apply only
  to runs without a `clock` section.
- **The diagram does not show the `world-build` builder yet.** Adding it needs the user's
  decision.
