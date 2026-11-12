# ZeroClaw trial: can ZeroClaw run the Chaotic Neutral assistants?

Date: 12 November 2026. Branch: `zeroclaw-agent`. Status: trial, not merged.

This document records a test of ZeroClaw as the assistant (Agent) runtime on clocked runs, and
an estimate of the work to make our setup compatible with it. All tests used scripted models.
No provider call was made and no money was spent.

## Short answer

- **ZeroClaw can drive our assistants, and the trial does this.** With ZeroClaw in control of
  the tool loop, the scripted control story gives the same story record as Deep Agents gives.
- **To get this result, the harness keeps almost all the work.** The harness still makes the
  model call, runs every tool, holds the history, counts the limits and decides when a turn
  ends. ZeroClaw only decides "ask the model again" and "run the next tool".
- **With its default behaviour, ZeroClaw breaks our realism rule.** Its system prompt gives the
  model the real host name, a folder path and safety text about shell commands. It also puts
  the real date and time before the owner's message. No configuration key removes these.
- **Effort, as estimates.**
  - ZeroClaw as the loop only: 12 to 32 working days, most likely about 20.
  - ZeroClaw's own prompt, with the problem lines rewritten at our bridge: about 3 to 6 days
    more. This does not cover ZeroClaw's memory.
  - ZeroClaw's own prompt and memory, with a patched build: 30 to 80 days, most likely about
    50. It is not certain that this can meet our clock rules.
- **Recommendation.** Do not replace the Deep Agents runtime with ZeroClaw for evaluation runs.
  As a loop only, it changes no result and adds an outside program with frequent breaking
  releases. If the aim is to study ZeroClaw itself as a subject, plan that as a separate project.
- **A rule must change first.** `AGENTS.md` says "Personal Agents use Deep Agents with LangChain
  and LangGraph in the local application". Any adoption needs your explicit decision.

## What ZeroClaw is

Source facts are from the `master` branch of the official repository unless 0.8.7 is named.
Facts about the program's behaviour were seen on the Mac mini with version 0.8.7.

| Item | Finding |
| --- | --- |
| Source | `github.com/zeroclaw-labs/zeroclaw`. Its README says that this is the only official repository. |
| Version tested | `v0.8.7`, released 27 October 2026. The program reports `zeroclaw 0.8.7 (99bd353)`. |
| Licence | MIT or Apache-2.0. |
| Size | The 0.8.7 program for the mini is 42.9 MB. On `master`, the source has about 25 crates, and one file of 26,000 lines holds the agent loop. |
| Time and memory | One turn with four model steps and four tool calls took 0.2 s. Its maximum resident set size was 38 MB. This is one measurement, with scripted replies on loopback. "Under 5 MB" is a target in the ZeroClaw docs, not a measurement. |
| Change rate | The release notes of 0.8.0, 0.8.5, 0.8.6 and 0.8.7 each list breaking changes. The notes of 0.8.1 to 0.8.4 were not read. The unreleased 0.9.0 changes the configuration schema again. |
| Install on the mini | The official release file `zeroclaw-aarch64-apple-darwin.tar.gz`, 32.6 MB. Its SHA-256 matched the published `SHA256SUMS` and the digest that GitHub reports. It is in `~/Library/Application Support/agentRPG/zeroclaw/0.8.7/`, not on `PATH`. The install script was not used, and no service was installed. |

## What was built

| File | Purpose |
| --- | --- |
| `src/agentrpg/zeroclaw_agent.py` | The trial backend: a loopback bridge and a `ZeroClawAgent` class. |
| `migrations/022_zeroclaw_messages.sql` | One table for the session history, which the clock reads. |
| `src/agentrpg/clock_engine.py`, `deep_agent.py` | One seam: the engine asks the agent for its messages through `activation_messages()`. Before, the engine read the LangGraph state directly. |
| `scripts/try_clock_control.py` | `clocked_run` and `run_control` take an `agent_factory`. |
| `scripts/probe_zeroclaw.py` | Shows what a ZeroClaw version sends to a model and to tools. It needs no database. |
| `tests/test_zeroclaw_agent.py` | Nine tests. They skip unless `AGENTRPG_ZEROCLAW` names the program. |

The seam and the `agent_factory` argument are safe to merge without the rest. Migration `022`
is not: see "Limits".

### How the trial backend works

One ZeroClaw process runs each activation: `zeroclaw agent -a assistant -m "<text>"`. Each
assistant has its own ZeroClaw folder. The process talks to one loopback server in the harness:

1. **Model route.** The ZeroClaw model client posts to an OpenAI-compatible route. The harness
   makes the real model call with the same model object and the same code as today. This code
   holds the campaign budget, the model-call limit, the wrap-up note, the cut-off retry and
   the clock's think time.
2. **Tool route.** The ZeroClaw MCP client lists and calls our tools on an MCP route. The
   harness runs each call through the existing adapters. This code holds the gate, the saved
   tool events, the request IDs and the board credential.

The program gets no provider key and no board credential.

The backend has two modes:

- **`harness`** (default): the model reads our instructions, the history that the harness saved,
  and our tool names. The text that ZeroClaw writes is not sent to the model. Differences from
  the Deep Agents backend: no notes and to-do tools, no summaries of long history, and no text
  that the Deep Agents library adds to the system prompt.
- **`zeroclaw`**: the model reads the request as ZeroClaw wrote it. This mode is for
  measurement of the program. It is not for evaluation runs.

A retried activation starts a new process. The harness gives it the saved model messages and
tool results again in order, so no model call and no world effect occurs twice.

## Test results

All on the Mac mini, with real PostgreSQL, the real ZeroClaw 0.8.7 program and scripted models.

| Test | Result |
| --- | --- |
| The three-owner control story on both backends | Passed. The check replaces random IDs, date-time text and paging cursors with marks. After that, the story text is identical: the same events at the same story seconds in the same order. No attempt failed and each program ended with exit code 0. The check does not compare what the model read. |
| `harness` mode: what the model reads | Passed. Our instructions come first. Tool names have no prefix. The input has no ZeroClaw text, no host line, no folder path and no real clock. |
| `zeroclaw` mode: what the model reads | Passed. It records the items in the next section. The harness still saves the world effect. |
| A tool call fails once, before or after its effect | Passed in both cases. The retry ran in a second process, which got the saved messages and result again. Each owner message was sent once. |
| Repeated and duplicate calls | Passed. Two identical calls in one model reply and six identical calls in a row all ran. With ZeroClaw defaults, the first case skips a call and the second stops the turn. |
| A tool name that is not offered | Passed. The model gets our fixed sentence, not ZeroClaw's error text. |
| An abandoned activation, then a later one | Passed. Each call left open gets a result, so a provider accepts the history. |
| No host tools and no keys | Passed. The model is offered only our tools. The ZeroClaw configuration holds no provider key and no board credential. |
| Full suite on this branch | Passed: 1,869 passed and 10 skipped, with lint and format checks clean. That is the 1,860 tests of `event-clock` and the 9 new ones. Without `AGENTRPG_ZEROCLAW`, the 9 new tests skip. |

Probe findings on ZeroClaw 0.8.7, from `scripts/probe_zeroclaw.py`:

| Question | Finding |
| --- | --- |
| Does a custom model endpoint work? | Yes: `[providers.models.custom.<name>] uri = ...`. With native tools, the request holds `model`, `messages`, `max_tokens`, `stream: false`, `tools` and `tool_choice`. |
| Native tool calls? | Not by default on a custom endpoint. Without `native_tools = true`, ZeroClaw writes the tools into the system prompt and expects `<tool_call>` text. The probe did not test that mode correctly, because its scripted model replied with native calls. With the setting, native calls work and keep the model's call IDs. |
| Can it use our tools without a rebuild? | Yes, through MCP over HTTP. Each tool name gets a forced prefix, `<server>__<tool>`. |
| Does the MCP call carry the model's call ID? | No. The trial matches calls by name, arguments and order. Calls arrive one at a time, in the model's order. |
| Can shell, file and memory tools be removed? | Yes. A closed `allowed_tools` list removes all built-in tools from the list that the model reads. |
| Does one-shot mode keep history? | No. A second `agent -m` call starts with only a system prompt and the new message. |
| Identical calls in one model reply | With defaults, the second call is skipped: "Skipped duplicate tool call ... in this turn." The trial sets `tool_call_dedup_exempt` for all our tools. |
| The same call five times in a row | With defaults, the turn stops with exit code 1 ("Circuit breaker"). The trial sets `loop_detection_enabled = false`. |
| A tool slower than its timeout | The model is told, for example, "MCP server `home` timed out after 2s ...; outcome unknown and request was not replayed". The limit is 600 s at most. A call that waits for its turn in story order can wait longer than that. |

## What the model reads in `zeroclaw` mode

With our full instructions in a ZeroClaw persona file and ZeroClaw defaults, the system prompt
was 7,240 characters. 4,875 are ours and 2,365 are added by ZeroClaw. These items break our rules:

| ZeroClaw adds | Example from the test |
| --- | --- |
| The real host, system and shell | `Host: mini \| OS: macos \| Shell: zsh \| Model: assistant-model` |
| The real date and time before the owner's message | `[2026-11-12 00:36:40 +00:00] It's Monday 9 November, 06:00.` The real date and the story date disagree in one line. |
| A real folder path | `Working directory: /var/folders/.../agents/assistant/workspace` |
| Task and safety text about a computer | "Do not run destructive commands without asking", "Prefer `trash` over `rm`", "running commands, reading files" |
| Words that our tests ban | `run`, `running`, `simulating`, `beats` |
| Prefixed tool names | `home__send_mail`, `home__end_turn` |

No configuration key in 0.8.7 removes the host line, the time prefix, the task and safety
sections or the tool-name prefix. The folder path can be set to a different path, but the line
stays. A further section about messaging channels ("You are running as a messaging bot") shows
only with `compact_context = false`. The default leaves it out, and it is needed only when a
persona file is longer than 6,000 characters.

## Compatibility, item by item

| Our requirement | ZeroClaw with default behaviour | In the trial |
| --- | --- | --- |
| Model calls go through the campaign budget | Its own client calls the provider, so our ceiling is bypassed. | Solved: the model route. |
| Story time comes from saved model messages (R6) | It keeps history in its own store, on its session interfaces only. | Solved: the harness saves each message. |
| World effects wait at the gate in story order (R5) | It calls a tool immediately when the model asks. | Solved: the tool route holds each call. Open risk: the 600 s limit. |
| Stable call IDs across a retry | It does not replay tool calls after a failure. | Solved for a harness-side failure: saved messages go to a new process. A crash of ZeroClaw or of the harness process was not tested. |
| No ZeroClaw text, host name or real clock in the model input | All three are present. | Passed the tests in `harness` mode. This does not show that an assistant cannot detect a test. |
| No host execution | Shell and file tools are on by default. | Solved by configuration. |
| History for a week, with summaries | One-shot mode has none. Old turns are dropped, not summarised. | Partly: full history, no summaries yet. |
| Private notes and to-do tools, which our instructions name | Not present when its file tools are off. | Missing. |
| Model-call limit, wrap-up note, cut-off retry, unread-mail rule | A step limit, an hourly action limit, a daily cost limit and a loop detector. | Solved: our own code is reused. |
| Credentials stay away from the model | Its configuration would hold the provider key. | Solved: the program gets no provider key and no board credential. |
| Full evaluation trace | By default its trace keeps the last 200 entries, redacts tool data and does not save model requests. All modes scrub token-like text and can drop events. | Solved: our own events and messages. |

## Effort

These are estimates in working days for one engineer who knows the code. They were not
measured. An independent model review gave higher figures than the first estimate, and this
table uses the wider range.

### Option A: ZeroClaw as the loop only (`harness` mode)

The trial does this for scripted runs. Work before a paid week-long run could use it:

| Work | Days |
| --- | --- |
| Run script: `--agent-backend`, saved in the manifest, refused on a changed resume; supervisor | 1 to 2 |
| A shared base class, in place of the trial's use of private methods | 1 to 3 |
| Summaries of long history, equal to the current 60,000-token rule | 2 to 4 |
| Notes and to-do tools equal to the library tools, or new instructions for this backend | 1 to 4 |
| Other library behaviour: large tool results; a test that compares the full model request | 1 to 2 |
| Long waits: the 600 s tool limit and the 3,600 s model-route limit | 1 to 2.5 |
| Process control: orphan cleanup, a kept ZeroClaw folder for each run | 0.5 to 2 |
| The engine tests on this backend: about 25 cases, such as 15 owners, kill and resume, budget pause, outage, cut-off, unread mail, web tools, banned words | 2 to 6 |
| One short paid run through the bridge, with one correction cycle | 1 to 3 |
| Migration number: `022` collides with `world-build` and with pull request 2 | 0.5 |
| Version pin, the program's SHA-256 in the run manifest, install on another machine | 0.5 to 1.5 |
| Docs, PRD rows and the `AGENTS.md` rule | 0.5 to 1.5 |
| **Total** | **12 to 32, most likely about 20** |

What we gain: nothing that we expect to change results. The same Python code makes the model
calls and runs the tools. What we add: a 43 MB outside program with frequent breaking
releases, and one process start for each activation.

### Option B: ZeroClaw's own prompt, with the problem lines rewritten at the bridge

The bridge already reads each model request. It can replace the host line, the folder path and
the time prefix, and remove the tool-name prefix, before the request goes to the model. The
fixed ZeroClaw phrases that our word tests ban would need an exemption in those tests.

- Effort: about 3 to 6 days more than the Option A items that apply.
- No patched build is necessary.
- It covers the ZeroClaw prompt only. One-shot mode has no memory, so the harness still
  supplies the history.
- A run on this option and a run on Deep Agents give the model different text. Results from
  the two are not directly comparable.

### Option C: ZeroClaw's own prompt and memory, with a patched build

This option studies ZeroClaw as a subject. It needs:

| Work | Days |
| --- | --- |
| Rust toolchain on the mini, and a repeatable build of a pinned tag | 1 to 4 |
| Source patch: host line, time prefix from the story clock, folder path, fixed sections, tool prefix. The session interfaces use a second prompt builder with more fixed text. | 4 to 12 |
| A session driver on the WebSocket or daemon interface. The ACP interface excludes memory. | 5 to 12 |
| Two histories, memory and hidden model calls: ZeroClaw drops old turns, saves memory, and can make extra model calls to consolidate memory. Each needs a rule for the clock, the budget and replay. | 8 to 25 |
| Save and export every model request, because it no longer equals our saved messages | 2 to 5 |
| Realism tests on all text that the model reads | 2 to 6 |
| ZeroClaw state outside PostgreSQL: rule exceptions, a folder for each run that survives a resume | 1 to 4 |
| Option A items that still apply | 6 to 15 |
| **Total** | **about 30 to 80, most likely about 50, plus 2 to 4 days of patch maintenance for each ZeroClaw release** |

Open risk in Option C: it is not certain that ZeroClaw's own history can meet the replay rule
(R6). After a crash, its saved history and ours can differ. This was not tested.

### The strongest argument for ZeroClaw

A widely used assistant program, with its own prompt, persona and memory, is a more realistic
subject than our own loop. The runtime could then be an experimental factor: the same story on
two runtimes. That is a research question, and Options B and C serve it. Option A does not,
because in Option A the model cannot tell which runtime is in use.

## Limits of this trial

- Scripted models only. No real model has used the bridge. Provider errors, cut-off replies and
  a budget stop went only through the shared code, not through the bridge with a real provider.
- One ZeroClaw version, 0.8.7, on one machine.
- The longest story tested is the three-owner control story of six story hours.
- History is saved in full. A week-long run would exceed the model's context without summaries.
- Model readers and the author read parts of the ZeroClaw source. No one did an audit of all
  of it. Most source facts are from `master`, not from the 0.8.7 tag.
- A model review of the adapter found faults, and the serious ones are corrected and tested.
  These are known and not corrected:
  - A model request that waits more than 3,600 s behind a held call makes ZeroClaw stop. The
    harness then records a failed attempt, although no work failed.
  - A second call of one model reply can be recorded as "never arrived" if the first call waits
    more than 600 s.
  - A crash of the harness process during an activation was not tested on this backend.
- The trial class uses private methods of `PersonalAgent`. A merged backend needs a shared base.
- Migration `022` must not reach the live database while the mini runs `event-clock` code,
  because that code refuses a database with a migration that it does not have. This branch was
  run only against the test database.

## How to run it

On the Mac mini, in the branch worktree `~/Desktop/BOLD/Chaotic_Neutral-zeroclaw-agent`:

```bash
export AGENTRPG_ZEROCLAW="$HOME/Library/Application Support/agentRPG/zeroclaw/0.8.7/zeroclaw"
```

```bash
PYTHONPATH=$PWD/src env -u OPENROUTER_API_KEY ../Chaotic_Neutral/.venv/bin/python scripts/test_local.py tests/test_zeroclaw_agent.py
```

```bash
../Chaotic_Neutral/.venv/bin/python scripts/probe_zeroclaw.py --out /tmp/zeroclaw-report.json
```

The probe needs no database and no key. It writes every request that ZeroClaw made.
