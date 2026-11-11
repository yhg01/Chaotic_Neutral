# Chaotic Neutral onboarding

Status as of 11 November 2026. Part 1 is for people. Part 2 is for coding agents and the people
who direct them.

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

- **Runs so far.**
  - **Bill week** (4 November): six weeks with two owners each (Dan and Mei). The six weeks cost
    $17.66. The whole session, including pilots and three stopped runs, cost about $31.73. Results
    are in `docs/BILL_WEEK_RESULTS.md`.
  - **Five-lives week** (8 to 10 November): five owners in one town, at three drama levels: run4
    low, run5 medium, run6 high. Together they cost $60.40, of which the Director was $33.20.
    Results are in `docs/FIVE_LIVES_RESULTS.md`.
- **Models used.**
  - Assistants: `deepseek/deepseek-v4.1-flash`.
  - Owners: `qwen/qwen3.8-flash`.
  - Director: `anthropic/claude-opus-5.5`.
- **Latest code: branch `event-clock`, commit `fffcefa`.**
  - Owners on clocked runs can now approve a booking. Before this, no in-system booking on a
    clocked run could succeed, because owners had no way to approve one.
  - An owner who answers a dollar request with a yes in words only, or who says another person
    did something, is asked about it once.
  - These checks use word patterns, so they miss some cases. They passed 1,860 tests with
    scripted models. **No paid run has used them yet.**
- **Branch `world-build`.** It adds a build stage: before a run, the Director orders websites
  and papers, and a coder model writes them. It has been tried on toy worlds only.
- **Open problems.**
  - The week runs have no control scenario, and there is only one run per drama level. So no
    effect of drama can be claimed.
  - No human has checked the findings. One model read the run records.
  - In the five-lives runs, 52 to 62% of tool calls re-read owner messages or the update feed.
  - Replies that reach the output limit can still drop tasks. The cause is not known.
  - Branching from a clocked run is not built.
- **Budget.**
  - About **$2.27** was left on the working OpenRouter key on 10 November. The second key returns
    HTTP 401.
  - A five-lives week cost $17.60 to $24.68. The high-drama week needed its ceiling raised from
    $25 to $28.
  - A new paid run needs more credit or a new key. Check the balance before you start.

### Where things run

Everything runs on the **Mac mini**. The laptop is for editing, reading and git only.

- Reach the mini with `ssh yuhe@mini`. The repo is in `~/Desktop/BOLD/Chaotic_Neutral`, on
  branch `event-clock`.
- PostgreSQL (port 55432) and the API (port 8000) are left running there. Both listen on loopback
  only.
- To see the replay portal and run console from the laptop, open a tunnel. Then go to
  `http://127.0.0.1:18000/runs` (replay) or `http://127.0.0.1:18000/console` (controls). This
  was checked on 11 November.

```bash
ssh -N -o ExitOnForwardFailure=yes -L 18000:127.0.0.1:8000 yuhe@mini
```

### One-line commands (run on the mini)

Run `ssh yuhe@mini`, then `cd ~/Desktop/BOLD/Chaotic_Neutral`.

`launch.sh` and `resume.sh` remove any `OPENROUTER_API_KEY` from the shell, so the key comes from
the mini's `.env`. Put `env -u OPENROUTER_API_KEY` before any other command that calls a model.

| To do this | Run this |
| --- | --- |
| Check that the API and database are up | `curl -s 127.0.0.1:8000/health` |
| Start PostgreSQL | `.venv/bin/python scripts/postgres_local.py start` |
| Start the API, only if `ps -ax \| grep api_loop` shows no loop | `nohup caffeinate -dimsu .local/overnight/api_loop.sh >/dev/null 2>&1 &` |
| Free check with no model calls | `.venv/bin/python scripts/run_clocked.py --scenario scenarios/agent_bill_week.json --scripted --advance-minutes 120` |
| Start a paid, supervised week | `.local/overnight/launch.sh run7 five_lives_week medium 7 30` |
| Follow a supervised run | `tail -f .local/overnight/run7.log` |
| See spend of the latest 100 campaigns | `.venv/bin/python scripts/campaign_status.py` (add `--campaign UUID` for one) |
| Pause a run (or press **Pause run** in `/console`) | `curl -s -X POST 127.0.0.1:8000/api/v1/console/runs/RUN_ID/execution/pause -H 'Content-Type: application/json' -d "{\"request_id\":\"$(uuidgen)\"}"` |
| Resume a supervised run | `.local/overnight/resume.sh run7` |
| Raise one run's ceiling, then resume | `.venv/bin/python scripts/raise_campaign_ceiling.py --resume .local/scenario-runs/RUN_ID/credentials.json --usd 35` |
| Export a run's records | `.venv/bin/python scripts/export_run_evidence.py RUN_ID` |
| Run all tests | `env -u OPENROUTER_API_KEY .venv/bin/python scripts/test_local.py` |

Notes:

- **Helper scripts.** `launch.sh`, `resume.sh` and `api_loop.sh` are local helpers in
  `.local/overnight/` on the mini. They are not in git. `api_loop.sh` logs to
  `.local/overnight/api.log`.
- **`launch.sh NAME SCENARIO DRAMA SEED BUDGET_USD`.**
  - SCENARIO is a file name in `scenarios/` without `.json`.
  - It starts `scripts/supervise_clocked.py` in the background, with the three models above and a
    new campaign.
  - **Always use a new NAME.** If `.local/overnight/NAME.json` exists, it resumes that old run and
    ignores the other arguments. `run4` to `run6`, `b1`, `b2`, `r5` and `test_bill` are taken.
- **The supervisor.**
  - It restarts the run after a crash or a provider outage.
  - It stops at the end of the week, at the budget ceiling, on a pause, or on an invalid result.
    It also stops after 40 restarts.
- **The free check is not read-only.** It makes no model calls and needs no server or key. But it
  writes a scripted run into the database in `.env`, which is the live one. The run appears in
  `/runs`. It also writes a manifest in `.local/scenario-runs/`.
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
| Drama | `off`, `low`, `medium` or `high`: the third `launch.sh` argument (`--drama`). |
| Seed | The fourth `launch.sh` argument (`--seed`). |
| Budget | The fifth `launch.sh` argument (`--budget-usd`). `launch.sh` makes a new campaign itself. Give high drama at least $30. Raise a ceiling with `raise_campaign_ceiling.py`, never by starting a new campaign for the same run. |
| Models | Edit `launch.sh`, or call `scripts/run_clocked.py` with `--model` (assistants), `--actor-model` (owners) and `--director-model` (Director). Without flags, the assistant model is `OPENROUTER_MODEL` in `.env`, or else `openai/gpt-4.1-mini`. The owner model is `OPENROUTER_ACTOR_MODEL`, or else the assistant model. The Director follows the owner model. Always set all three. |
| Length | `--advance-minutes N` on `run_clocked.py` stops after N more story minutes. `launch.sh` does not take it, and a supervised restart drops it. |
| API key | `OPENROUTER_API_KEY` in the mini's `.env`. Edit it on the mini yourself. Never paste a key into a chat, a document or git. |

---

## Part 2. For coding agents

### Overview

- Read Part 1 first.
- The project is called Chaotic Neutral. The code and folders keep the old name `agentrpg`:
  the package `src/agentrpg/`, `python -m agentrpg`, `AGENTRPG_*` settings, the database names,
  and the `agentRPG` folders. Saved result files also keep it. Use Chaotic Neutral in prose, docs
  and screens. Do not rename code, settings or folders unless the user asks.
- The main line is branch `event-clock`. Clocked runs go through:
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
- Tests use scripted models from `scripts/try_clock_control.py` (`ScriptModel`, `ScriptedOwner`,
  `clocked_run`). They use real PostgreSQL and make no provider calls.

### Where it is

- **Branches.**
  - `event-clock` is at `fffcefa`. The mini's main checkout is there too.
  - `world-build` is `e0ad064` (`event-clock` before `fffcefa`) plus one commit, `7841abe`, the
    build stage. It adds migration `022_site_modules.sql`.
  - Because of a mistake on 11 November, `world-build`'s `docs/EVENT_CLOCK.md` and PRD rows N-03
    and N-08 already describe `fffcefa` and cite E-48. But that branch has neither the code nor
    the E-48 row. Fix this when you merge.
  - Local refs show no push of either branch; the last fetch was on 4 November. `main` tracks
    `origin/main` (github.com/yhg01/Chaotic_Neutral).
- **Tests.**
  - `event-clock`: 1,860 passed, 9 skipped (PRD E-48).
  - `world-build`: 1,900 passed, 10 skipped (`docs/WORLD_BUILD.md`).
- **Migrations.**
  - `event-clock` ends at `021`. `migrate.py` refuses a database that holds a migration the code
    lacks, and refuses a changed migration.
  - So never let `022` reach the live database while the mini runs `event-clock` code. A paid
    `build_world.py` and some scripts migrate the database in `.env`.
  - The `world-build` work uses a separate mini worktree,
    `~/Desktop/BOLD/Chaotic_Neutral-world-build`. Point it only at the test database.
- **Local-only files on the laptop.**
  - `CLAUDE.md`, `handoff_3_nov.md` and `.claude/` are untracked.
  - The "Where to run" section of `AGENTS.md` is uncommitted. A fresh clone and the mini do not
    have it.
  - Another contributor has uncommitted edits in the shop files (`shop_*.py`, `static/shop.*`,
    `tests/test_shop*.py`, `shop_legacy.py`), `docs/BROWSER_SHOP_RUNTIME.md` and
    `src/agentrpg/autonomy_validation.py`. Do not stage, revert or reformat them.
- **Next work.** These are the open problems from Part 1, plus:
  - Confirm `fffcefa` in one cheap paid run, with the user's agreement.
  - Add control scenarios and repeat runs for the week stories.
  - Add a human-reviewed reference set.
  - Count decisions, not saved records.

### Rules the user has asked for

**Where to run**
- Run nothing on the laptop: no runs, servers, PostgreSQL or tests. On the laptop, only edit,
  read and use git.
- Run everything on the mini through `ssh yuhe@mini`. If the mini is not reachable, stop and tell
  the user. Never fall back to the laptop.
- On the mini:
  - Start commands with `env -u OPENROUTER_API_KEY`. The laptop's shell sets a bad key.
  - Run tests with `scripts/test_local.py`. It uses a separate `_test` database. Plain pytest
    skips the database tests.
  - Do not switch the main checkout's branch, because it runs the API.
- To work on another branch on the mini:
  1. Send it with `git push -f mini:Desktop/BOLD/Chaotic_Neutral BRANCH`. Never push to the
     checked-out `event-clock` this way.
  2. Make a worktree with `git worktree add --detach ~/Desktop/BOLD/Chaotic_Neutral-BRANCH`, and
     symlink `.env` into it.
  3. Run with `PYTHONPATH=$PWD/src` and the main checkout's `.venv/bin/python`.
  4. If the branch has a new migration, point `DATABASE_URL` at the `_test` database.
  5. Do not run `git checkout` in that worktree while tests are running.

**Git**
- Preserve other contributors' work. Commit only the paths you changed.
- Never push to the public GitHub repo unless the user asks. Before any push, read the diff and
  scan it for credentials and local files.
- Do not change tracked code while a run is going.
- **Two sessions can share the laptop checkout at once.** On 11 November, one session's `git
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
- The laptop checkout is on a cloud-synced Desktop. If a read times out, report it as blocked,
  not as missing.
- Write in clear, simple English compatible with ASD-STE100. State verified results and limits,
  and claim nothing you did not measure.
- Also from the handoff:
  - Concordia is a reference, not a dependency.
  - Deep Agents runs in the local app, with no managed Agent Server.
  - Do not bump the pinned library versions as a side task.

### Files to read

| File | Why |
| --- | --- |
| `AGENTS.md` | The full working rules. `CLAUDE.md` imports it. |
| `CLAUDE.md` (local) | The "Where to run" rule for the mini. |
| `README.md`, `PRD.md`, `docs/ARCHITECTURE.md`, `docs/AGENT_RUNTIME.md`, `docs/API.md` | Required reading in `AGENTS.md`. `PRD.md` holds the component, connection and evidence rows you must update. |
| `docs/diagram-source.html`, `docs/diagram.html`, `docs/diagram.svg`, `docs/diagram.json` | The system source of truth: 13 nodes and 28 connections. Later explicit user instructions win. |
| `docs/EVENT_CLOCK.md` | The contract for clocked runs: rules R1 to R16, run flags, models and limits. |
| `docs/DIRECTOR_RUNTIME.md`, `docs/CONCORDIA_REFERENCE.md` | Director and owner design. |
| `docs/ENVIRONMENT_EXTENSIONS.md` | Mail, document and market permissions, and the dashed-link contract. |
| `docs/FIVE_LIVES_RESULTS.md`, `docs/BILL_WEEK_RESULTS.md`, `docs/FIVE_LIVES_WEEK_PLOT.md` | Latest results, known faults, and the current story. |
| `docs/WORLD_BUILD.md` (`world-build` only) | Build stage rules B1 to B14 and commands. |
| `docs/RUN_CONSOLE.md` | Console, replay portal and the token-free loopback rules. |
| `src/agentrpg/agent_surface.py` | Everything an assistant reads. Any change affects realism. |
| `tests/test_clock_realism.py`, `tests/test_natural_surface.py` | The realism word checks. |
| `src/agentrpg/migrate.py` | How migrations are applied and refused. |
| `.local/run-evidence/` (laptop) | Exported records of run4 to run6 and b1 to b3. Read these, not the live database. |
| `handoff_3_nov.md` (local) | Earlier user decisions and runtime rules. Its run commands and test counts are out of date. |
| Claude Code memory: `~/.claude/projects/<this project's folder>/memory/` | The user's standing instructions on realism, spend, the mini and the shared checkout. Claude Code loads it by itself; other agents must read it. |

### Known traps

- **`README.md` is behind.** It does not cover clocked runs, and its test count and model
  examples are old. `docs/EVENT_CLOCK.md` is current.
- **Some `AGENTS.md` rules are scoped.** The rules about phases and declared branches apply only
  to runs without a `clock` section.
- **`.claude/launch.json` starts the API on the laptop.** Do not use it. Runs belong on the mini.
- **The diagram does not show the `world-build` builder yet.** Adding it needs the user's
  decision.
