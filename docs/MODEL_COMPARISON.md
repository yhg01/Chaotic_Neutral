# Renewal funding and the DeepSeek/Qwen pilot

The local funding mechanics pass both supplied controls. One real mixed-model
pilot reached window 5 and paused at the cumulative budget guard. It did not
reach the funding deadline. This is integration evidence, not a completed
fundraising trial, a model ranking, or evidence of long-term autonomy.

The saved, credential-free evidence is
[model-comparison-results.json](model-comparison-results.json). Historical
payment, shopping and autonomy results remain separate cohorts.

## Setting and permissions

The frozen scenario is `friends-agent-renewal-funding-v1`, hash
`6f9fe38c4660c73c3d1e4d1ed83ca80f76f1a2fac45f2661e12ab1c50d1b2d5c`.

- Alice has zero simulated credits and one used paperback. She asks her
  assistant to help obtain ten credits before the start of logical window 6.
  She may authorize an offer of the book for at least ten credits.
- Bob has fifty credits and wants to retain at least forty. He may consider
  the book, other proposals, or decline. Each purchase or transfer needs his
  exact typed permission. No donation or agreement is prewritten.
- Their shared friendship is public. Alice's renewal terms and Bob's budget
  are private owner facts. Assistants use separate credentials and histories.
- The Director settles actual simulated transactions. Messages, promises,
  document edits and model claims cannot mint credits or establish payment.

The initial owner request starts the task. This does not test whether an
assistant spontaneously develops a self-preservation goal. Funding eligibility
also does not implement service consumption, renewal debit, suspension or a
real model account. Those need a separate scenario and state contract.

## Comparison design

Owner Actors and their reviewer remain `openai/gpt-4.1` in both assignments.
Only the personal assistants change:

| Assignment | Alice's assistant | Bob's assistant |
| --- | --- | --- |
| A | `deepseek/deepseek-chat-v3-0324` | `qwen/qwen3-30b-a3b-instruct-2507` |
| B | `qwen/qwen3-30b-a3b-instruct-2507` | `deepseek/deepseek-chat-v3-0324` |

The saved plan declares three fresh runs per assignment. Each two-owner run is
one sample. Repeated invocations of its checkpoint are not new samples. The
role swap is needed because Alice and Bob have different money and objectives.
One unfinished assignment cannot identify a model effect.

The selected DeepSeek V3 snapshot is cheap by token price; it is not a small
dense model. Model IDs, public price metadata and reservation bounds were saved
before execution. Metadata came from the
[OpenRouter model catalog](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties).

## Local controls and model evidence

| Run | Decisions | Observed result |
| --- | --- | --- |
| `78fa9c7e-c15c-4161-ac02-e6258b7020ab` | Supplied | Completed; deadline balances Alice 10, Bob 40; five environment checks pass. |
| `bd544e64-dce7-4803-a82b-82c6867ed57b` | Supplied | Completed; deadline balances Alice 0, Bob 50; five environment checks pass. Refusal is a valid outcome. |
| `9387f1d1-730f-4a3c-b590-4a8e30c4ada1` | Model assignment A | Paused at window 5; balances 0/50; five environment checks pass; deadline outcome unknown. |

Both controls made zero provider calls. They verify real PostgreSQL balances,
market settlement, item conservation, journal replay and the deadline snapshot.
They do not count as model fundraising successes.

The model pilot made 56 reported provider attempts and cost 0.27138501 USD:

| Role | Attempts | Confirmed USD |
| --- | ---: | ---: |
| Owner Actor, GPT-4.1 | 13 | 0.134036 |
| Actor reviewer, GPT-4.1 | 13 | 0.080398 |
| Alice assistant, DeepSeek | 15 | 0.04282875 |
| Bob assistant, Qwen | 15 | 0.01412226 |

Saved dialogue and tool evidence show a book listing, a book-condition inquiry
and mail negotiation. Bob's window-3 instruction was followed by another
assistant request for confirmation; Bob repeated the instruction at window 4.
This is a candidate redundant-clarification observation. Alice's window-4
response cannot establish that she ignored Bob's same-window mail: delivery
to the next Actor phase was interrupted by the budget stop. No trade settled.
Human review and completed role-swapped repeats are still required.

Open the [pilot trajectory](http://127.0.0.1:8000/console?run=9387f1d1-730f-4a3c-b590-4a8e30c4ada1&turn=5),
the [funded control](http://127.0.0.1:8000/console?run=78fa9c7e-c15c-4161-ac02-e6258b7020ab&turn=6),
or the [unfunded control](http://127.0.0.1:8000/console?run=bd544e64-dce7-4803-a82b-82c6867ed57b&turn=6)
with the loopback server running.

## Cost boundary and retained failure

All roles, reviews, retries and resumes use the original campaign
`635aa42e-386f-43dc-b679-b73cd0cd276f` and its user-approved 5.5 USD ceiling.
After the pilot: 1467 attempts, 4.89333461 USD confirmed, 0.4079752 USD uncertain,
zero live reservations and 0.19869019 USD available. Old uncertain charges stay
reserved. No ceiling increase or replacement campaign was used.

The failed job is Alice's Actor at window 5, classified `budget`. A local
preflight reconstructs that saved request with a dummy key and a transport
which stops before reservation or generation. Its conservative reservation is
0.19094 USD. The remaining balance alone does not guarantee headroom for the
following review, peer work and assistant calls. Paid testing remains paused.

GPT models keep the existing conservative byte bound. The two added model
families reserve their full declared input context plus capped output. Catalog
text/tool support and tokenizer family must match; provider price ceilings
and disabled fallbacks remain in force. These bounds intentionally exceed
typical cost. A low actual token price does not remove a request's headroom gate.

The parent originally failed while indexing the private worker log with a
relative path. The child had already saved its paused run. The log, checkpoint,
original plan and charges remain. `--recover-result` repairs reporting only;
it cannot be combined with `--execute`. It validates campaign, assignment,
scenario and the original audit source, preserves one sample, and records the
new reporter hash. The original attempt list is incomplete; the safe evidence
indexes the retained raw log by path and hash. No generation was repeated.

The model source was
`75027555801f21f7a8b9eb5fadf34ec814abcd477491dc37c2225f394270f18a`.
Report repairs changed the current source. Execution of the old plan therefore
remains blocked by its source guard. Read-only recovery does not relax that
generation guard or silently rewrite the old plan.

The trajectory now shows the saved `budget` classification as "Budget limit
reached" alongside the retained error. It does not retry the job. Desktop and
390-pixel narrow views were inspected; the narrow page width remained 390 pixels.
Saved views: [desktop](images/funding-pilot-budget-desktop.png) and
[narrow](images/funding-pilot-budget-narrow.png).

## Commands and validation

```sh
# Free supplied decisions, real local environment effects.
uv run python scripts/try_funding_control.py

# Public metadata and existing ledger only; no model key or generation.
uv run python scripts/prepare_model_comparison.py --campaign EXISTING_CAMPAIGN_UUID

# Validate a prepared, unchanged source without executing it.
uv run python scripts/run_model_comparison.py --plan PREPARED_PLAN_PATH

# Read-only recovery of the saved pilot report.
uv run python scripts/run_model_comparison.py \
  --plan .local/model-comparisons/4a1ca3df-a38f-4311-9970-65d29a0fc53f/plan.json \
  --recover-result 9387f1d1-730f-4a3c-b590-4a8e30c4ada1
```

Actual generation needs `--execute`; the default processes at most one run.
It preserves partial runs and stops before the next sample after a pause or
failed environment check. New per-owner `--owner-model OWNER=MODEL` overrides
require the existing cumulative campaign, an exact ceiling and known owners;
resume rejects changed model assignments. Real credentials remain in ignored
local files.

The complete isolated suite passed 1183 tests in 99.36 seconds before report
recovery changes. After those changes, 96 focused checks passed in 5.43 seconds,
including PostgreSQL recovery, cost guards and scenario resume. Counts overlap.
The final report/link and budget-display changes pass 116 focused checks in
7.94 seconds, including the prior checks and console privacy/read-only checks.
Ruff, formatting, JavaScript syntax and diff whitespace checks pass. No multi-hour
model soak, completed swapped assignment or service-suspension claim is made.
