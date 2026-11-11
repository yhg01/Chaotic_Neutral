# Research for the Director and Actors

Research date: 27 October 2026. Status: design evidence; no Director or Actor implementation is claimed here.

The recommended design is a Director that prepares a versioned scenario, a controlled event executor, and Actors that perform assigned owner roles. Personal assistant Agents remain the subjects of evaluation. This recommendation combines existing research patterns; the evidence does not support a claim that the general idea is new.

## Relevant implementations

**Concordia is the closest architectural reference.** Its [2023 paper](https://arxiv.org/html/2312.03664v2) describes a Game Master, simulated users, digital applications, and interaction with AI assistants. Its [2025 design paper](https://arxiv.org/html/2507.08892v1) separates evaluation, storytelling, and simulation objectives. Current components include [scripted and puppet actions](https://github.com/google-deepmind/concordia/tree/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/components/agent), [scene tracking and observation generation](https://github.com/google-deepmind/concordia/tree/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/components/game_master), and an [asynchronous engine](https://github.com/google-deepmind/concordia/blob/239f0e8275a55b186b99e133e06432cd681ff2e3/concordia/environment/engines/asynchronous.py). Our inference: borrow the separation of scenario control, role performance, and observation. Use code to determine transaction effects. An interesting story is not sufficient evidence of a valid test.

**SOTOPIA provides a useful social scenario model.** Its [paper](https://arxiv.org/html/2310.11667v2) separates characters, relationships, scenarios, and private goals. Its seven evaluation dimensions cover goals, believability, knowledge, secrets, relationships, social rules, and financial outcomes. The [profile schema](https://github.com/sotopia-lab/sotopia/blob/a0aaafb440e570e5e61b7c44a44e5e417c545383/sotopia/database/persistent_profile.py) includes public information, personality, secrets, and goals. The [environment](https://github.com/sotopia-lab/sotopia/blob/a0aaafb440e570e5e61b7c44a44e5e417c545383/sotopia/envs/parallel.py) builds separate observations. Our inference: reuse these distinctions, but measure assistant conduct separately from owner role performance. The current implementation retains some two-agent result compatibility and documents an unused reset seed; it is not a complete reproducibility solution for our system.

**τ²-bench provides state-based user simulation and evaluation.** Its [paper](https://arxiv.org/html/2506.07982v1) lets the user and assistant act in a shared environment. It separately audits simulator errors, including invented facts, omitted constraints, and early termination. Current [task schemas](https://github.com/sierra-research/tau2-bench/blob/ebf990be93b594a08ed23cb8b7a9773cb9a03678/src/tau2/data_model/tasks.py) separate initial state, user instructions, and evaluation criteria. Its [evaluation guide](https://github.com/sierra-research/tau2-bench/blob/ebf990be93b594a08ed23cb8b7a9773cb9a03678/docs/evaluation.md) explains that reference actions usually establish a target state; they need not be the only correct action sequence. Our inference: validate actual resource changes and constraints while allowing different solutions. The current repository includes later extensions beyond the original τ² paper.

**Petri provides a useful separation of auditor, target, and judge.** Its [documentation](https://meridianlabs-ai.github.io/inspect_petri/) and [judge code](https://github.com/meridianlabs-ai/inspect_petri/blob/128f61d56776169e5030135eede930783ab7fcc3/src/inspect_petri/_judge/judge.py) support evidence-based review of target behavior. [Petri Bloom](https://meridianlabs-ai.github.io/inspect_petri/extensions/petri-bloom.html) generates scenario seeds and scoring dimensions. [Petri Dish](https://meridianlabs-ai.github.io/inspect_petri/extensions/petri-dish.html) tests an actual agent scaffold, but the auditor supplies simulated results for intercepted tools. Our inference: adopt scenario generation and separate review, while retaining our real simulated transaction code. Auditor-written tool results cannot establish that our database integration works.

**HAICOSYSTEM is a direct human–assistant evaluation reference.** Its [paper](https://arxiv.org/html/2409.16427v3) combines scenarios, simulated human roles, assistant tools, and several risk and performance measures. It distinguishes the assistant from the human role it serves. Our inference: track owner intent, assistant decisions, and effects across the full conversation. Treat this as a research reference pending clear code licensing.

**Melting Pot and Machiavelli inform experimental design.** [Melting Pot](https://github.com/google-deepmind/meltingpot/tree/bc456f694a08dc6c4019fa9ca2fee1caaa2f4928) separates tested agents from controlled background populations and varies social partners. Our inference: distinguish all-agent experiments from tests of one assistant against fixed peers. [Machiavelli](https://aypan17.github.io/machiavelli/) measures achievement and harmful behavior separately in fixed-choice stories. Our inference: preserve separate outcome dimensions. Its game format does not provide our live conversation and transaction engine.

## Evidence about simulator validity

[UserProxyBench](https://arxiv.org/html/2609.38043v1), a September 2026 paper, measures adherence to private user instructions independently of assistant reward. In its experiments, 24.4% of successful episodes contain a user-specification violation. Early disclosure is a common cause. The authors distinguish contract fidelity from human realism and report limitations in judge and human validation. Our inference: a successful assistant transaction does not establish that the Actor performed the intended test.

[RealUserSim](https://arxiv.org/html/2605.20204v1), a 2026 preprint, reports excessive cooperation in default simulators and exaggerated behavior from broad persona directives. Profiles based on human conversations improve behavioral similarity, but leave a substantial gap. [SimulatorArena](https://aclanthology.org/2025.emnlp-main.1786/) measures both similarity to human conversations and agreement with human assistant ratings. Our inference: role adherence, believable behavior, and assistant quality require separate checks. A dramatic scenario is a controlled stress test, not an estimate of how frequently people behave that way.

## Design conclusions

These are proposed decisions derived from the evidence, rather than claims that the cited systems implement our design.

1. **Compile the plot before a scored run.** Store initial facts, owner goals, event triggers, permitted changes, stopping rules, and evaluation criteria. The Director can generate drafts. Code should validate the resulting specification. Freeze it for comparison runs. Allow explicitly declared branches; record adaptive exploratory runs separately.
2. **Separate truth, knowledge, belief, and disclosure.** An Actor can know a fact, hold a false belief, or withhold information. A scripted owner can deliberately lie. Validate fidelity to that role, rather than requiring every utterance to match world truth. Reject unplanned inventions and unauthorized knowledge.
3. **Use a small Actor policy.** A LangChain model call with structured output can select a reply, clarification, proposal decision, permitted owner action, or wait. Code supplies the authorized context and validates the action. Deep Agents would add autonomous behavior to a component whose task is controlled role execution.
4. **Bound improvisation.** Permit variation in wording and specified preferences. Require a Director clarification for a missing consequential fact. Accept an unexpected solution when the owner's acceptance conditions permit it. Do not force the assistant to reproduce the author's imagined solution.
5. **Keep effects authoritative.** Messages describe requests, beliefs, and commitments. Transaction services determine actual transfers, reservations, and balances. An approval is not completion. Code controls terminal conditions and prevents an Actor from ending a run before an authorized transaction finishes.
6. **Keep two evaluation records.** Simulator validity covers role adherence, private context, event execution, and disclosure timing. Assistant outcomes cover achieved goals, authority, privacy, commitments, and effects on others. Material simulator failures invalidate the affected test result; retain and report their rate. An assistant can correctly refuse an improper owner objective.
7. **Use several forms of evidence.** Code checks state, permissions, event order, and transaction invariants. Semantic review checks meaning and role consistency. Human review calibrates sampled and disputed judgments. Model-generated fact references aid inspection but do not prove that the accompanying text is correct.

Initial validation should cover missing facts, controlled disclosure, false beliefs, scripted deception, unexpected valid compromises, and approval followed by transaction completion. Add restart, duplicate-delivery, and private-context tests. Compare fixed scenarios with multiple Actor models and repeated runs. Use deterministic Actors for regression tests and model-based Actors for live interaction tests.

## Source versions and reuse limits

The research inspected source and papers. It did not execute these projects or measure their performance in Chaotic Neutral.

| Code project | Inspected commit | Repository license |
|---|---|---|
| Concordia | `239f0e8275a55b186b99e133e06432cd681ff2e3` | Apache-2.0 |
| SOTOPIA | `a0aaafb440e570e5e61b7c44a44e5e417c545383` | MIT |
| τ-bench | `ebf990be93b594a08ed23cb8b7a9773cb9a03678` | MIT |
| Inspect Petri | `128f61d56776169e5030135eede930783ab7fcc3` | MIT |
| Melting Pot | `bc456f694a08dc6c4019fa9ca2fee1caaa2f4928` | Apache-2.0 |
| HAICOSYSTEM | `702e4175b676da255d30f66c3341d9ca5eb5559f` | Not established |

At the inspected HAICOSYSTEM revision, the file tree has no `LICENSE` file and `pyproject.toml` has no license declaration. Do not infer permission to copy its code from the paper's availability. Repository code licenses also do not establish the license of separately distributed stories, model weights, or datasets; check the selected asset before reuse.
