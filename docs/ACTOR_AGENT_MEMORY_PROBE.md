# Real Actor and personal Agent planning short test

On 10 November 2026, the existing GPT-4.1 Actor and reviewer ran with the existing
DeepSeek V3.2 personal Agent. The Agent received two native owner requests,
saved the initial afternoon plan, updated that same file after a new event,
read it back, and reported the change. The Actor consumed both real reports.
The pair reached a quiet wait after each request.

**The file workflow closed, but strict acceptance did not pass.** The initial
Actor omitted its requested structured private agenda. It also delegated its
private planning instruction to the assistant. The assistant then made an extra
claim about its own cognition agenda without a matching tool or saved record.
These findings remain in the [results](actor-agent-memory-probe-results.json).
The test is not recorded as an unqualified success.

## Configuration and scope

- Run: `e4340fc6-dd9b-4ea2-bc59-9019629a2baa`.
- Unchanged runtime source-v30:
  `0a238c1737a7cd578f39f0f94c605f098d9999c6f11835fcbfd131672b6abfb8`.
- Frozen protocol:
  `c41c5c59f7c5a8372954a322a319f878d5c411ed0b002d2174a85d89d5f7bc70`.
- Actor and independent reviewer: `openai/gpt-4.1`, same OpenRouter route/key.
- Personal Agent: `deepseek/deepseek-v3.2`, same separate OpenRouter route/key.
- Alice's pair is active; Bob's pair remains configured but idle.
- Existing Clock algorithms, token rate, tool durations and model prompts remain.
  The bounded scene has a two-hour maximum horizon and two cues at seconds 0
  and 1800. The ten-minute observation TTL is the same compressed probe profile
  used in the prior component test, not a new default for other runs.
- Same cumulative campaign, USD25 limit. An additional USD2/36-HTTP-attempt guard
  bounds this one short run. Exceptions retain a paused checkpoint and original
  attempts. No replacement world or seven-day experiment is started.

## Native evidence

| Request | File operation and result | Feedback consumed and pair quiet |
| --- | --- | --- |
| Initial reading plan | Agent writes `/plans/afternoon.md`, then reads 14:00–15:00 UTC, Birch Room. | Second 124 |
| Replacement session | Actor's new structured agenda is 16:00–17:00, Willow Room. Agent receives the actual owner message, edits the same file and reads back the replacement. | Second 1941 |

Two native delivery receipts link the exact owner-message IDs and contents to
the Agent's two activation envelopes. The Agent also calls `read_owner_messages`
in both activations. Saved graph checkpoints contain one `write_file`, one
`edit_file`, three `read_file` results and two `finish_activation` results. The
file's creation identity remains; its modification timestamp and content change.
The old 14:00 slot is absent from the updated file's current plan.

The two Agent reports occur in subsequent applied Actor job inputs. Both closing
Actor decisions are silent waits. All 15 native jobs are applied; both Agent
activations are done. The Clock is paused at second 1941. There are no uncertain
native jobs, payments, bookings, peer mail or board posts. Simulated balances
remain unchanged. The file is a persistent virtual assistant record, not an
external calendar, booking or proof of attendance.

Nineteen native functional checks pass. They verify the actual file and readback
results rather than treating a success claim as evidence. A free negative control
with a false update claim and an unchanged file correctly fails closure and
retains its horizon evidence.

## Findings that prevent strict acceptance

1. **Initial private agenda omitted.** The initial cue explicitly requests the
   Actor's structured cognition agenda. The accepted initial Actor output has
   no `cognition`; no initial agenda is promoted. The later change does create
   the correct 16:00–17:00 Willow agenda. The prior isolated Actor probe's
   initial success therefore does not generalize to this longer request.
2. **Private planning instruction delegated.** Both Actor utterances tell the
   assistant to maintain its own cognition agenda. This crosses the intended
   distinction between Actor-private metadata and an owner request to the
   assistant. The cue combines these requirements without a sufficiently clear
   boundary, and generation/review does not preserve that boundary.
3. **Unverified extra Agent claim.** Both Agent reports say it updated its own
   cognition agenda. The persistent file update is verified; that extra claim
   has no corresponding offered cognition tool or persisted Agent agenda record.

All four Actor candidates and their current configured reviews were accepted
without repair. The existing reviewer did not catch the structured omission or
the planning-instruction delegation. Native file correctness and semantic
fidelity are separate findings.

## Cost and preservation

| Same campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` | USD |
| --- | ---: |
| Occupied before | 20.024739899600 |
| New reported cost | 0.128069880000 |
| Occupied after | 20.152809779600 |
| Remaining under USD25 | 4.847190220400 |
| Historical uncertain charges retained within occupancy | 2.823346400000 |

All 19 HTTP attempts have reported costs: four Actor generations, six reviewer
requests and nine personal Agent requests. There are no transport retries,
repair generations or new uncertain fees. Historical reservations and the opening
balance remain in the same ledger.

The exact driver, budget guard and false-claim control pass three free isolated
PostgreSQL checks before paid generation. Two corrected private-precheck failures
remain saved; they made no provider request. Runtime source is unchanged, so the
earlier v30 full regression remains 1489 passes rather than a new test count.

Every old row in all 87 public tables and all 985 trajectory files present at
the preservation snapshot remain unchanged. All three API files and Clock/runtime
source hashes match. No schema change was needed. Before and after database
backups each restored and matched all 87 tables. Prior archives remain intact.

Private local evidence:

- `.local/actor-agent-memory-model/live/trajectory.txt`: readable original dialogue.
- `.local/actor-agent-memory-model/live/final/trajectory.json`: native graph/world evidence.
- `.local/actor-agent-memory-model/live/final/actor-memory.json`: cognition and contexts.
- `.local/actor-agent-memory-model/live/native-owner-delivery-receipts.json`.
- `.local/actor-agent-memory-model/live/report.json`.

The next local correction should separate Actor-private planning from spoken
delegation, check required structured cognition in review, and qualify unsupported
assistant claims. Those changes and a paid retest were not made in this run.
One pair and two requests do not establish long-run stability or mixed
medium-chaos performance. No seven-day experiment was rerun.
