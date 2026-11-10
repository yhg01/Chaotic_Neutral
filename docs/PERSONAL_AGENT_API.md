# Personal Agent API configuration

The separate local profile selects DeepSeek V3.2 through OpenRouter for personal
assistant Agents. Owner Actors, Actor reviewers, shopping stimulus reviewers,
scenario authors and final semantic reviewers keep their existing configuration.
The profile adds no diagram node, tool, credential permission or world effect.

## Configure a new run

Copy `personal-agent.env.example` to `.env.personal-agent.local` only if that local
file does not already exist. Fill `PERSONAL_AGENT_API_KEY` in the local editor:

```dotenv
PERSONAL_AGENT_API_ENABLED=true
PERSONAL_AGENT_PROVIDER="openrouter"
PERSONAL_AGENT_MODEL="deepseek/deepseek-v3.2"
PERSONAL_AGENT_BASE_URL="https://openrouter.ai/api/v1"
PERSONAL_AGENT_API_KEY=""
```

Keep this file private (`chmod 600 .env.personal-agent.local`). Git ignores it.
Do not paste a real key into a command, report, issue or commit. The original
`.env` still supplies database, board and simulator settings. An enabled profile
requires its own key field: an empty field stops before a provider request and
does not fall back to `OPENROUTER_API_KEY`. An existing OpenRouter key can be
entered in this field; it need not be a separate provider account.

Only `PERSONAL_AGENT_*` values are read from the sidecar. Process environment
values take precedence over sidecar values. Values already supplied by the
caller's `.env` also take precedence. The fixed OpenRouter HTTPS origin is the
only supported provider route in this delivery. Filling another URL does not
enable another provider.

New-run selection order is an explicit `--model`, the enabled personal Agent
model, then the legacy `OPENROUTER_MODEL` default when the profile is disabled or
absent. A per-owner `--owner-model OWNER=MODEL` remains the final model selection
for that owner. It uses the same assistant-only credential route. Changing the
assistant default does not change the old Actor fallback; explicit Actor options
retain their existing precedence.

## Entry points and recovery

The profile is connected to `run_agent.py`, `run_scenario.py`,
`run_transfer_models.py`, `run_transfer_actors.py`, `try_browser_shop.py`,
`test_model_agents.py`, and the Inbox/transfer-task model probes. Free control
profiles do not require a personal API key. Scenario authoring remains unchanged.

New scenario, transfer and shopping manifests save the selected model, fixed
provider route and credential variable name. They never save a provider key.
Resume uses this saved selection even if the new-run model default changes.
Credential rotation can replace the value in that same selected variable.
The enabled flag selects defaults for new runs; it is not a pause control for
an existing run.

Historical manifests without `personal_agent_api` keep their original model and
`OPENROUTER_API_KEY`, including from the live console and copied branch manifests.
They ignore the sidecar. Switching the local default does not migrate an existing
trajectory or alter completed tool effects. Bare `run_agent.py` tasks do not have
a scenario manifest; use the original model explicitly when resuming an older
unfinished standalone conversation.

Buyer and seller assistants use the personal credential. Shopping Actors,
response reviews and independent stimulus reviews use the simulator credential,
including after engine recreation. Model IDs and limits remain frozen by the
existing run mechanism.

## Cost and SDK compatibility

DeepSeek V3.2 is added to the explicit text-model budget allowlist. Campaign
runners fetch current public metadata, verify its DeepSeek tokenizer and text/tool
contract, and reserve the full declared input context plus bounded output.
They retain the original cumulative campaign, spent amounts, uncertain charges,
retry accounting and provider price limits. A model change does not add budget.
This conservative reservation can stop a small-budget run before its first
request even when its expected actual cost is much lower.

The pinned SDK drops `reasoning.enabled`. The model factory therefore sends
`reasoning={"effort":"none"}`, OpenRouter's reasoning opt-out. The budget layer
permits only this exact additional option for V3.2; reasoning activation, extra
billing options, media and unsupported models remain rejected. Existing model
defaults are unchanged. Sources: [DeepSeek V3.2](https://openrouter.ai/deepseek/deepseek-v3.2)
and [OpenRouter reasoning control](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

## Verification scope

`tests/test_personal_agent_config.py` checks independent credentials, missing-key
failure, unchanged Actor selection, frozen legacy/new routes and shopping review
isolation. Its integration check uses the actual OpenRouter SDK, Deep Agents,
local board tools, PostgreSQL checkpoints and a persistent test campaign.
Provider responses are supplied fixtures. It verifies one saved board write and
completed-task replay without another request or write. These checks do not
establish DeepSeek behavior, provider availability or seven-day autonomy.

At the implementation snapshot, the local key had not been supplied. On
8 November 2026, the saved key passed the non-generating OpenRouter key check
(HTTP 200). No paid model request or historical-run migration was made. The
original PostgreSQL service is unavailable, and no data files were found in its
configured directory or the checked original storage locations. Live model
tool-use verification is pending recovery of the original database and campaign
ledger. A three-call component probe is prepared; it does not create a new budget.

The full regression before the final shopping missing-key guard passed 1,224
checks in 99.27 seconds. The final focused suite, including that guard, passed
102 checks in 4.40 seconds. Both used a disposable PostgreSQL instance. See
[the structured record](personal-agent-api-results.json) for scope and retained
failed test attempts.

## Real provider follow-up

The [approved partial recovery and real probe](DATABASE_RECOVERY.md) completes
the bounded personal Agent tool check on 8 November 2026. The historical cap and
uncertain fees remain intact. Three provider requests cost 0.0010495872 USD;
new-process replay makes no further requests. The provider returned textual
reasoning despite the explicit opt-out; validated history is preserved within
the full-context input bound. The initial local failure and source repair remain
saved. This does not verify a full Actor scenario or long-term model autonomy.

## Full Actor-task budget gate

The [8 November preflight](deepseek-full-transfer-budget-preflight.json) keeps
GPT-4.1 Actors/reviewers and selects DeepSeek for both assistants. Available
allowance is 0.1976406028 USD; initial concurrent Actor reservations alone need
0.314692 USD. Reviews, assistant activations and later windows are excluded.
The complete transfer test was not started and made no new paid request.
These are conservative reservation bounds, not an expected final bill. The
original 5.5 USD ceiling and historical uncertain costs remain unchanged.
