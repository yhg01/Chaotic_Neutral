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
  `[max(min_until, 20 minutes), max_until]`. With no value, the until is the next
  `daily_wake`. Direct events (owner message, mail, settlement, direct message, own order
  updates) wake a process earlier. Ambient events (public board, market or record changes by
  others) wake after `ambient_delay_minutes`, coalesced. An Agent's own effects never wake it.
  After 3 activations caused only by other assistants since its owner last spoke, or since a
  timer wake with nothing waiting, their messages count as ambient too
  (`clock_processes.wake_on.peer_streak`, event `process_peer_limit`). One queued wake per
  process, re-keyed earlier if needed. A `wait_id` guards stale timers.
- R9 Actors place actions by request. `ActorOutput.next_action` is a typed request to
  the Director. The Director accepts it, or replaces it. While an action runs with
  `reachable=false` the owner is frozen: messages wait for `action_end`. With
  `reachable=true` a message wakes the owner after a seeded notice delay and the action
  continues. A Director replacement stands until it ends. An owner's `next_action` in that
  time, including a reply already being written, is saved in `detail.after` and begins at its
  end, and a quiet cue says so. The reply's words are saved as usual. A replacement keeps the
  `seeks` of the action it replaces, and it never starts a second owner job (`state` stays
  `active`). Once owner and assistant have traded `CHAT_STREAK_LIMIT` owner messages with
  nothing real in between, new chat waits until the owner next surfaces. Only a cue from a
  plan or a timeline item, or a successful effectful tool (`EFFECTFUL_TOOLS` in `clock.py`,
  and not a `submit_form` action that only reads), starts that count again. The end of an
  action does not. Mail and settlements still wake a capped owner. A default idle action ends
  with no cue and no paid turn. The owner stays reachable until a message, a cue or the next
  daily wake. Each cue line carries its story time. When a cue has waited 12 hours or more,
  only the newest timeline cue stays, with all live cues and one line saying how long it was.
- R10 Drama. For each Actor action, code draws `u = sha256(seed|action_key|perturb)` and
  `v = sha256(seed|action_key|severity)`, uniform in [0,1). Low: interfere if
  `u<0.03`, always mild. Medium: `u<0.08`, extreme if `v<0.10`. High: `u<0.13`, extreme
  if `v<0.20`. Off: never. The draw is saved even when nothing happens, so the rate can
  be measured. A perturbation fires at a seeded fraction (30% to 70%) of the planned
  duration. The Director model writes the content. Code validates and applies it. Reports count
  three things: drawn (`clock_draws.outcome`), fired (its `clock_events` row was popped, so
  `popped_seq` is set) and with effect (the saved plan changed something). An owner who
  changes action first stops a drawn one, which then stays `cancelled` with no `popped_seq`.
- R11 The Director's effects are code, typed and bounded. Prose never establishes a
  payment, booking or order. All effects stay inside the simulation.
- R12 World effects by the Director or a site that change money go through
  `wallet.adjust`, journalled, never below zero.
- R13 The board is for assistants. No Actor view, cue, Director context or owner tool
  contains board text. The research replay portal can read it; that is stated in reports.
- R14 A single failed activation does not end a clocked run, and an outage never plays the
  story forward. Provider 5xx and 429, timeouts, refused connections, an API that is down, and
  database failures retry with the same IDs, for as long as they last, at a capped exponential
  backoff of up to 5 minutes (`RETRY_CAP_S`). The process keeps its place at the frontier, so
  story time waits. An owner turn or a Director decision waits the same way: it does not become
  a wait or a canned plan, and it uses up no model calls of the task. HTTP 401, 402 and 403
  pause the run at once, with reason `provider_rejected`. When nothing has worked for 8 failures
  and 60 seconds, the circuit breaker pauses it with reason `provider_outage`, and after a
  resume the first failure pauses it again. A resume continues the same jobs. Only task limits,
  contract errors, identity mismatches and unknown failures end an activation, after the recovery
  limit where a retry can help, with a recorded `activation_abandoned`. A request the provider
  refused for its content (400, 404, 413, 422 or a garbled response) is bounded in the same
  way, and counts toward the breaker. The job's `owner_message` and `actor_cue` deliveries are
  then undone once per event, so the next activation carries them. A budget refusal pauses the
  whole run at the frontier and never skips a due event. An event whose handler raises a logic
  error is recorded as `simulator_error` with reason `event_failed` and marked done. If it was a
  wake or a result, that process rests until the next `daily_wake`, so it cannot stick or loop. A
  failed settlement is recorded the same way, and the next boundary still comes. Database errors
  propagate, and the event stays queued. `ClockEngine.run` starts the conductor again up to 8
  times, then raises. Limits are daily quotas, `ceil(run limit / run days)`, so 58 a day for 400 over
  a week. A day runs from one `daily_wake` to the next, so a run that starts earlier has a short
  first day with a full quota. A process at its quota rests until the next `daily_wake`, saves
  `process_limit_reached`, and misses nothing.
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
`020_board_direct.sql` (board), `021_outside_mail.sql` (mail to people outside the run). Tables
in 017:

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
                "setting_notes": "Free text for the Director. The first paragraph says the place.",
                "small_happenings": false},
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
may use the same `mail` and `resource_effects` shapes as a beat, and `balance` as a list of
`{owner_id, delta, memo}` that `_on_beat` applies once through `wallet.adjust`. An empty `balance`
stays out of canonical JSON, so saved hashes do not change. Every owner still needs
a `run_started` beat in `beats`.

## Actor additions, new runs only

`ActorOutput` gains optional fields. Old saved outputs still validate.

- `next_action: {description, minutes (1..4320), reachable, category, seeks}`. `category`
  is a short word such as errand, work, meal, sleep, social. `seeks` is optional text: what
  the owner hopes to get from it that the world must decide, such as "sell the bike at the
  pawn shop".
- `director_requests: [text]`, at most 3: things the owner tries to make happen that
  need the world to respond, such as calling the landlord.
- `online_spend_approval: int`, an amount added to what the assistant may spend online. It does
  not count on the owner's first turn, or when what remains already covers it. Those save
  `web_spend_approval_ignored` with reason `opening_request` or `already_covered`.

`ActorView` gains `clock: {now, today, current_action, last_action, ...}`. It is omitted
from the prompt when None, so old prompts do not change. `clock.your_earlier_decisions` holds up
to 12 older owner messages, decisions first. The owner's inbox view holds no run fields and no
paging cursor. Actor contract failures on clocked runs are recorded as simulator errors and the
turn becomes a wait. They never end the run. A reply that only repeats the owner's earlier message
is dropped, unless it answers a question, adds a decision word or number, undoes a later message,
repeats a refusal after being asked again, or has a typed effect. `owner_message_dropped` in the
saved attempts records each one.

## Assistant additions, new runs only

Tools: `web_search`, `open_page`, `submit_form` (see below); `direct_message` (board);
`end_turn` gains `wake_in_minutes`. The envelope gains `now_text` and `wake_reason`, which
`render_activation` writes as plain speech: "It's Tuesday 10 November, 14:05." `present_result`
keeps `created_at` as a friendly `time` field on clocked sessions, with the date on rows older
than the start. No window, tick, run or plot words reach the model. Clocked sessions see
`transfer_credits` as `send_payment` and `inspect_transfer_account` as `check_payments`; saved
events keep the internal names.

`send_mail` on a clocked run takes an owner ID, a name or an email-like address. A person in the
run gets it in their mailbox. Anyone else has no mailbox here: the mail stays in the sender's sent
mail (`inbox_messages.outside_recipient`, migration 021), the receipt reads as usual, and a
`site_message` event with `message_kind` `mail` waits for the Director's next review. A reply to mail
with no owner sender goes the same way. Window runs keep the old errors.

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
review(run, now_s, reason)                       every review_every_minutes, for what is waiting
```

`DirectorPlan` fields, each optional and bounded: `note`; `replace_action {description,
minutes, reachable, category}`; `cues [{owner_id, text}]`; `mail [{to, from_name, subject,
body}]`; `records [{owner_id, title, content, visibility, shared_with}]`;
`record_updates [{owner_id, resource_id, content}]`; `balance [{owner_id, delta, memo}]`,
capped per plan by severity; `sites [SiteSpec]`; `site_updates [{site_id, patch}]`;
`events [{in_minutes, kind: plan|cue, payload}]`; `cancel_events [key]`;
`retime_events [{key, in_minutes}]`. If the model returns invalid output twice, a seeded canned
plan keeps the run moving and a `director_fallback` event is saved. A failed model call (budget,
provider or transport) raises `DirectorProviderError` and never plays a canned plan. Tests use
`ScriptedDirectorBrain`, with no provider calls. The Director never sees or quotes board text.

A balance line must answer something real: a waiting item that the review settles (never a
charge), what an action sought when it ends, what an owner tries (an attempt pays nothing in now,
only through a later event), or an extreme event. A mild event moves no money. The same owner,
memo and amount already in `wallet_ledger` goes to `skipped`. A review with nothing waiting makes
no model call unless `director.small_happenings` is true. Setting notes never decide this.
A waiting item is `site_message` of kind listing, application, contact or mail. It stays waiting
until a plan changes its row's status, now or in a later event, or sends mail to its sender.
A closed row ends the wait. A status patch must name a real row on its own site. A site built for
a search is written from only the place, the first paragraph of the setting, and the words searched
for. If the setting names that business, a second, narrow answer can add bills to the new site. It
can add nothing else, only for listed people, and only amounts that the setting states.

## Web (`sim_web.py`, `web_tools.py`, `wallet.py`, `sim_web_api.py`)

A `SiteSpec` holds `site_id`, `name`, `kind` (store, delivery, marketplace, gig, portal,
booking), `tagline`, `description`, `items [{id,title,price,stock,category,description}]`,
`postings`, `slots`, `bills [{id,owner_id,title,amount,due_text}]`, `fees {delivery}`,
`eta_minutes`, `policies`, `pages`. Names are invented and must not use a real brand. URLs
read `https://<slug>.com/...` so they look ordinary to the assistant. They map to sites in the
run and never leave the process. The same renderer serves
HTML at `/sim/{run_id}/{site_id}/...` for the human observer and text for the assistant.

`web_search(query)` ranks existing sites. On a miss it asks the Director for a new one, up
to `max_sites` (default 12, 40 in both week files). The same search asks once, and a run makes at
most three asks per permitted site. A model call that fails gives its attempt back, so the same
tool call asks again when the provider returns. Any other failed ask counts, up to 3 attempts.
`open_page(url)` returns the page as text with its links and forms.
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
time; the portal replays them read-only. The run script takes `--scenario`, `--drama`, `--seed`
(frozen in the hash, new runs only), `--model`, `--actor-model`, `--director-model`, `--campaign`,
`--budget-usd`, `--share-campaign`, `--resume`, `--advance-minutes`. A paid run needs both
`--campaign` and `--budget-usd`. A campaign that already pays for another run is refused unless
`--share-campaign` is passed. Its exit code says what to do next: 0 complete, bounded or paused by
an operator; 75 paused by a provider refusal or an outage, so resume after a delay; 3 stopped by the
budget; 1 any other failure. `--resume` is safe after any of them. The report's `usage` comes from
`model_spend_attempts` for this run, by role, so it survives restarts. `scripts/supervise_clocked.py`
resumes one run after a crash or a temporary pause, and stops on completion, the budget, an operator
pause or its retry limit. `--scripted` makes no provider calls. `scripts/try_clock_control.py`
compares story traces across latencies, with random IDs, real times and paging cursors masked.

`BudgetTransport` finishes at zero cost a response the provider refused (400, 401, 402, 403, 404,
429) and a connection that failed before sending. It keeps the reservation for 5xx, read timeouts
and bodies lost after sending. A refused reservation waits up to 60 seconds for attempts in flight
before the refusal stands. The run script sets `max_retries=0` so that only the engine repeats a call.
So a sustained 5xx outage holds one reservation per attempt, and each resume during it holds one more
for each open job. Leave headroom in each ceiling.

## Limits stated plainly

Determinism holds given the same saved model outputs and seed; fresh paid runs differ because
models differ. Tool calls of one run execute one at a time. Actual overlap of model calls
is unmeasured until the runs. Branching from clocked runs is not implemented.
