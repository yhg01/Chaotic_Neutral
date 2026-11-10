# Private Actor memory and daily agendas

This opt-in profile adds private retrieval and daily agenda proposals to the
existing human-owner Actor. It reuses the same Actor and reviewer objects and
adds no embedding request, planning model, Agent tool or diagram connection.
The [free validation record](actor-memory-validation-results.json) separates
supplied choices from real-model behaviour. The later authorized
[three-turn real-model probe](ACTOR_MEMORY_MODEL_PROBE.md) passes expiry,
replanning and client/store recreation checks at USD0.084232. It does not
authorize or start a new seven-day experiment.

The subsequent [Actor/Agent short test](ACTOR_AGENT_MEMORY_PROBE.md) closes the
actual file-write/change/readback/report path, but does not pass strict fidelity:
the initial structured agenda is omitted and private planning is delegated in
spoken dialogue. The earlier isolated Actor success is not a general adherence
claim. These findings remain saved for the next local correction.

## Reference and implementation

The reference is the official [Generative Agents repository](https://github.com/joonspk-research/generative_agents/tree/fe05a71d3e4ed7d10bf68aa4eda6dd995ec070f4),
revision `fe05a71d3e4ed7d10bf68aa4eda6dd995ec070f4`, under Apache-2.0. We inspected
its memory, retrieval, scratch schedule and reflection modules, ran the recorded
three-person demo, and probed the original retrieval offline. An alternative
Ollama fork advertised a different stack and MIT in its README, but its license
file could not be verified; no code from that fork was used.

The useful pattern is observations, fallible thoughts, retrieval and a revisable
day plan. agentRPG implements it with new Python/PostgreSQL code. No Smallville
source, saved persona memories, world state or task policy is imported. There is
no new dependency. The existing Concordia architecture remains unchanged.

The source probes reproduced lost last-access timestamps on reload, reversed
recency ordering in an isolated equal-candidate test, empty-retrieval failure
and missing expiry filtering. This profile instead persists access time and
uses time-based decay and as-of validity. These are implementation results,
not evidence of improved model behaviour.

## Private data and validity

Migration `025_actor_memory.sql` adds `actor_memory_profiles`, `actor_memories`,
`actor_memory_snapshots` and `actor_agendas`. The initial free-validation round
applied it only in disposable test databases. The later bounded model probe
applied it additively to the backed-up development database: all rows in the
83 existing tables remain unchanged, with four new memory tables. Use the
existing migration command; never reset an existing database.

A memory belongs to one run and owner. Ingestion uses only the Actor's exact
saved native view: known facts, visible conversation and delivered observations.
Source roles are retained. An assistant claim remains a statement; a thought
remains an inference with evidence references. Current native facts, cues,
receipts, authority and confirmation tasks remain authoritative.

Fresh candidates satisfy `created_seconds <= as_of_seconds < expires_seconds`.
Superseded own thoughts are excluded without deleting their history. Expired
and future records remain auditable but do not enter fresh retrieval. The model
cannot erase observations, supersede a peer record or cite invisible evidence.

Retrieval combines lexical cosine relevance, importance and recency, with default
weights 3, 2 and 0.5. Recency is `2 ** (-age_seconds / half_life_seconds)` using
last access rather than a list index. Empty and equal-score sets are defined
and deterministic. This is a local lexical baseline, not semantic embedding
retrieval; it can miss paraphrases and multilingual relations.

Default retrieval returns at most eight memories and 6,000 text characters.
Source text truncation is labelled. The current native cue and its conditions
are not replaced or shortened by this budget. Selected last-access values are
persisted in PostgreSQL. A new store or independent Python process reads the
same state. Replaying a frozen snapshot does not rerank or touch it again.

## Planning and authority

An enabled Actor may return `cognition.thoughts` and `cognition.agendas` within
its existing response. Thoughts cite the frozen own context. Agendas cover
today or tomorrow in UTC, with at most 24 ordered, nonoverlapping slots per day.
Gaps are allowed. A revision replaces that day's current intention while
retaining earlier versions; an empty version clears it. Yesterday's agenda
cannot become today's plan automatically.

Slots are labelled future intentions, current intentions or elapsed/unverified
intentions. Elapsed time does not complete an activity. An agenda cannot create
a booking, payment, consent or another owner's agreement. The personal Agent
receives actual owner dialogue only; private cognition is not forwarded as an
assistant instruction. Existing disclosure and spending checks remain active.

The same generation response and review path carry cognition; no independent
planner or embedding call is added. Longer context/output may still increase
token cost. The bounded model probe measures one component at USD0.084232;
it does not measure incremental cost against a memory-disabled control.
Existing review and repair calls remain configured.

Existing Clock events alone wake the Actor. This version adds no automatic
daily/agenda-slot wakeup or independent activity engine. The Actor can plan or
revise when a normal cue, question or reply awakens it. Planning cannot replace
the dialogue required by a declared current cue.

## Frozen input and recovery

`RememberingActor` wraps a `ModelActor` or supplied control Actor. It requires
the exact saved `director_clock_jobs` input and freezes an additional private
context under the same job ID. Original Director input/hash and Clock packet
are unchanged. Separate hashes identify the original and enriched views.

The Actor and reviewer receive the same enriched context. Accepted output,
original review diagnostics and generated output-token count are journalled.
A known committed response can return the original decision and diagnostics
without another model call; prior tokens are carried into elapsed accounting.
Unknown generation results remain blocked and cannot be regenerated by memory.

Private cognition is promoted only after the matching native Actor job is
applied and its saved output exactly matches the journal. Promotion runs on
the next Actor preparation or explicit export. Rejected and pending decisions
cannot establish thought or agenda state. An agenda expired on application
remains in the accepted journal but is not promoted as active. Models run
outside memory transactions. Short owner-specific locks avoid a global lock.

ClockRuntime adds only optional Actor binding metadata. Its scheduling,
dispatch, model/tool timing and settlement methods are unchanged. The core
SimulationClock, DirectorClock and timed-transfer source files are unchanged.
Removing the wrapper on resume fails the existing frozen-binding check.
Disabled legacy views/outputs/schemas omit cognition fields. Enable this
profile before the first job of a new world; old runs are not retrofitted.

## Use and verification

```python
from agentrpg.actor_memory import ActorMemoryStore, RememberingActor

store = ActorMemoryStore(pool)
await store.enable(run_id)
actors = {
    owner: RememberingActor(store, run_id, owner, actor)
    for owner, actor in existing_actors.items()
}
runtime = ClockRuntime(pool, actors=actors, agents=existing_agents)
await runtime.enable(run_id, frozen_operator_configuration)
# Continue the bounded scheduler only when execution is authorized.
```

Provider models, API clients and budget bindings remain the existing instances.
The paid CLI does not select this profile by default. Any future paid experiment
needs its own frozen configuration, affordability check and execution authority.

After stopping a free control, export the existing Clock trajectory and
`await store.export(run_id, directory)`. This adds a separate private
`actor-memory.json` containing provenance, frozen contexts, decisions and agenda
versions. It does not rewrite a historical trajectory.

```bash
TEST_DATABASE_URL='postgresql://.../agentrpg_test' \
  uv run pytest tests/test_actor_memory.py
```

Free controls use real isolated PostgreSQL, actual ModelActor/review code with
supplied responses, an independent Python process, journal-response loss and a
three-friend seven-day mixed workflow. They test mechanics rather than naturalness
or improved model task success. The same cumulative campaign remains
`635aa42e-386f-43dc-b679-b73cd0cd276f`, capped at USD25 with historical uncertainty.
