# Source recovery for the first GitHub push

On 1 November 2026, four files in the local checkout were cloud placeholders.
Their metadata was present, but reads failed with an operating-system timeout.
The cloud materialization command also failed because its helper was unavailable.
The first push includes restored copies of all four files.

| File | Recovery source | Restored bytes |
| --- | --- | ---: |
| `scenarios/vehicle-conflict-2.json` | Frozen scenario saved by the two-owner run | 6,036 |
| `scenarios/vehicle-conflict-15.json` | Frozen scenario saved by the fifteen-owner run | 28,494 |
| `tests/test_evaluation.py` | Original creation, later edits, and formatting records | 13,445 |
| `tests/test_scenario_author.py` | Original creation, later edits, and formatting records | 6,855 |

Both scenarios were rendered as UTF-8 JSON with two-space indentation and a final
newline. Their content matches the saved run definitions and passes the current
scenario schema. Both tests were reconstructed in edit order from this project's
original implementation records. Their Python syntax and Ruff checks pass.

All four byte counts match the original metadata. The unavailable originals
could not be read for a direct hash comparison. Those placeholders were preserved
in private local storage before replacement. No runtime behavior was changed.

The restored test files have these SHA-256 hashes:

```text
test_evaluation.py       9195cf5376bea89cbd67454ffdaa19fdbb55389b0a35aa570ffe12da6494f9ba
test_scenario_author.py  978e55a0d98b1314d6e748f52550f183b5896860a2017e647de66119b476f76f
```

The full PostgreSQL suite passed **901 tests in 68.69 seconds**, with no
exclusions. Ruff lint and formatting passed, as did JavaScript syntax checks for
the console and observer. No paid model calls were needed. All 155 candidate
files were readable. The scan found no configured credentials or credential
patterns in those files.

The earlier partial test results remain historical. E-20 in the
[PRD](../PRD.md#evidence-register) records this verification.
Recovery does not prevent future cloud eviction. Use an active checkout outside
cloud-managed folders.

The GitHub source excludes `.env`, local databases, run credentials, private run
records, virtual environments, and recovery working files. It includes the source,
tests, scenarios, migrations, documentation, and dependency lock.
