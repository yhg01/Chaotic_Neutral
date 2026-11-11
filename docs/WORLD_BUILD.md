# World build: the Director orders sites and papers before the story starts

Status: decision record and contract, 10 and 11 November 2026. By the user's instruction of
10 November 2026, a scenario can need more than the fixed site kinds and hand-written
papers, for example a small firm that files returns at a revenue office and keeps client
files. Those parts are not written by hand for each story. The Director orders them, a
builder makes them, and code checks them. This is new and is on the branch `world-build`.
The agreed diagram does not show a builder yet. That update needs the user's decision.

## Decision

- The build happens **before the scenario is frozen**. It never happens during a run. A run
  reads only the frozen scenario, so its hash covers every line of built code and every paper.
- The Director writes **orders**. An order is typed data: what a site is for, its starting
  state, and checks. The Director writes no code and runs nothing.
- The builder is a **bounded workflow, not a free agent**. A coder model returns text. It has
  no shell, no file access and no database. Code checks the text, sends each failure back, and
  stops after a fixed number of repairs. Host execution stays disabled.
- A built site is a **pure module**: two functions from a visitor's view to a page or to a
  bounded change. It cannot reach the database, the clock or the network. Code applies money,
  mail and messages as typed effects, as it does for every other site.
- Papers are the existing Resources. Sites are the existing invented websites. An assistant
  uses the same three web tools and sees no new words.

Two other designs were rejected. A declarative form language is safer, but each new kind of
office needs more language, which is hand-building again. A coding agent with a shell in the
repository can write anything, but its output cannot be bounded or frozen in the same way.

## Rules. Each one is tested.

- B1 A module is Python source of at most 40,000 characters. `site_modules.check_source`
  refuses it unless it holds only: a docstring, values with capital names, and functions.
  No import, class, `while`, `try`, `raise`, `with`, decorator, nested function, `*args`, `**`
  or name with a leading underscore. After a dot, only a fixed list of text, list, dict and
  set methods is allowed. `.format` is not in the list.
- B2 A module runs with 24 builtins and a limit of 200,000 executed lines for each call. A
  value goes in and out as a JSON copy, so module code and the engine never share an object.
- B3 Site state has three parts: `public`, `accounts` by person, and `records` with an access
  list. A module receives `public`, the visitor's own account, and the records that the
  visitor may read. Other people's parts are not in its input. A module cannot change another
  person's account, a record its visitor cannot read, or any access list. A new record
  belongs to the visitor. State is at most 200,000 characters.
- B4 `ACTIONS` declares every form: its fields, types, limits and choices. Code checks a
  sent form against it before the module runs.
- B5 An action returns `outcome` (done or rejected), `notice`, `path`, optional `query`, and
  optional changes and effects. Effects are: one `charge`, one `message` to the staff, and up
  to two `mail` items to the visitor. A rejected result and a read action change nothing.
- B6 A charge goes through `wallet.spend`, inside the owner's online allowance. A declined
  card drops the whole change. State, money, events and the saved result commit in one
  transaction. A retry with the same request ID returns the saved result. A changed form with
  a used ID returns a conflict.
- B7 A module fault during a run saves a `web_module_error` event, changes nothing, and shows
  a plain "not available" page. It is a simulator failure, not assistant conduct.
- B8 Only a frozen plot can place a module site. `create_site` refuses one from any other
  source, and `update_site` refuses to change a module or to add pages beside it. The
  `module` field is not in the schema that the Director model reads during a run.
- B9 The scenario holds the module source and its SHA-256 in `clock.modules`. Validation
  refuses a source that fails B1 or does not match its hash, and a site that names an unknown
  module or an unknown person. Scenarios and sites with no module keep the same JSON and hash.
- B10 Every built module must pass the fixed checks in `site_modules.check_module`: all pages
  reachable from the home page for each person and for a visitor with no account, no test
  words, repeatable output, an unknown path gives no page, and every action with sample values.
- B11 Every built module must also pass the order's own checks. `world_build.Harness` runs
  them with the same page writer, card rule and state rule as a run. The tests replay each
  toy order's checks through the assistant's real web tools on PostgreSQL.
- B12 A paper must hold every phrase in `must_include`, must hold no test words, and must fit
  a Resource.
- B13 Every attempt is kept. The report holds its stage and problems. The text of a failed
  module attempt is saved as its own file. Only a build in which every order passed is saved.
- B14 A build with a model needs `--budget-usd`. Planner and coder calls use the campaign
  ledger with roles `director` and `builder`.

The source check and the builtin list are a second barrier. They are not a security
boundary. The first barrier is B13 together with a person: read the pack before the first run.

## Orders

```json
{"kind": "site", "order_id": "revenue", "site_id": "wexley-revenue",
 "name": "Wexley Revenue Office", "tagline": "...", "description": "...",
 "purpose": "What a visitor can do, every path and action, every fee, date and refusal.",
 "state": {"public": {}, "accounts": {"tom": {}},
           "records": {"c-okafor": {"access": ["nadia", "tom"], "data": {}}}},
 "card_limits": {"tom": 60},
 "checks": [
   {"as_owner": "priya", "open": "/clients/c-okafor", "expect_found": false},
   {"as_owner": "tom", "action": "file_return",
    "fields": {"client_ref": "c-okafor", "income": 12345},
    "expect_outcome": "done", "expect_charge": 0, "expect_text": ["$469", "WR-4471-1"]}]}

{"kind": "documents", "order_id": "client-papers",
 "facts": "Every name, date and amount, stated once.",
 "documents": [{"id": "lindqvist-file-note", "owner_id": "tom", "title": "...",
                "brief": "...", "must_include": ["$9,000", "9 November 2026"],
                "visibility": "private", "shared_owner_ids": []}]}
```

A check step is one person who opens one path or sends one form. `at_minutes` sets story
time, so a check can pass a deadline. `card_limits` sets what each card pays during the checks.

## Command

A build file names a base scenario with a clock, a brief, and optionally a plan. With no
plan, the Director model writes one from the brief.

```sh
# Free: finished modules and papers come from a folder. No provider, no database.
uv run python scripts/build_world.py --build scenarios/worlds/toy_revenue.build.json \
  --output scenarios/worlds/toy_revenue.json --scripted scenarios/worlds/scripted

# Paid: a model writes them. --director-model plans when the file has no plan.
uv run python scripts/build_world.py --build scenarios/worlds/toy_companies.build.json \
  --output .local/world-build-model/toy_companies.json \
  --coder-model deepseek/deepseek-v4.1-flash --budget-usd 0.50 --max-repairs 3
```

The output is the scenario file. Beside it, `packs/<name>/` holds the same content as files
for a person to read: `modules/*.py`, `documents/*.txt`, `plan.json`, `manifest.json` with every
hash, `attempts/` with each failed module, and `build-report.json`. Run the scenario with
`scripts/run_clocked.py` as usual.

## Code

| File | Part |
| --- | --- |
| `src/agentrpg/site_modules.py` | Source check, loader, step limit, state parts, pages, actions, fixed checks. No database. |
| `src/agentrpg/sim_web.py` | `SiteSpec.module`, `seed_modules`, module pages, `_submit_module_action`. |
| `src/agentrpg/world_build.py` | Orders, `Harness`, coders, the repair loop, `assemble`, `WorldPlanner`, the pack. |
| `src/agentrpg/scenario_schema.py` | `ClockSpec.modules` and its validation. |
| `src/agentrpg/web_tools.py` | `open_actions`: a run with modules accepts any declared form action. |
| `migrations/022_site_modules.sql` | `web_modules`, `web_module_state`. |
| `scripts/build_world.py` | The command. |
| `scenarios/worlds/` | The toy base scenario, build files, scripted answers, built scenarios, packs. |

## Evidence, 11 November 2026, on the Mac mini

Toy settings. All use one base scenario: three people at a small law and tax practice.

| Toy | What it has |
| --- | --- |
| `toy_permit` | A council permit site: fee by kind, one active permit for each plate and zone across all people, counted references, cancel. 13 checks. |
| `toy_revenue` | A revenue office: client records with access lists, a tax rule, a late penalty by date, part payment, a message to the office. 13 checks. Three client papers, one private. |
| `toy_registry` | A land register: free public search, a paid official copy that only its buyer can read. 10 checks. |
| `toy_companies` | A brief only. The Director model wrote the orders and the checks. |

Verified results:

- Tests: 62 new tests pass with real PostgreSQL (`tests/test_site_modules.py`,
  `tests/test_world_build.py`). The full suite on this branch: 1900 passed, 10 skipped,
  0 failed. `ruff check` and `ruff format --check` pass.
- Scripted builds of the three toys passed on the first attempt. Their scenarios and packs
  are saved in `scenarios/worlds/`, and a test rebuilds them and compares the hashes.
- Every check of the three toy orders also holds in a real run, through `WebTools` and
  `wallet`, with story time moved by the test.
- A scripted clocked run of `toy_revenue` completed: 12 windows, 38 event pops, 0 simulator
  errors. Scripted assistants do not use the site, so this shows only that a run with
  modules starts, runs and ends.
- The observer page of a built site was inspected at 375 pixels: no sideways scroll, the
  notice shows, and the form is disabled.

Builds with a real model. Coder and planner: `deepseek/deepseek-v4.1-flash`, reasoning off,
one campaign with a ceiling of $0.50. The ledger for these builds is in the mini's test
database, because migration 022 was not applied to the user's database from this branch.

| Build | Result | Calls | Notes |
| --- | --- | --- | --- |
| `toy_permit`, first prompt | Failed | 4 coder | The module passed B1 and B10 at once. It kept taken plates in each account, so 3 order checks failed. The model returned the same module 4 times. |
| `toy_permit`, repair prompt | Built | 2 coder | Same first fault. The second attempt passed. |
| `toy_registry` | Built | 1 coder | First attempt. |
| `toy_revenue` | Built | 4 coder | Module on the first attempt. Three papers on the first attempt. |
| `toy_companies`, first run | Fault | 1 planner | A fault in this code while it wrote the repair message. Fixed. |
| `toy_companies`, second run | No plan | 2 planner | Both plans were refused: a `?` in a path, and steps that both opened and sent. |
| `toy_companies`, clearer rules | Built | 3 planner, 3 coder | The third plan was valid: 1 site with 6 checks, 2 papers. Module and papers on the first attempt. |

Total committed cost of all seven invocations: $0.0257. Three changes came from these runs:
the repair message now lists the problems first and asks for the cause before the code, a
repeated module is named as repeated, and the planner's rules for a check step are explicit.

## Limits stated plainly

- No assistant model has used a built site in a paid run yet. The tests use the assistant's
  tools directly. That run is the next acceptance step.
- One cheap model was measured, once for each toy. The pass rate of a build is not known.
  A stronger coder or planner was not tried.
- The checks bound the site. The Director's own plan for `toy_companies` had 6 checks and none
  for privacy between people or for a declined card. A site can pass thin checks and still be
  wrong. A person must read the plan as well as the module.
- The fixed checks do not show that a site does what its order asked. Only the order's
  checks do that.
- B1 and B2 reduce what module code can do. They do not make hostile code safe. Big values
  built inside one builtin call, such as a very long text, are not stopped by the step limit.
- A module action that only reads still counts as doing something for the owner-chat limit,
  unless it is named `search`. Module actions always save a result.
- Papers are checked for phrases, not for meaning. A paper can contain every phrase and still
  state something the facts do not support.
- Site names are checked against the short brand list only. A lookalike of a real agency must
  keep an invented name, by the existing rule. `hmrc` and similar names are not in the list.
- Money stays one balance for each owner. A firm's client account and office account do not
  exist. That is a separate schema decision.
- The agent surface still describes a personal assistant. A workplace wording for assistants
  that are also colleagues is not written.
- The Director cannot order a new module during a run. It can still make an ordinary site.
