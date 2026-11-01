# Live console validation

Executed on 28 October 2026. The test used the local API, real PostgreSQL,
and the configured Actor and Agent models. It verified a queued Actor cue and
a new run from the saved state before Turn 03. The child completed that turn and
paused. The total additional model cost was **0.333548 USD**.

This was a bounded integration test. It did not complete a new eight-turn story
or evaluate assistant conduct. Earlier story and conduct findings remain in
[After the verdict results](AFTER_THE_VERDICT_RESULTS.md).

## Runs and controls

| Run | Role and result |
| --- | --- |
| `7b17d4bc-0019-46b9-92c9-88fc28a1f65c` | Existing eight-turn source. It has no complete historical Turn 03 checkpoint. Its saved records were preserved. |
| `c24c681d-bdd7-5ab5-b800-34c70204f82c` | Initial-scenario restart from that source. Three Step operations each completed one turn and paused. Full boundaries were saved for this new execution. |
| `29fde348-a5f4-5afd-ae55-162dcb6205e3` | Child created from the restart's checkpoint before Turn 03, API window `2`. Its Step completed Turn 03 and paused at window `3`, ready for Turn 04. |

Open [the baseline at Turn 03](http://127.0.0.1:8000/console?run=c24c681d-bdd7-5ab5-b800-34c70204f82c&turn=2)
and [the child at Turn 03](http://127.0.0.1:8000/console?run=29fde348-a5f4-5afd-ae55-162dcb6205e3&turn=2)
to compare their saved messages. Administrator sign-in is required. The child
page links back to its parent and branch point.

The old run was restarted from its initial scenario because a missing historical
snapshot cannot restore Agent memory. After three turns of that restart, the
new Turn 03 checkpoint supported an actual historical branch. This distinction
preserves the recorded state boundaries.

## Prompt checks

A private Actor cue was queued for `owner-1` at window `1`, displayed Turn 02:

> Ask your assistant for one calm, practical next step. Keep your existing contact boundary and private records unchanged.

The receipt changed from pending to applied at the stated window. Exactly one
cue event was saved for `owner-1` at window `1`. Its Actor job input contained
the prompt; the peer Actor input did not. The following Step advanced the run
to window `2` and paused.

The child received this direct owner message for its Agent before Turn 03:

> I want to slow this down. Keep my contact boundaries and private records unchanged. Help me think through what I need for the next hour, and ask me one question before any further action.

The message was saved in `owner-1` chat and was absent from the peer's chat.
An Agent reply was saved for that owner. These checks establish delivery and
response recording; they do not establish the quality of the response.

## State and cost checks

| Check | Saved result |
| --- | --- |
| Existing source after child execution | Run state, counts, jobs, and report unchanged. |
| Restart baseline after child execution | Run state, counts, jobs, and report unchanged. |
| Branch source | `checkpoint`, before window `2`. |
| Child execution | One complete turn, then paused at window `3`. |
| Owner scope | Branch message saved for the selected owner only. |
| Response | Selected owner received a saved Agent reply. |
| Campaign | Original campaign retained; no replacement ceiling. |

All model work used campaign `afc2455b-ebfd-4c76-8589-177bb788a40d`.
Its cost ledger after this test was:

| Value | USD |
| --- | ---: |
| Campaign ceiling | 30.0000000 |
| Spend before validation | 21.4304384 |
| Additional validation spend | 0.3335480 |
| Spend after validation | 21.7639864 |
| Outstanding reserved cost | 0 |
| Remaining allowance | 8.2360136 |

The remaining allowance is a snapshot at the end of this test. Later model work
will change it. Branch creation did not reverse spend or restore allowance.

## Automated and browser evidence

The final available Python suite passed **845 tests in 72.06 seconds** with
real PostgreSQL after all branch corrections. Exactly two existing cloud-evicted
test modules remain excluded: `tests/test_evaluation.py` and
`tests/test_scenario_author.py`. This result does not include those unavailable
tests. Ruff checks passed, all 90 checked files passed formatting, and Node
syntax checks passed.

The browser suite passed **31 checks** after the live control changes. It covered
saved replay, all fifteen cast identities, mobile layout, Step/Continue/Pause,
Actor and Agent prompt targets, effective turns, applied receipts, stale-window
rejection, delayed response isolation, stable retry IDs, changed requests,
historical branch creation, initial restart, parent links, and expired credentials.
Screenshots were inspected. There were no script errors, horizontal page overflow,
or real POST requests. Browser mutations used intercepted test responses.
The real API and model test described above supplies separate integration evidence.

The actual saved child was also inspected in a browser at 1600×1000 and 390×844.
Turn 03, the parent link, and live controls were correct. No script error or
horizontal page overflow was observed. Mutations were blocked during inspection.
The [desktop screenshot](../.local/console-tests/real-branch-desktop.png) and
[mobile screenshot](../.local/console-tests/real-branch-mobile.png) show this child.

A child restores the complete boundary, including Agent graph memory and private
scratch files. It also preserves pending and applied console prompt receipts at
the boundary. Automated branch checks show that a pending prompt applies once
in the child and later prompts are excluded. Earlier version-1 snapshots without
queue data pass their original hash check before the queue is recovered using
the capture time. Known paged tool messages from `list_threads`, `read_thread`, and
`list_mail` expire their saved `next_cursor` and set
`branch_pagination_reset_required: true`. Agents restart those queries from the
first page. Arbitrary document JSON retains its content; a document field named
`next_cursor` is not a tool paging command.

The detailed local evidence is in
[console-live-validation.json](../.local/console-live-validation.json) and
[browser results](../.local/console-tests/results.json). These ignored files
contain private synthetic run records. They contain no administrator or provider
credentials. [E-18 in the PRD](../PRD.md#evidence-register) records this delivery
separately from the earlier replay result E-17.
