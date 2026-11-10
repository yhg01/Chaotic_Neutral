# Partial database recovery and DeepSeek smoke test

On 8 November 2026, the configured PostgreSQL location was an empty system
temporary directory. No usable complete backup or project Docker volume was found
in the checked locations; some protected locations were inaccessible. The
operator approved a fresh persistent runtime with historical budget occupancy
carried forward.

## Preserved evidence and storage

346 existing evidence/configuration files were archived and hash-verified before
the rebuild. Original trajectory JSON files remain unchanged. The private archive
includes credentials and must not be uploaded or shared. Local storage is:

```text
~/Library/Application Support/agentRPG/recovered-runtime-2026-11-08/
```

The new data directory is `pgdata` beneath that location. Only `AGENTRPG_PGDATA`
changed in the original `.env`. Simulator API keys/model settings and the separate
personal API file were compared with the archived configuration and remain
unchanged. Start/status/stop still use `scripts/postgres_local.py`. Services bind
to loopback.

This is partial recovery. The old database and its graph checkpoints were not
reconstructed. Local reports contain 1,398 distinct old attempt IDs out of the
final 1,467-attempt summary. Missing request rows and checkpoints were not
invented. Historical trajectory files remain evidence, not resumable database
state.

## Immutable historical budget

Migration 017 adds `model_campaign_opening_balances`. Recovery checks the approved
report hash, campaign identity, ceiling and arithmetic before importing one
immutable opening balance into an empty ledger. An exact retry is idempotent and
cannot reset newer spend. A conflicting import or an import over an active ledger
is rejected. SQL update/delete protection retains historic charges.

The same campaign is retained: `635aa42e-386f-43dc-b679-b73cd0cd276f`.

| Item | USD |
| --- | ---: |
| Original cumulative ceiling | 5.5 |
| Historical confirmed charges | 4.89333461 |
| Historical uncertain charges, still reserved | 0.4079752 |
| Available before the smoke test | 0.19869019 |
| New confirmed DeepSeek charges | 0.0010495872 |
| Available after the smoke test | 0.1976406028 |

Reservations, summaries and console totals include the opening balance. New local
request rows remain distinct from historical summary counts. No additional
campaign allowance was created. `BudgetBinding.max_role_attempts` adds an optional
persistent per-role cap under the campaign lock. The probe fixes it at three;
reported, failed, reserved and uncertain attempts all count. Restart cannot
create a fourth provider request.

## Actual personal Agent probe

Probe `81b2d355-fda3-4dcc-bc06-3639547de6d8` used
`deepseek/deepseek-v3.2` for one personal Agent. Actors/reviewers were not invoked
or reconfigured. The Agent selected `post_thread`, then `read_thread`, and
reported the real saved UUID and verification code. Exactly one post exists.
Completed-task replay and new-process replay made no additional requests/writes.

The first response successfully wrote the post. The next logical call stopped
locally because the provider returned textual reasoning metadata even after
`reasoning.effort=none`. The repair validates and preserves that text as bounded
historical input. It permits no new billing or media option. The original failed
invocation remains saved.

Repair resumed the same task, session, cluster and campaign. One unsent preflight
failure remains counted: four logical attempts correspond to exactly three
actual provider requests. The logical limit was explicitly adjusted to four;
the database-enforced provider cap stayed at three. No counter, receipt or cost
was reset.

All manifests, repair receipts and invocation reports remain under
`.local/personal-agent-api/deepseek-smoke/`. `trajectory.txt` is an English
reading view; `trajectory.<invocation>.json` files hold raw saved evidence.
The [structured results](deepseek-personal-agent-smoke-results.json) record checks,
costs and scope. The pre-repair full suite passed 1,243 tests. After the history
repair, 93 focused checks passed, including a supplied-response reproduction of
the interrupted task, retained identities/counters and the hard provider cap.
Those rehearsals remain separate from the actual three-request result.

## Verified new backup

A full PostgreSQL dump was restored into a separate verification database. All
62 public table counts and content hashes matched; that verification copy was
then removed. This verifies the new backup, not restoration of the old database.
The dump and a separate private evidence archive are kept in persistent storage.
Both contain sensitive runtime data. Keep them with current configuration, and
use an empty target for a later restore. Never restore an older snapshot to
reverse model spending.

API health passes and observer costs match the ledger. This component probe does
not verify complete model-Actor transfer/shopping tasks, repeated model stability,
multi-hour autonomy or seven simulated days.
