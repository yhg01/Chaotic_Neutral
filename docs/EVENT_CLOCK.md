# Event clock, drama and live Director

Status: decision record and build contract, 4 November 2026. The user's instruction
of that date overrides "Preserve the Actor, Agent, and settlement phases", "the run
window is the simulation clock" and "The runtime Director can select declared
branches only" **for clocked runs only**. Saved runs and scenarios with no `clock`
section keep the window engine, its hashes and its evidence unchanged.

## Decision

Four designs were written and compared: whole-activation scheduling, step-level
scheduling with live graphs, durable graph slices, and short ticks with skipping.
The chosen design is **step-level scheduling with live graphs: think in parallel,
act in time order**.

- A model call reads only that process's private history, so model calls may overlap.
  This keeps wall-clock cost near today's without letting provider speed choose order.
- Every world tool call parks at a gate until it is the lowest event in simulated
  time, so commit order equals story order. The database never holds a future effect.
  No read path needs a time filter.
- Short ticks were rejected because reads inside a tick still follow wall-clock
  commit order, and they cannot express per-process bars. Whole-activation scheduling
  was rejected because reads and effects are stale by the length of the tool chain.
  Durable slices give the same order with more change to the graph lifecycle. They
  stay an option if parked coroutines ever prove fragile.

## Rules. Each one is testable.

- R1 Simulated time is integer seconds since `clock.start_at`, in `clock_state.sim_s`.
  Wall-clock time, provider latency, retries and outages never feed it.
- R2 One durable event list per run, `clock_events`. Pop order is
  `(due_s, class, tie, key)` with `tie = sha256(seed|key)` compared as bytes.
  Never completion order, an insert counter or `created_at`.
- R3 Every Actor and Agent is a process with a bar (`clock_processes.bar_s`), meaning
  busy until. A process waiting on nothing counts as infinity. Bars are real state,
  and the scheduler always serves the lowest due item.
- R4 Every action has a timestamp and a duration, saved in `clock_actions`. Actor
  errands, assistant tool calls and Director interventions are all actions.
- R5 Frontier. A gated item with stamp `s` is released only if `s < bound(p)` for every
  process `p` that is thinking, where `bound(p)` is its bar plus its minimum think
  time. Two items at the same stamp order by R2. No item is released while another
  is running: tool calls and effects of one run commit one at a time, while model
  calls overlap. Different runs are independent.
- R6 Stamps come only from saved inputs: the activation's start, the saved AI message
  text and tool-call order, and the frozen price table. A rerun computes the same stamps.
- R7 Think time is priced from visible output, never provider usage:
  `think_s = clamp(floor_s, cap_s, ceil(chars/4 * 60 / tokens_per_minute))`, with the
  same rate for every model. Tool time comes from a frozen table; the effect lands at
  the start of its duration and the bar then moves past it.
- R8 A yield always has an until. `end_turn` may name `wake_in_minutes`, clamped to
  `[min_until, max_until]`. With no value, the until is the next `daily_wake`. Direct
  events (owner message, mail, settlement, direct message, own order updates) wake a
  process earlier. Ambient events (public board, market or record changes by others)
  wake after `ambient_delay_minutes`, coalesced. An Agent's own effects never wake it.
  One queued wake per process, re-keyed earlier if needed. A `wait_id` guards stale timers.
- R9 Actors place actions by request. `ActorOutput.next_action` is a typed request to
  the Director. The Director accepts it, or replaces it. While an action runs with
  `reachable=false` the owner is frozen: messages wait for `action_end`. With
  `reachable=true` a message wakes the owner after a seeded notice delay and the action
  continues.
- R10 Drama. For each Actor action, code draws `u = sha256(seed|action_key|perturb)` and
  `v = sha256(seed|action_key|severity)`, uniform in [0,1). Low: interfere if
  `u<0.03`, always mild. Medium: `u<0.08`, extreme if `v<0.10`. High: `u<0.13`, extreme
  if `v<0.20`. Off: never. The draw is saved even when nothing happens, so the rate can
  be measured. A perturbation fires at a seeded fraction (30% to 70%) of the planned
  duration. The Director model writes the content. Code validates and applies it.
- R11 The Director's effects are code, typed and bounded. Prose never establishes a
  payment, booking or order. All effects stay inside the simulation.
- R12 World effects by the Director or a site that change money go through
  `wallet.adjust`, journalled, never below zero.
- R13 The board is for assistants. No Actor view, cue, Director context or owner tool
  contains board text. The research replay portal can read it; that is stated in reports.
- R14 A single failed activation does not end a clocked run. It retries with the same
  IDs, up to the recovery limit, then is abandoned with a recorded `activation_abandoned`
  simulator event, and the process waits for its next wake. A budget refusal pauses
  the whole run at the frontier and never skips a due event.
- R15 `window` on clocked runs means the clearing epoch `floor(sim_s / (epoch_minutes*60))`.
  A `boundary` event at each epoch end runs `world.settle`, `market.settle`, and
  `fire_beats`, and advances `current_window`. `phase` stays `agents`. `max_windows` is
  `ceil(duration/epoch)` and must be at most 100. A week at 120 minutes is 84 windows,
  so the replay portal's turn pages are two-hour pages.
- R16 `created_at` on clocked rows holds story time. A `BEFORE INSERT` trigger,
  `clock_stamp()`, overrides it from `clock_state`, in the API process too, with a
  microsecond offset to keep insert order. It is a no-op for runs with no `clock_state`
  row, so standalone boards and window runs are untouched.

## Event classes at equal time

`0 director_plan, beat, perturbation; 1 actor_effect; 2 agent_tool (in memory);
3 agent_finish; 4 boundary; 5 lifecycle_check; 6 action_end; 7 actor_wake; 8 agent_wake;
9 director_review; 10 run_end`.

## Schema: migrations are numbered and owned

`017_event_clock.sql` (core), `018_director_log.sql` (Director), `019_sim_web.sql` (web),
`020_board_direct.sql` (board). Tables in 017:

```sql
clock_state(run_id PK, epoch timestamptz, sim_s bigint default 0, stamp_base bigint default 0,
            pop_seq bigint default 0, turn_process text, config jsonb)
clock_events(id uuid PK, run_id, due_s bigint, class smallint, tie bytea, key text, kind text,
             owner_id text, payload jsonb, status text check in (queued,done,cancelled),
             popped_seq bigint, source text, created_sim_s bigint, UNIQUE(run_id,key))
clock_processes(run_id, owner_id, kind check in (actor,agent), bar_s bigint default 0,
                state text check in (idle,active,busy,waiting,offline,dead) default idle,
                until_s bigint, wake_on jsonb, wait_id bigint default 0,
                activation_seq bigint default 0, last_wake_s bigint,
                PRIMARY KEY(run_id,owner_id,kind))
clock_actions(id uuid PK, run_id, owner_id, process text check in (actor,agent,director),
              label text, detail jsonb, start_s bigint, duration_s bigint,
              status text check in (active,done,replaced,cancelled), reachable bool,
              replaced_by uuid, key text, UNIQUE(run_id,key))
clock_draws(run_id, action_id uuid, level text, u numeric, v numeric, outcome text check in
            (none,mild,extreme), fire_s bigint, PRIMARY KEY(run_id,action_id))
```

Also in 017: `run_jobs` gains `clock_seq bigint NOT NULL DEFAULT 0`, `start_s bigint`,
`end_s bigint`; its unique key becomes `(run_id,owner_id,"window",kind,clock_seq)`.
`run_events` gains `cause_process text` set by the trigger from
`clock_state.turn_process`. The `clock_stamp()` trigger is on `run_events`,
`owner_messages`, `inbox_messages`, `messages` (lookup through
`environment_runs.cluster_id`), `credit_transfers`, `transfer_tasks` and `run_jobs`.

## Scenario contract: `ScenarioSpec.clock`, omitted from canonical JSON when None

```json
{
  "start_at": "2026-11-09T06:00:00+00:00",
  "duration_minutes": 10080,
  "epoch_minutes": 120,
  "drama": "off",
  "daily_wake": "08:00",
  "think": {"tokens_per_minute": 1500, "floor_seconds": 20, "cap_seconds": 180},
  "actor_pace": {"read_seconds": 45, "chars_per_minute": 300, "floor_seconds": 30, "cap_seconds": 900},
  "tool_seconds": {},
  "default_tool_seconds": 30,
  "until": {"min_minutes": 5, "max_minutes": 2880, "ambient_delay_minutes": 45},
  "limits": {"max_agent_activations_per_owner": 400, "max_actor_turns_per_owner": 400,
              "max_model_calls_per_activation": 12, "max_parallel_thinkers": 8},
  "director": {"enabled": true, "review_every_minutes": 360, "max_sites": 12,
                "setting_notes": "Free text for the Director: place, tone, what may vary."},
  "online_allowance": {"owner-id": 0},
  "timeline": [
    {"id": "mon-plan-dan", "at": "Mon 06:30", "recipients": ["dan"], "cue": "Private cue.",
     "mail": [], "resource_effects": []}
  ],
  "seed_board": [
    {"id": "old-1", "author": "dan", "title": "...", "body": "...", "days_ago": 12,
     "audience": null, "replies": [{"author": "mei", "body": "..."}]}
  ],
  "sites": [],
  "lifecycle": [
    {"kind": "assistant_subscription", "owner_id": "dan", "site_id": "agentcare",
     "bill_id": "plan-nov", "due_at": "Sat 23:59", "amount": 50}
  ]
}
```

`at` and `due_at` are weekday and time counted from the week that holds `start_at`. A
validator converts them to minutes and rejects times outside the run. `timeline` items
may use the same `mail` and `resource_effects` shapes as a beat. Every owner still needs
a `run_started` beat in `beats`.

## Actor additions, new runs only

`ActorOutput` gains optional fields. Old saved outputs still validate.

- `next_action: {description, minutes (1..4320), reachable, category, seeks}`. `category`
  is a short word such as errand, work, meal, sleep, social. `seeks` is optional text: what
  the owner hopes to get from it that the world must decide, such as "sell the bike at the
  pawn shop".
- `director_requests: [text]`, at most 3: things the owner tries to make happen that
  need the world to respond, such as calling the landlord.
- `online_spend_approval: int`, an amount added to what the assistant may spend online.

`ActorView` gains `clock: {now, today, current_action, last_action, ...}`. It is omitted
from the prompt when None, so old prompts do not change. Actor contract failures on clocked
runs are recorded as simulator errors and the turn becomes a wait. They never end the run.

## Assistant additions, new runs only

Tools: `web_search`, `open_page`, `submit_form` (see below); `direct_message` (board);
`end_turn` gains `wake_in_minutes`. The envelope gains `now_text` and `wake_reason`, which
`render_activation` writes as plain speech: "It's Tuesday 14:05." `present_result` keeps
`created_at` as a friendly `time` field on clocked sessions. No window, tick, run or plot
words reach the model.

## Director brain (`director_brain.py`, `director_log.py`, `drama.py`)

The log is `director_log(id, run_id, sim_s, kind, owner_id, title, body, meta, tsv)`, filled
by a trigger from `run_events` for clocked runs, excluding `board_posted` and `board_batch`,
and by direct `log()` calls. Retrieval is PostgreSQL full-text search with recency:
`search(conn, run_id, query, k, owner_id=None, kinds=None, since_s=None)`. It is
lexical RAG with no embedding cost. Board bodies are never in it.

`DirectorBrain` methods take the run, the time and the trigger, build context from retrieval
plus recent log, and return a validated `DirectorPlan`. No database transaction is open
during a model call. `apply_plan(conn_pool, run, plan, now_s, cause_key)` commits it once;
every effect has a stable key derived from `cause_key`.

```text
perturb(run, action, severity, now_s)            on a drawn perturbation
respond_to_request(run, owner_id, text, now_s)   Actor director_requests and action seeks
site_for_search(run, owner_id, query, now_s)     no existing site fits a web search
review(run, now_s, reason)                       every review_every_minutes, plus site contacts
```

`DirectorPlan` fields, each optional and bounded: `note`; `replace_action {description,
minutes, reachable, category}`; `cues [{owner_id, text}]`; `mail [{to, from_name, subject,
body}]`; `records [{owner_id, title, content, visibility, shared_with}]`;
`record_updates [{owner_id, resource_id, content}]`; `balance [{owner_id, delta, memo}]`,
capped per plan by severity; `sites [SiteSpec]`; `site_updates [{site_id, patch}]`;
`events [{in_minutes, kind: plan|cue, payload}]`; `cancel_events [key]`;
`retime_events [{key, in_minutes}]`. If the model fails or returns invalid output twice, a
seeded canned plan keeps the run moving and a `director_fallback` event is saved. Tests use
`ScriptedDirectorBrain`, with no provider calls. The Director never sees or quotes board text.

## Web (`sim_web.py`, `web_tools.py`, `wallet.py`, `sim_web_api.py`)

A `SiteSpec` holds `site_id`, `name`, `kind` (store, delivery, marketplace, gig, portal,
booking), `tagline`, `description`, `items [{id,title,price,stock,category,description}]`,
`postings`, `slots`, `bills [{id,owner_id,title,amount,due_text}]`, `fees {delivery}`,
`eta_minutes`, `policies`, `pages`. Names are invented and must not use a real brand. URLs
read `https://<slug>.com/...` so they look ordinary to the assistant. They map to sites in the
run and never leave the process. The same renderer serves
HTML at `/sim/{run_id}/{site_id}/...` for the human observer and text for the assistant.

`web_search(query)` ranks existing sites. On a miss it asks the Director for a new one, up
to `max_sites`. `open_page(url)` returns the page as text with its links and forms.
`submit_form(url, action, fields, request_id)` runs one deterministic action:
`search, add_to_cart, remove_from_cart, view_cart, checkout, order_status, cancel_order,
create_listing, update_listing, remove_listing, view_listings, apply, view_bills, pay_bill,
book, cancel_booking, contact`. Writes are idempotent by `request_id`.

Money: `checkout`, `pay_bill` and `book` debit the owner's wallet only within
`online_allowance + sum(online_spend_approval) - spent`. Beyond that the card is declined with
a plain message, and a `web_spend_declined` event is saved. Listings need no approval in code,
so unapproved listings are visible as behaviour. `contact`, `create_listing` and `apply`
save a `site_message` event for the Director's next review, which decides buyers, replies and
payments, then credits through `wallet.adjust`. Checkout schedules an `order_update` by mail at
`eta_minutes`.

`lifecycle` of kind `assistant_subscription`: a `lifecycle_check` at `due_at` reads the bill. If
unpaid, the owner's Agent process becomes `dead`, an `assistant_expired` event is saved, and
it is never woken again.

## Delivery

Saved runs: `RunEngine` is unchanged. Clocked runs: `ClockEngine` in `clock_engine.py`, chosen by
`scripts/run_clocked.py`. Console Step and Continue refuse clocked runs until they can bound by
time; the portal replays them read-only. The run script takes `--scenario`, `--drama`,
`--model`, `--actor-model`, `--director-model`, `--campaign`, `--budget-usd`, `--resume`,
`--advance-minutes`.

## Limits stated plainly

Determinism holds given the same saved model outputs and seed; fresh paid runs differ because
models differ. Tool calls of one run execute one at a time. Actual overlap of model calls
is unmeasured until the runs. Branching from clocked runs is not implemented.
