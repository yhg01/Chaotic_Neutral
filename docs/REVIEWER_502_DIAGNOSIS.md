# Reviewer 502 investigation

## Later recovery — 10 November 2026

The [new six-probe report](reviewer-stability-2026-11-10.json) supersedes the
availability status below: six spaced requests passed on their first attempt,
without errors or retries. Reviewer/core files and all three API files still
match the frozen configuration. The subsequent simplified two-person rehearsal
completed a full day. This bounded recovery does not establish long-term service
availability. The earlier failures and unknown fees remain preserved.

The rest of this document records the **historical 9 November investigation**,
including its failed clean-response gate. Its costs are stage snapshots, not the
current campaign total. See [current findings](CLOCK_RUNTIME_FINDINGS.md).


The unchanged production reviewer request still has intermittent upstream errors.
The bounded stability check **failed**. No new one-day world or seven-day model
experiment was started. Runtime source, models, API files, dependencies, request
limits and routing policy are unchanged.

## What was checked

The selected key passed authentication and has available credit and key allowance.
The current clients use a 45000 ms SDK timeout, a 60-second structured-generation
timeout, explicit `retry_config=None`, and the original two wrapper retries.
This excludes the observed millisecond-unit and hidden-SDK-retry configuration
mistakes from the current client setup. No proxy environment variable was set.

Two free GPT-4.1 endpoint snapshots, at 23:18 and 23:20 UTC on 9 November 2026,
reported the OpenAI endpoint with status -5 and 0% uptime in the preceding five
minutes. Azure endpoints reported status 0 and 100%. Metadata from three older
successful reviewer calls identified OpenAI and matched the saved fees.
The [aggregate gateway status](https://status.openrouter.ai/) showed operational.
These measurements cover different scopes and do not establish identical health.

The exact request keeps `require_parameters=true`, `allow_fallbacks=false`, and
its existing price ceilings. Backup-provider routing is therefore disabled within
a failed request. See the [official routing contract](https://openrouter.ai/docs/guides/routing/provider-selection).
Healthy Azure metadata alone does not prove that the complete frozen request can
use an Azure endpoint. Parameter aliases, account preferences and route eligibility
were not changed or live-tested as alternative configurations.

## Actual bounded check

The diagnostic reconstructs reviewer inputs through the existing `ModelActor`
code from real saved Actor candidates and views. Offline preparation confirmed
that each reconstructed candidate equals its saved review candidate. Original
views and unchanged review-construction code supply the remaining input. Old
HTTP bodies were not retained, so byte equality with an old request is not claimed.
It creates no Actor output, task, authority, payment, message or world run.

The plan allowed six schema-valid, first-attempt reviewer responses, spaced at
least 30 seconds, with at most USD0.40 of added occupied cost. Any interface error
would fail this bounded admission check. This is not a reliability guarantee.

Only the first logical review was attempted:

| Actual HTTP attempt | Response | Result |
| --- | --- | --- |
| 1 | HTTP200 with error502, `provider_unavailable`, no choices or usage receipt | Full unknown-cost reservation retained |
| 2 | HTTP200, one OpenAI completion, valid review JSON | Original retry recovered the request; reported cost saved |

The response was schema-valid and the saved candidate was accepted. However,
first-attempt stability failed, so the other five checks and the full one-day
retest were not started. No rejected or counterfactual component output was
inserted into the stopped world. Its uncertain job remains unchanged.

The first failure is present in the raw HTTP body before SDK parsing. The retry
then succeeds through OpenAI. This supports an intermittent upstream availability
failure. The failed body does not name an upstream supplier, so that individual
failure cannot be attributed to a specific provider with certainty. The endpoint
snapshots are dated aggregates; they do not mean every later request must fail.

## SDK and open-source review

| Existing integration | Source, license and maintenance | Decision |
| --- | --- | --- |
| `openrouter` 0.11.46 | [Official SDK](https://github.com/OpenRouterTeam/python-sdk), Apache-2.0; recent repository commit verified on 9 November 2026 | Keep the installed typed client and explicit SDK retry disablement |
| `langchain-openrouter` 0.2.9 | [Official LangChain integration](https://github.com/langchain-ai/langchain/tree/master/libs/partners/openrouter), MIT; active repository and explicit `langchain-core`/`openrouter` dependencies | Keep the existing adapter and its current timeout conversion and counted retries |

Upstream reports describe [hidden retries](https://github.com/langchain-ai/langchain/issues/40379)
and [timeout-unit surprises](https://github.com/langchain-ai/langchain/issues/39812).
The current clients were inspected directly and already compensate for both.
No library was upgraded and no upstream implementation was copied. This
investigation did not change runtime source, so the prior 1410-test local result
remains the source-matched regression evidence; the real diagnostic is separate.

## Costs, preservation and continuation

This check added USD0.003308 reported cost and USD0.102948 unknown exposure,
USD0.106256 occupied in total. The same USD20 campaign now has:

- Reported fees: USD13.3275766396.
- Unknown reservations: USD2.8233464; no live reservation remains.
- Total occupied: USD16.1509230396.
- Available: USD3.8490769604.

All previous charges, opening balances, original trajectories and failed
attempts remain. A successful retry does not release the failed attempt's
reservation. The diagnostic made no native world effect. See the
[machine report](reviewer-502-diagnostic-results.json) and private wire evidence.

Before continuing, obtain recovery evidence and repeat the bounded review gate
with the same model, key and request policy. After six clean spaced responses,
recheck current same-campaign headroom before a fresh one-day four-cue world.
The stopped world cannot resume its uncertain job by regenerating model output.
Seven-day launch remains explicitly unauthorized and technical eligibility is
false. No background retry or monitor was scheduled.

The final PostgreSQL backup was independently restored and all 80 public tables
matched. A separate comparison proves every pre-existing row unchanged, with
only two new diagnostic spend rows. All 17 original trajectory hashes and all
three API-file hashes match. Provider credentials were absent from the 104
modified/untracked public files scanned and from the private diagnostic files.
