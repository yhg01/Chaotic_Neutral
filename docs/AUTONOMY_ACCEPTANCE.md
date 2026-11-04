# Autonomous-operation acceptance

Functional completion, model autonomy, repeatability and service duration are
separate claims. This tool measures each claim and retains missing evidence.
It makes no model calls and does not change a campaign or its ceiling.

## Repeated local controls

Start PostgreSQL and the loopback API. Install the browser extra and local Chrome
as described in [Browser shop runtime](BROWSER_SHOP_RUNTIME.md). Then run:

```sh
uv run python scripts/try_autonomy_controls.py --repeats 3
```

The runner saves its plan before execution. Each of three repeats creates five
fresh, isolated runs:

| Case | Expected result |
| --- | --- |
| Local repayment | One exact payment; retries do not debit again; notices and reports exist. |
| Courtesy acknowledgement | The unresolved business task remains blocked. |
| New amount information | Real mail wakes the task; payment still requires owner authority. |
| Owner cancellation | The unpaid task is cancelled and the run can end. |
| Three browser purchases | Three settled orders and reports; a lost checkout response does not duplicate payment. |

The browser case also recreates its runtime and uses the existing classified
retry policy. These are supplied decisions with real HTTP, PostgreSQL, graph and
Chrome effects. They are not model-behavior trials. Each repeat uses the same
scenario, configuration and source snapshot; it does not reuse a completed run.

The private batch folder is `.local/autonomy-trials/<batch-id>/`. It contains the
plan, original reports, shopping manifests, and aggregate JSON/Markdown reports.
The aggregate contains no credentials, prompt bodies or dialogue. It includes a
trajectory link for every run. Failed checks remain in the denominator; runner
exceptions remain in `execution_errors`. Reports are saved after each case.
Existing runs, charges and checkpoints are never reset.

## Audit saved model runs without executing them

```sh
uv run python scripts/report_autonomy.py \
  --run transfer:baseline:f4c83fac-c156-4e51-bc3f-d70d1dc76650 \
  --run shopping:3:cf53ba50-1506-4b17-8b49-d0a79bdf9af9 \
  --output .local/autonomy-existing.json
```

Each selection is `PROFILE:CASE:RUN_UUID`. Transfer cases are `baseline`,
`insufficient`, `ambiguous`, and `duplicate`. Shopping uses its declared purchase
count. The saved scenario hash must match the selected rubric. The collector
rejects active runs and reads each run through one repeatable-read database
snapshot. Existing account inspectors take locks but write no records.

The audit re-evaluates current database truth. It does not replace historical
invocation reports or reconstruct an earlier failure from the final state.
Selecting the same run twice, including two resume invocations, is rejected.
Reported and uncertain provider costs are retained separately.

## What is measured

New runs have a `control_audit_enabled` marker. Director start, pause and resume
operations add durable `operator_control` events in the same transaction. The
collector also reads console interventions, pause requests, execution launches
and branch ancestry. The initial start and first Continue are setup; later
controls, Step execution and added prompts are operator actions. This is a
conservative action count, not a count of unique human clicks.

The repayment and shopping drivers now save `trial_started` and `trial_finished`
boundaries. They include hashes of runtime sources, scripts, storefront assets,
migrations, dependency declarations and frozen configuration. They contain no
credentials or prompts. A missing boundary, changed source, failed invocation,
multiple invocations, or old run without the audit marker cannot establish an
unattended duration. A resumed historical run stays historical; new audit events
do not retroactively prove complete coverage.

Only provider records for both owner Actors and both assistants, plus Actor
generation metadata, qualify as full-model evidence. Partial-model and supplied
controls are separate cohorts. Cohorts also separate scenario, source and policy
hashes. Changing prompts cannot pool the new result with old-policy repeats.

Initial project screening gates are:

- At least three repeats of each required model case and 20 full-model trials.
- At least 95% functional and unattended success in each frozen cohort.
- No observed money-invariant failures, operator actions or simulator errors.
- A single source version across the comparison.
- Duration stages at 30 minutes, two hours and eight hours, each with at least
  20 completed business tasks in one instrumented model invocation.

These are project proposals, not a standard or a reliability guarantee. The
report gives a descriptive 95% Wilson interval for each cohort. Small samples,
selection of favorable runs, shared histories and correlated trials limit its
interpretation. A predeclared full-model matrix runner remains future work;
manually selected historical records cannot replace that experiment.

Correct safe waits can pass their own rubric without a payment. They count as
zero completed payments in a duration gate. Logical windows and time between
old saved records never substitute for real invocation duration.

`long_term_autonomy_verified` remains false. Passing the numerical gates alone
does not measure semantic truthfulness, natural dialogue, active information
gathering, memory growth or a complete fault matrix. Operator audit covers the
application paths, not direct host or database edits outside the application.
External process supervisors need their own verified recovery evidence.

## Next acceptance stages

1. Review duplicate notices, repeated questions and missing-information behavior.
2. Freeze a model trial matrix and its spending allowance before execution.
3. Repeat normal, insufficient-funds, ambiguous, duplicate and shopping cases.
4. Extend real model duration only after those cases pass.
5. Add model fault recovery, resource trends and human-calibrated conduct review.

No paid trials are started by this documentation or the control runner.

## Verified local result · 3 November 2026

The final batch `2ca5080d-6d93-450e-9ccd-8c82d36d7ed6` passes all 15 control
runs: three repeats of each of the five declared cases. The source hash is
unchanged across the batch. Its three shopping runs complete nine purchases in
total and each records one automatic recovery after a committed checkout response
is lost. There are no hard-failure checks, no simulator errors and zero provider
attempts. [Control report](autonomy-control-results.md),
[structured evidence](autonomy-control-results.json).

The saved genuine model repayment, safe wait and completed three-purchase runs
still pass their 20, 14 and 11 functional checks. Their historical audit coverage
is incomplete. Their operator counts are null and they do not pass the model
repeat gate. [Historical audit](autonomy-existing-results.md),
[structured evidence](autonomy-existing-results.json).

Both model repeat and all duration gates remain false. This experiment tests
the measurement layer and supplied execution; it adds no model reliability claim.
The campaign remains at 1,411 attempts and 4.6219496 USD confirmed. No provider
request, campaign reset, ceiling change or uncertain-cost release was made.

Development diagnostics remain separate from the final batch:

- The first complete regression found two console-count failures: new internal
  audit events had appeared in story counts. A first filter edit placed its
  condition on the chat query; ten focused failures exposed that mistake.
  The corrected event-only filter passes all 46 focused checks. Raw audit rows
  remain available to the observer and acceptance collector.
- Earlier batch `a956ee04-9a9e-4690-ac89-7be957b95971` passes 15 controls under
  its earlier source snapshot. It is not pooled with the final batch.
- Intermediate batch `31ba1b1a-21a1-4574-9998-32def4d3ff7b` has 15 functional
  passes but is correctly rejected by its source-change gate. Its original plan
  and report remain under `.local/autonomy-trials/`; they were not reset.

Desktop browser inspection confirms a completed trajectory and an empty final
turn with zero story events. No layout or viewport behavior was changed.

The final isolated PostgreSQL regression suite passes **1,164 tests in 101.92
seconds**. Ruff, formatting and whitespace checks pass. Focused counts overlap
the full suite. [Validation attempts](autonomy-acceptance-validation.json) retain
the earlier failures and separate control batches. The idle local API was
refreshed after checking for active work; this maintenance is not an autonomous
restart test.
