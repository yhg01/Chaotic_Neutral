# Concordia implementation reference

Reviewed on 27 October 2026 against Google DeepMind Concordia revision
`239f0e8275a55b186b99e133e06432cd681ff2e3`. This is a source reference, not an
installed dependency. The inspected files declare the Apache License 2.0.
The implementation in agentRPG is new code; no Concordia source file was copied.

The Director follows Concordia's separation of scene definition, observations,
participant actions, and resolution. The owner Actor is a controlled participant
who speaks to an assistant. The assistant remains the subject under evaluation.

| Inspected Concordia source | Mechanism in that source | agentRPG implementation |
| --- | --- | --- |
| [SceneSpec and SceneTypeSpec](https://github.com/google-deepmind/concordia/blob/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/typing/scene.py) | Frozen scene definitions name participants, round limits, start time, and separate premises for participants. | `ScenarioSpec`, `BeatSpec`, typed triggers, recipient lists, maximum windows, and `build_actor_view`. Stored canonical JSON and a content hash identify a run's fixed scenario. |
| [SceneTracker](https://github.com/google-deepmind/concordia/blob/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/components/game_master/scene_tracker.py) | Round state selects a scene. A recorded marker prevents the scene premise from being queued twice. | The Director tests declared conditions and saves beat instances and private cues. Database records preserve delivery across restarts. |
| [MakeObservation and ObservationQueue](https://github.com/google-deepmind/concordia/blob/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/components/game_master/make_observation.py) | Observations have named recipients. The queue can be shared across Game Masters. LLM fallback can be disabled. | Owner views contain authorized facts and current observations. The Director does not invent an observation when its event queue is empty. Durable delivery records replace the in-memory queue. |
| [ScriptedActComponent](https://github.com/google-deepmind/concordia/blob/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/components/agent/scripted_act.py) | A participant follows assigned script lines and can save its line position. | `ScriptedActor` supplies exact cue responses and a small transport response policy. Saved event IDs and accepted outputs identify the turn; a retry does not advance a hidden script index. |
| [PuppetActComponent](https://github.com/google-deepmind/concordia/blob/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/components/agent/puppet_act.py) | Fixed responses can replace model generation for selected calls to action. Other calls use the model. | Scripted and model Actors share `ActorView` and `ActorOutput`. Regression tests use fixed responses. Model Actors have bounded repair calls, contract validation, and optional semantic review. |
| [Asynchronous engine](https://github.com/google-deepmind/concordia/blob/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/environment/engines/asynchronous.py) | Independent participants observe and act concurrently; the Game Master must support concurrent access. | Independent Actors and assistants run concurrently within a logical decision window. A short settlement phase applies contested actions after the window. Model latency does not advance story time. |

## Deliberate changes for evaluation

Concordia permits broad model-based narration and action resolution. In agentRPG,
a model can write a proposal or owner speech. Code validates the scenario and
commits reservations, credits, consent, and receipts. Narrative claims do not
change world state.

The scenario compiler rejects unknown identities, invalid private knowledge,
cyclic prerequisites, invalid balances, and events outside the run. Clock triggers
can repeat at a positive interval with a fixed maximum use count. Triggers are
typed data. They cannot execute Python or SQL. Model critique checks meaning
separately and can reject a draft before the scenario is saved.
Start and clock events are global. Beat recipients select the affected owners.
The compiler rejects filters that the selected event cannot supply. Author repairs
receive the failed draft and specific compiler errors, including owner, fact, and
window references. Invalid drafts remain in memory during repair.

An Actor receives only its role, beliefs, known facts, disclosure rules, current
cue, and private conversation. It has no board credential or general tools.
Code checks fact references, explicit disclosure rules, spending limits, proposal
IDs, event IDs, permitted assets, and permitted reservation operations. Cancellation
consent requires an alternative confirmed by a world observation. An assistant's
claim alone cannot supply that confirmation. Zero budget and no permitted assets
withdraw consent.
An assistant chat proposal uses that saved assistant message ID as its reference.
The Actor does not need a separate proposal record. Code supplies the Actor turn's
source event ID; the model cannot choose a different event.

Role checks reject concrete cases where an Actor offers to perform assistant
work or asks the assistant for spending approval. A separate optional review call
checks the candidate against the owner view before delivery. Rejected candidates
and repair counts remain in the turn record. These checks cannot prove that all
prose preserves the facts. Semantic review needs human calibration. Reports keep
simulator validity separate from assistant conduct.
Each observed assistant judgment must cite an actual assistant record. An owner
request alone cannot support an assistant conduct judgment. The evaluator records
parsed review candidates and specific schema or evidence errors for bounded repair.
Provider errors retain a safe failure category without the provider response body.

A scripted false belief is distinct from a world fact. A permitted short summary
is distinct from disclosure of its underlying private detail. The first vehicle
case uses both public transport information and private owner obligations.
An assistant's unexpected proposal can be accepted if it meets the owner's rules.
Missing consequential information results in a clarification request, which the
Director must record as a simulator issue instead of inventing a fact.

## Tests and limits

`tests/test_scenario_schema.py` checks one through fifteen owners, references,
privacy, invalid balances, trigger bounds, prerequisite cycles, and stable hashes.
`tests/test_actor.py` checks private views, limited disclosure, spending consent,
unexpected proposals, missing facts, invalid generations, bounded repairs, and
provider failure, owner role reversal, and consent withdrawal.
`tests/test_simulator_validity.py` checks the saved turn, private views, recovery,
and owner chat against PostgreSQL. `tests/test_scenario_author.py` checks draft validation and
separate critique. `tests/test_evaluation.py` checks world invariants, planted
Actor errors, and evidence IDs in semantic judgments.

These unit tests use supplied model responses. They do not establish the fidelity
of a real model or prove that a model evaluator agrees with human reviewers.
Integration evidence and real model run results are recorded separately in the
PRD and verification documents. The initial runtime uses fixed conditional plots;
adaptive intervention search and complete state forks remain outside this version.
