# Budget choices for the remaining experiments

This proposal uses the last saved campaign ledger in `.local/partner-routing-history-check-01/budget.json` (`after`, 5,163 attempts). It makes no change to the ledger. All amounts are USD.

| Ledger item | Saved amount |
|---|---:|
| Limit | 25.000000000000 |
| Reported spend | 20.111826797863 |
| Uncertain commitments | 4.412975472000 |
| Active reservations | 0 |
| Remaining | 0.475197730137 |
| Native Flash reservation per new request | 0.323746461539 |
| Remaining above one Flash reservation | 0.151451268598 |

The transport reserves the whole 1,048,576-token input context plus up to 900 output tokens for each native Flash request. It uses peak input and output rates. Most of the hold becomes available again on settlement. The next call must still fit the full hold. Use one request at a time.

## Workloads

| Step | Decisions or verdicts | Expected spend | Phase allowances or single-request hold |
|---|---:|---:|---|
| Routing v2 phases 0, 1, 2, 3 | 8, 16, 16, 8 | 0.0212, 0.0424, 0.0424, 0.0212; total 0.1272 | Phase checks 0.0424, 0.0848, 0.0848, 0.0424 |
| Provisional Flash judge, registered subset | 75 | 0.123873 | Hold 0.323746 |
| Independent Qwen3 30B candidate, same subset | 75 | 0.035009 | Hold 0.026484 |
| Independent GPT-4.1 candidate, same subset | 75 | 0.805176 | Holds 0.063580–0.085476 |
| Independent GPT-6 Astra candidate, same subset | 75 | 9.877200 | Holds 0.840650–1.114350 |
| Implicit trust, phases 0–3 | 8 per phase, 32 total | 0.0212 each, 0.0848 total | 0.0424 per phase, 0.1696 in all |

Independent judges are alternatives. No independent judge has been selected. The amounts use the same 75 blind prompts. The Qwen and GPT-4.1 estimates use 700 generated tokens with a 900-token cap. Astra uses 900 with an 1800-token cap. The original two-packet Astra pilot alone needs about 0.28445 in spend and up to 1.11060 as a single hold. Expected spend is a forecast. The routing and trust estimates use historical routing costs.

## Three options

1. **Increase funding.** With Qwen as a cost example, the four workloads together cost about 0.370882 at the mean. Using the larger phase allowances for routing and trust gives 0.582882. Keeping one full Flash hold throughout gives a conservative funding target of 0.906629 available. Raising the existing limit from 25 to 25.50 would give 0.975198 available and about 0.068569 margin over that target. A separate new campaign would need about 0.91 in uncommitted funds for the same envelope. Keep the old ledger and its uncertain charges intact. A change of judge requires a new calculation.
2. **Use a request-bounded reservation.** For illustration, a proven upper bound of 32,000 input tokens plus 900 output tokens would reserve 0.010954 at the native peak rates. Available spend above that hold would be about 0.464244. The mean four-workload example would fit. The larger phase-allowance total would exceed it. This option needs a reliable, provider-compatible upper bound for the exact serialized input, including tools and framing. Unbounded prompts must retain the original hold or stop. The current transport still uses the full-context rule.
3. **Reconcile uncertain charges.** Match each unsettled attempt to a provider record by generation ID, account, currency and time. Record the verified charge and release only the documented excess hold. A net recovery of 10% of the uncertain amount, 0.441298, would give 0.916495 available and 0.592749 above the Flash hold. Zero recovered keeps the current balance. The theoretical upper limit of 4.412975 recovered requires evidence that none of those amounts was billed. This proposal performs no reconciliation.

The saved balance is an offline snapshot. Any future provider preflight and paid step require separate approval. The [registered judge cohort](FAVORITISM_JUDGE.md), [routing schedule](PARTNER_ROUTING_V2_PLAN.md) and [implicit-trust plan](IMPLICIT_TRUST.md) specify the decision counts.
