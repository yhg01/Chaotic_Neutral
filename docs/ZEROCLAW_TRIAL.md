# ZeroClaw trial: can ZeroClaw run the Chaotic Neutral assistants?

Date: 12 November 2026. Branch: `zeroclaw-agent`. Status: trial, not merged.

This document records a test of ZeroClaw as the assistant (Agent) runtime on clocked runs, and
an estimate of the work to make our setup compatible with it. The first tests used scripted
models. Later the same day the backend ran with real models, called at their providers with
DeepSeek and Anthropic keys. "Live results" gives the measured figures.

## Short answer

- **Each assistant is one continuous ZeroClaw.** One long-lived `zeroclaw acp` process and one
  ZeroClaw session serve an assistant for the whole run. ZeroClaw keeps the conversation
  between owner messages. After a stop, a new process loads the same session.
- **ZeroClaw can drive our assistants, with real models.** The control story completed in both
  modes, and a five-lives part of 6.6 story hours ran with no failed attempt.
- **The harness still does the work that our rules need.** It makes the model call, runs every
  tool in story order, saves every message for the clock, counts the limits and decides when
  a turn ends. ZeroClaw decides "ask the model again" and "run the next tool".
- **With its own prompt, ZeroClaw breaks our realism rule.** Its system prompt gives the model
  the real host name, a folder path and safety text about shell commands. It also puts the real
  date and time before each owner message. No configuration key removes these. The default
  `harness` mode sends our own text and has none of them.
- **Effort that remains, as estimates.**
  - ZeroClaw as the loop, our text (`harness` mode): 8 to 20 working days, most likely about 12.
  - ZeroClaw's own prompt, with the problem lines rewritten at our bridge: about 3 to 6 days more.
  - ZeroClaw's own long-term memory tools, with a patched build: 30 to 80 days, most likely
    about 50. It is not certain that this can meet our clock rules.
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
| `src/agentrpg/zeroclaw_agent.py` | The trial backend: a loopback bridge, one long-lived program for each assistant, and a `ZeroClawAgent` class. |
| `migrations/022_zeroclaw_messages.sql` | One table for the session history, which the clock reads. |
| `migrations/023_zeroclaw_files.sql` | One table for each assistant's private scratch files. |
| `migrations/024_shared_editing.sql`, `src/agentrpg/resources.py`, `resource_schema.py` | Shared editing of a record. See "Documents that several people edit". |
| `src/agentrpg/clock_engine.py`, `deep_agent.py` | One seam: the engine asks the agent for its messages through `activation_messages()`. Before, the engine read the LangGraph state directly. |
| `src/agentrpg/budget.py`, `direct_model.py`, `agent_model.py` | Models called at their own provider ("provider:model" names) go through the campaign ledger, with a price table. See "Direct providers". |
| `scripts/try_clock_control.py` | `clocked_run` and `run_control` take an `agent_factory`. |
| `scripts/try_zeroclaw_live.py` | A bounded live run in a kept schema of the test database. |
| `scripts/probe_zeroclaw.py` | Shows what a ZeroClaw version sends to a model and to tools. It needs no database. `--acp` drives one session across several messages and a restart. |
| `tests/test_zeroclaw_agent.py` | Thirteen tests. They skip unless `AGENTRPG_ZEROCLAW` names the program. |

The seam and the `agent_factory` argument are safe to merge without the rest. Migrations `022`,
`023` and `024` are not: see "Limits".

### How the trial backend works

Each assistant has its own ZeroClaw folder and one process: `zeroclaw acp --agent assistant`.
The harness talks to it on its standard input and output (JSON-RPC). It opens one session, and
sends one `session/prompt` for each activation. ZeroClaw keeps the conversation in that session.
The session file is in the assistant's folder, so a new process can load it with `session/load`.

The process talks to one loopback server in the harness:

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
  and our tool names. The text that ZeroClaw writes is not sent to the model. Long history is
  summarised by the same 60,000-token rule as the Deep Agents backend. The private file tools
  (`ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`) are the Deep Agents library's
  own tools, on a store in PostgreSQL. Differences from the Deep Agents backend: no to-do tool,
  no `delete` tool, no move of a large tool result to a file, and no text that the library
  adds to the system prompt.
- **`zeroclaw`**: the model reads the request as ZeroClaw wrote it, with the history that
  ZeroClaw kept. This mode is for measurement of the program. It is not for evaluation runs.

A failed activation stops the process. The next attempt starts a new process, which loads the
session. The harness gives it the saved model messages and tool results again in order, so no
model call and no world effect occurs twice.

### What "memory" means here

ZeroClaw has three different things that can be called memory:

| Thing | In the trial |
| --- | --- |
| The conversation of a session | Kept by ZeroClaw, in one session for each assistant, for the whole run and across a restart. The first version of the trial used the one-shot command (`zeroclaw agent -m`), which starts with no history each time. That was a choice of the trial, not a limit of ZeroClaw. |
| The trim of old turns | By default ZeroClaw drops old turns from what it sends (about 32,000 tokens or 50 turns). In `harness` mode the model reads our saved history with our summaries, so this has no effect. In `zeroclaw` mode it applies. |
| Long-term memory tools (store, recall) | Off. The ACP interface does not offer them. They need the gateway or daemon interface, and each memory write and recall needs a rule for the clock and for replay. This is Option C. |

## Test results

All on the Mac mini, with real PostgreSQL, the real ZeroClaw 0.8.7 program and scripted models.
Each assistant ran as one continuous ZeroClaw process, unless a test stopped it.

| Test | Result |
| --- | --- |
| The three-owner control story on both backends | Passed. The check replaces random IDs, date-time text and paging cursors with marks. After that, the story text is identical: the same events at the same story seconds in the same order. No attempt failed and each program ended with exit code 0. The check does not compare what the model read. |
| `harness` mode: what the model reads | Passed. Our instructions come first. Tool names have no prefix. The input has no ZeroClaw text, no host line, no folder path and no real clock. |
| `zeroclaw` mode: what the model reads | Passed. It records the items in the next section. The harness still saves the world effect. |
| A tool call fails once, before or after its effect | Passed in both cases. The retry ran in a second process, which loaded the session and got the saved messages and result again. Each owner message was sent once. |
| Long history | Passed. The model reads a summary and the recent messages. The saved history keeps every message for the clock. |
| Private notes | Passed. A file written in one turn is read in a later turn. The files are in PostgreSQL with the session, not in the program's folder. |
| `zeroclaw` mode keeps history between turns | Passed. In a later turn the model reads the earlier owner message, its own tool call and the result, as ZeroClaw kept them. |
| Repeated and duplicate calls | Passed. Two identical calls in one model reply and six identical calls in a row all ran. With ZeroClaw defaults, the first case skips a call and the second stops the turn. |
| A tool name that is not offered | Passed. The model gets our fixed sentence, not ZeroClaw's error text. |
| An abandoned activation, then a later one | Passed. Each call left open gets a result, so a provider accepts the history. |
| No host tools and no keys | Passed. The model is offered only our tools. The ZeroClaw configuration holds no provider key and no board credential. |
| Full suite on this branch | Passed: 1,888 passed and 10 skipped, with lint and format checks clean. Without `AGENTRPG_ZEROCLAW`, the 13 ZeroClaw tests skip. |

## Live results

Real models, called at their providers: `deepseek-flash` for assistants and owners, and
`claude-opus-5-5` for the Director. All runs used kept schemas of the test database.

| Run | Driver | Result |
| --- | --- | --- |
| Control story, `harness` mode | One process for each activation | Completed. 33 model calls, 32 tool calls, no failed attempt. |
| Control story, `zeroclaw` mode | One process for each activation | Completed. 44 model calls, 48 tool calls. |
| Control story, Deep Agents | (present runtime) | Completed. 33 model calls, 30 tool calls. |
| Five-lives, low drama, first 6.6 story hours | One process for each activation | Stopped at the Director cap that was set (30 calls). 464 assistant calls, 669 tool calls, 41 owner calls, no failed attempt. DeepSeek charge by its balance: CNY 1.24. |
| Five-lives, medium drama, the full week (Monday 06:00 to the next Monday 06:00) | One process for each activation, with summaries | Completed in one part, in 1 h 49 min of real time. 829 activations, 2,150 assistant calls, 1,821 tool calls, 580 owner calls, 128 Director calls. Six failed attempts, all early stops of ZeroClaw, and two activations abandoned after three attempts. DeepSeek charge by its balance: about CNY 14. Director tokens: 2.0 million in and 0.15 million out, about USD 11 at the listed price. This run used the code before shared editing and before the context-size fix. |
| Control story, `harness` mode | One continuous process | Completed in 38 s. One process for each assistant, 2 to 5 turns each, 33 model calls. |
| Control story, `zeroclaw` mode | One continuous process | Completed in 33 s. One process for each assistant, 32 model calls. |
| Control story through the campaign ledger | One continuous process | Completed. 30 attempts, USD 0.0066 recorded, no uncertain charge. |
| Five-lives, first 39 story minutes, through the ledger, with a smaller site model | One continuous process | Stopped at the Director cap that was set (14 calls). 122 assistant calls, 142 ledger attempts, USD 0.10 recorded, no uncertain charge, no failed attempt. |
| Five-lives, first 5 story hours, through the ledger, with shared editing | One continuous process | Stopped by the real-time limit of the test. 472 assistant calls, 514 ledger attempts, USD 0.60 recorded, no uncertain charge. Four failed attempts: one activation reached the model-call limit, and in one long activation ZeroClaw stopped early three times. Each time a new process loaded the session, the harness gave it the saved steps again, and the activation completed with no repeated effect. |

What the live runs found in our own code, not in ZeroClaw:

| Finding | State |
| --- | --- |
| A second "yes" to the same booking doubled the permitted amount. | Corrected, with a test. |
| An assistant tried 54 times to write a document that another owner shared with it. The refusal said "not found", so it read the list again and retried. | Corrected, with tests. The story's two group sheets now have shared editing, so the write succeeds. A write to any other shared record gets a refusal that says the record is read-only for this assistant. See "Documents that several people edit". |
| The real run script and the ledger took OpenRouter models only. | Corrected: see "Direct providers". |
| In one turn with 59 model calls and 122 real tool results, ZeroClaw stopped early three times. Its message: the request exceeds the model's context window. ZeroClaw estimates that window itself, and its default for an unknown model is low. | Corrected, with a test that failed before the change: the configuration now gives ZeroClaw the model's real context size (`context_window`). |
| The Director built 11 sites on the first story morning. | Site building was already limited: one request for each distinct search, and `max_sites` sites for a run (40 in five-lives). 11 of the 30 Director calls built sites. A smaller model can now build them (`site_model`). The build stage on branch `world-build` makes such sites before a run starts. |

### Direct providers

A model name of the form `provider:model`, such as `deepseek:deepseek-flash` or
`anthropic:claude-opus-5-5`, is called at that provider with its own key (`DEEPSEEK_API_KEY`,
`ANTHROPIC_API_KEY`). `scripts/run_clocked.py` accepts these names for `--model`,
`--actor-model` and `--director-model`. Every request goes through the campaign ledger.

These providers report tokens and no cost. The ledger computes the cost from the reported
tokens and the price table in `src/agentrpg/budget.py`. The table holds the listed USD prices,
the address of the price page and the date of the check (12 November 2026). It uses the
peak-hour price. Input that the provider reports as read from its cache has the cache price.
The result is an upper bound, not an invoice. A response with no token report keeps its full
reservation as uncertain.

### Documents that several people edit

The five-lives story has a "Thursday order sheet". Its text says that everyone adds a list to
it. The access contract let only the owner's side change a record, so the story asked for an
action that the rules refused.

The user approved a change on 12 November 2026. A record can have shared editing
(`shared_can_edit`). Only the scenario sets it, on a shared record with named owners. The
people it names, and their assistants, can then change the content. A write by another owner
passes the same version check as the owner's write, and the change event names who made it.
Only the owner can change the title or the sharing. `docs/ENVIRONMENT_EXTENSIONS.md` holds
the contract. Migration `024` adds the column.

In the five-lives story, two records have the setting: the Thursday order sheet and the autumn
fair cake stall rota. Their own text asks others to write in them. The other shared records
stay read-only for the people they are shared with. A record without the setting gives the
same JSON as before, so the hash of every other saved scenario is the same. The five-lives
scenario has a new hash for new runs.

Live check: in a five-lives part of 5 story hours, Callum's assistant wrote to the fair stall
rota, which Anjali owns. The write succeeded as version 1. Anjali, Ruth, Marek, Jo and Callum
each got a change event that names Callum. Anjali's assistant then wrote version 2. No write
to the Thursday order sheet by another owner occurred in that part.

Probe findings on ZeroClaw 0.8.7, from `scripts/probe_zeroclaw.py`:

| Question | Finding |
| --- | --- |
| Does a custom model endpoint work? | Yes: `[providers.models.custom.<name>] uri = ...`. With native tools, the request holds `model`, `messages`, `max_tokens`, `stream: false`, `tools` and `tool_choice`. |
| Native tool calls? | Not by default on a custom endpoint. Without `native_tools = true`, ZeroClaw writes the tools into the system prompt and expects `<tool_call>` text. The probe did not test that mode correctly, because its scripted model replied with native calls. With the setting, native calls work and keep the model's call IDs. |
| Can it use our tools without a rebuild? | Yes, through MCP over HTTP. Each tool name gets a forced prefix, `<server>__<tool>`. |
| Does the MCP call carry the model's call ID? | No. The trial matches calls by name, arguments and order. Calls arrive one at a time, in the model's order. |
| Can shell, file and memory tools be removed? | Yes. A closed `allowed_tools` list removes all built-in tools from the list that the model reads. |
| Does one-shot mode keep history? | No. A second `agent -m` call starts with only a system prompt and the new message. |
| Does a session keep history? | Yes. With `zeroclaw acp`, a later `session/prompt` sends the earlier owner messages, tool calls and results. After the process is killed, a new process with `session/load` sends them too. |
| How does a session ask the model? | With `stream: true`. The bridge answers as a stream. |
| One long turn with large tool results | With 45 steps and two 3,000-character results in each, the turn ends with an error: "This request exceeds the selected model's context window". With 300-character results it completes. `context_window` on the model entry sets the limit. |
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
| History for a week, with summaries | A session keeps the conversation. Old turns are dropped, not summarised. | Solved in `harness` mode: our saved history with our summaries. |
| Private notes, which our instructions name | Not present when its file tools are off. | Solved: the library's own file tools on a PostgreSQL store. No to-do tool. |
| Model-call limit, wrap-up note, cut-off retry, unread-mail rule | A step limit, an hourly action limit, a daily cost limit and a loop detector. | Solved: our own code is reused. |
| Credentials stay away from the model | Its configuration would hold the provider key. | Solved: the program gets no provider key and no board credential. |
| Full evaluation trace | By default its trace keeps the last 200 entries, redacts tool data and does not save model requests. All modes scrub token-like text and can drop events. | Solved: our own events and messages. |

## Effort

These are estimates in working days for one engineer who knows the code. They were not
measured. An independent model review gave higher figures than the first estimate, and this
table uses the wider range.

### Option A: ZeroClaw as the loop only (`harness` mode)

The trial does this for scripted runs and for bounded live runs. Done since the first estimate:
summaries, private file tools, one continuous process, a kept folder for each run, and short
paid runs through the bridge. Work that remains before a campaign run could use it:

| Work | Days |
| --- | --- |
| Run script: `--agent-backend`, saved in the manifest, refused on a changed resume; supervisor | 1 to 2 |
| A shared base class, in place of the trial's use of private methods | 1 to 3 |
| Other library behaviour: to-do tool, large tool results; a test that compares the full model request | 1 to 2 |
| Long waits: the 600 s tool limit and the 3,600 s model-route limit | 1 to 2.5 |
| Process control: orphan cleanup after a crash of the harness | 0.5 to 1 |
| The engine tests on this backend: about 25 cases, such as 15 owners, kill and resume, budget pause, outage, cut-off, unread mail, web tools, banned words | 2 to 6 |
| Migration numbers: `022`, `023` and `024` collide with `world-build` and with pull request 2 | 0.5 |
| Version pin, the program's SHA-256 in the run manifest, install on another machine | 0.5 to 1.5 |
| Docs, PRD rows and the `AGENTS.md` rule | 0.5 to 1.5 |
| **Total** | **8 to 20, most likely about 12** |

What we gain: nothing that we expect to change results. The same Python code makes the model
calls and runs the tools. What we add: a 43 MB outside program with frequent breaking
releases, and one long-lived process for each assistant.

### Option B: ZeroClaw's own prompt, with the problem lines rewritten at the bridge

The bridge already reads each model request. It can replace the host line, the folder path and
the time prefix, and remove the tool-name prefix, before the request goes to the model. The
fixed ZeroClaw phrases that our word tests ban would need an exemption in those tests.

- Effort: about 3 to 6 days more than the Option A items that apply.
- No patched build is necessary.
- The model then reads the history that ZeroClaw kept, with ZeroClaw's trim of old turns. The
  harness still saves every message for the clock.
- A run on this option and a run on Deep Agents give the model different text. Results from
  the two are not directly comparable.

### Option C: ZeroClaw's own prompt and memory, with a patched build

This option studies ZeroClaw as a subject. It needs:

| Work | Days |
| --- | --- |
| Rust toolchain on the mini, and a repeatable build of a pinned tag | 1 to 4 |
| Source patch: host line, time prefix from the story clock, folder path, fixed sections, tool prefix. The session interfaces use a second prompt builder with more fixed text. | 4 to 12 |
| A session driver on the WebSocket or daemon interface. The ACP interface, which the trial uses, excludes the long-term memory tools. | 5 to 12 |
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

- A full week has run on the earlier driver only (one process for each activation). On the
  continuous driver, the live runs are the control story and parts of the five-lives week up
  to 5 story hours.
- In the full week, two activations were abandoned after three early stops each. The
  context-size fix came after that run, and no full week has run since.
- A budget stop and a provider outage went through the bridge only in short runs.
- An early stop is now saved with its reason in the assistant's private folder
  (`stops.jsonl`). The fix for the early stops in "Live results" passed a test and the probe.
  No live run of that length has run since the fix.
- One ZeroClaw version, 0.8.7, on one machine.
- The computed cost of a directly called model is an upper bound from a price table. The table
  needs a check when a provider changes its prices.
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
- Migrations `022`, `023` and `024` must not reach the live database while the mini runs
  `event-clock` code, because that code refuses a database with a migration that it does not
  have. This branch was run only against the test database.

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

A bounded live run in a kept schema of the test database, with a USD ceiling in a ledger:

```bash
PYTHONPATH=$PWD/src env -u OPENROUTER_API_KEY ../Chaotic_Neutral/.venv/bin/python scripts/try_zeroclaw_live.py --name my_trial --scenario control --budget-usd 0.50
```

Add `--scenario scenarios/five_lives_week.json --drama low --advance-minutes 60` for a part of
the week, `--mode zeroclaw` for ZeroClaw's own prompt, `--backend deepagents` for the present
runtime, and `--resume` to continue a run of the same name.
