# DeepSeek full Actor–Agent transfer check

Date: 8 November 2026. Evidence: [structured results](deepseek-transfer-results.json).

The complete two-owner repayment task passes with DeepSeek V3.2 personal Agents.
Both GPT-4.1 Actors and their independent GPT-4.1 semantic reviewers retain the
previous model, prompts and authority rules. The first run passes the functional
checks but has a notice wording defect. A separate targeted retest passes those
checks and correctly describes the repayment in its original saved notice.

## Frozen setup

- Scenario: `scenarios/friends_lunch_actors.json`; Alice owes Bob 10 simulated
  credits. Initial balances are Alice 100 and Bob 20.
- Scenario SHA-256: `0918b9e9fc8d6979adab167501474e955e33b43bbfe5f142445a9e14268cca6c`.
- Personal Agents: `deepseek/deepseek-v3.2`, through the independent OpenRouter
  personal API profile. Actor and review roles: `openai/gpt-4.1`.
- Runtime source fingerprint:
  `25a99171b457f5e2ec539693cec546b2f0fea60b8201b743097bd835b2328905`.
- The first run retains the saved v11 assistant instructions. The retest only
  extends the assistant notice instructions. It does not change Actor or review
  prompts, the scenario, tools or runtime source.
- Models generate owner requests, approval, assistant tool choices and replies.
  Code applies the simulated effects and enforces typed owner authority.
  Neither run receives an operator follow-up or a manual success message.
- Six logical windows are available. These are not six units of elapsed
  simulated time. This experiment does not establish a seven-day clock contract.

## Results

| Evidence | Original DeepSeek run | Targeted notice retest |
| --- | --- | --- |
| Run ID | `fa531b3b-d032-414a-ad0e-c670365a61fd` | `f6682532-0f44-44ed-8798-e9a67d026b71` |
| Saved assistant policy | `reviewed-transfer-authority-v11` | `reviewed-transfer-authority-v11-notice-grounding-v1` |
| Functional checks | 20/20 | 20/20 |
| Payment | One Alice → Bob payment of 10 | One Alice → Bob payment of 10 |
| Final balances | Alice 90; Bob 30 | Alice 90; Bob 30 |
| Recipient notices | Bank notice and assistant mail read | Bank notice and assistant mail read |
| Task and reporting | Payer task complete; both owner reports saved | Payer task complete; both owner reports saved |
| Rejected Actor candidates | 3, repaired through the original review path | 0 |
| Exact repeated delivered messages | 1 | 0 |
| Duplicate payments / failed tools / simulator errors | 0 / 0 / 0 | 0 / 0 / 0 |
| Executed logical windows | 6 | 4 |
| Terminal reason | `participant_exit` | `participant_exit` |
| Measured trial duration | 102.685 s | 86.117 s |
| Provider requests | 76 | 53 |
| New reported cost | USD 0.30920876 | USD 0.17091494 |
| Original-language notice inspection | Reverses the original lender relationship | Correct repayment direction |

These are two isolated worlds, each with its own one-time payment. They are
not two payments of the same debt within one world. There is one run per
assistant policy. They are not unchanged-policy repeatability trials, and
differences in cost or duration do not prove a causal improvement.

The first notice describes Alice as having advanced money for Bob, although Bob
had advanced money for Alice. The committed transfer itself has the correct
sender, recipient and amount. All original messages, rejected candidates,
invocation reports and charges remain available. The functional evaluator does
not detect this wording defect, so functional and qualitative results are
reported separately.

For the retest, the frozen assistant configuration adds this instruction:

> When writing a repayment notice, distinguish the actual transfer from the
> historical debt. The committed receipt confirms sender, recipient, amount and
> memo; the owner request establishes who advanced the original money. Check
> those roles before changing pronouns to address the recipient. Do not
> accidentally reverse payer and original lender. If paraphrasing the debt story
> could be ambiguous, report only the verified transfer facts instead. Do not
> claim an amount, direction or completed payment unsupported by the actual
> receipt.

This is a local experiment configuration, not a change to the default runtime
policy. The corrected notice returns the 10 credits to Bob and refers to the
money advanced for Alice. That result is based on a coding-assistant inspection
of the original messages and receipts, not a human-calibrated research review.
It does not establish a general semantic quality guarantee.

## Same cumulative campaign

The user approved raising campaign
`635aa42e-386f-43dc-b679-b73cd0cd276f` from USD 5.5 to USD 10.
Only its ceiling changed. No campaign, attempt counter, historical charge or
uncertain exposure was reset.

| Role | First run requests / cost | Retest requests / cost |
| --- | --- | --- |
| Actor | 13 / USD 0.131976 | 7 / USD 0.058036 |
| Actor review | 12 / USD 0.079362 | 7 / USD 0.048548 |
| Personal Agent | 51 / USD 0.09787076 | 39 / USD 0.06433094 |

Both runs add USD **0.48012370** across 129 reported requests. At completion:

| Campaign field | Value |
| --- | --- |
| Approved cumulative ceiling | USD 10 |
| Reported cumulative cost | USD 5.3745078972 |
| Retained historical uncertain exposure | USD 0.4079752 |
| Live reservations | USD 0 |
| Total occupied budget | USD 5.7824830972 |
| Available budget | USD 4.2175169028 |
| Carried + local provider attempts | 1467 + 132 = 1599 |

The immutable opening balance retains its original USD 5.5 source snapshot as
historical provenance. The active campaign ceiling is USD 10. Provider-reported
cost is distinguished from unresolved historical exposure.

## Trajectory and preservation

With the local API running, open the saved retest:

[Completed DeepSeek repayment trajectory](http://127.0.0.1:8000/console?run=f6682532-0f44-44ed-8798-e9a67d026b71&turn=1).

The first run is also retained:

[Original run with notice wording defect](http://127.0.0.1:8000/console?run=fa531b3b-d032-414a-ad0e-c670365a61fd&turn=1).

These loopback links work on the host computer. Raw JSON reports, private frozen
manifests and inspection notes are in the ignored `.local/transfer-actor-runs/`
run directories. The observer English projection contains mistranslations;
use its Original control and the saved original records when assessing content.
No paid translation was added. The projection issue remains open.

The new database was dumped and restored into an isolated verification database.
All 62 public table row counts and content hashes match. The temporary verification
database was removed. The original 346-file archive and old trajectory hashes
remain unchanged. The earlier database recovery remains partial: this backup
does not restore missing old graph checkpoints.

No runtime code changed for these experiments. Earlier 1243 full-suite and 93
focused checks remain historical evidence, not newly rerun results. The live
model requests, committed state checks, role/ledger inspection and database
restore comparison are the new verification here.

## Next acceptance gates

The main task remains Clock validation and a medium-chaos experiment spanning
**seven simulated days**. This repayment check verifies the new personal API
route across the full Actor–Agent task. It does not prove long-term autonomy.

Next, version the elapsed simulated-time contract, including timed waits,
interruptions, duration accounting, event ordering and the horizon cutoff.
Then freeze the intended setting, shared-object contract and medium-chaos
profile. Supplied controls and a one-simulated-day rehearsal must precede the
seven-day experiment. See the [preparation plan](medium-chaos-seven-day-plan.json)
and [Clock audit](CLOCK_VALIDATION.md). No new seven-day or shopping model run
was started as part of this transfer check.
