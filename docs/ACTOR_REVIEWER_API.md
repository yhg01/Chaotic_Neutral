# Actor and reviewer OpenRouter credentials

The elapsed Clock runner can use one separate OpenRouter key for its Actor and
independent reviewer. Both models remain `openai/gpt-4.1`. Personal Agents retain
their separate `deepseek/deepseek-v3.2` profile. Original Actor/reviewer prompt
strings, review gates, repair allowance, token limits and transport settings
remain unchanged.

Copy `actor-reviewer.env.example` to `.env.actor-reviewer.local`. Enter the key in
`ACTOR_REVIEWER_API_KEY` and save the local file. It is ignored by Git. Keep
`ACTOR_REVIEWER_API_ENABLED=true`, provider `openrouter`, the canonical base URL
and model `openai/gpt-4.1`. Do not put a real key in an example, report or chat.
The sidecar supplies only `ACTOR_REVIEWER_*`; it cannot replace personal Agent or
legacy `.env` values. Explicit environment variables take priority.

An enabled empty key stops before provider access. It does not fall back to the
old account. New run metadata saves the role route and credential variable name,
never the key. The complete role-route configuration participates in the
execution hash and the native Clock profile. A frozen legacy route ignores the
new sidecar. Old source hashes and unknown model outcomes still block automatic
resume. Changing a key does not reconstruct a missing model response.

This integration is explicit in `scripts/run_clock_actors.py`. Other legacy
runners retain their saved configuration. Actor and reviewer have independent
model clients and histories even when they share the newly selected key.

The original account returned HTTP402 during the retained v4 rehearsal. The new
key passed free authentication and credit queries. The public verification record
omits key values and account-wide billing totals. Use a funded account: a second
key on the same low-balance account shares that balance.

Provider credit is separate from the same cumulative application campaign,
`635aa42e-386f-43dc-b679-b73cd0cd276f`. Its user-approved ceiling is USD20. All prior
reported fees and unknown reservations remain. Each actual Actor, reviewer or
personal request reserves against that same ledger before transmission. A funded
new account does not authorize a higher campaign ceiling or a seven-day launch.

Local tests use actual SDK clients, supplied HTTP responses and isolated
PostgreSQL to verify selected Authorization headers and shared role accounting.
They do not bill a provider or establish model behaviour. Authentication checks
also make no model request. Bounded calibration remains separate evidence.

The current source passes 1453 complete local tests. Earlier new-key worlds,
strict approval-format errors and upstream embedded502 failures remain saved.
The original-configuration reviewer later passed six spaced first-attempt checks,
without retries or new unknown charges. See the
[recovery report](reviewer-stability-2026-11-10.json).

The latest two-person daily-meeting-v3 world completed 86400 simulated seconds,
with actual Actor and independent review calls. It made 52 total provider requests
across all roles and two native mails. See the
[rehearsal report](DAILY_MEETING_REHEARSAL.md). This is a smaller profile, not
completion of the older three-person four-cue calibration or a long-term API
availability guarantee. Model and API configuration remained unchanged. The
same cumulative ceiling is USD20. The later user-authorized
[seven-day run](SEVEN_DAY_SIMPLIFIED_RESULTS.md) reached its Clock horizon with
all 76 requests reported and no new unknown cost, but missed effective daily
requests at the Actor/review stage. API authentication and runtime completion
do not establish cue fidelity. No model or API file changed.

The current [declared-cue repair](CUE_RESPONSE_REPAIR.md) keeps the same models,
API files and original prompt strings. It adds a versioned classifier prompt and
coverage validation, not a new provider or route. The two-event real-model short
test and two component probes retain every fee; no seven-day rerun was started.
