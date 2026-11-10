# Bounded real-model Actor memory and planning probe

On 10 November 2026, one GPT-4.1 Actor completed three native Clock jobs with
the existing independent GPT-4.1 reviewer. It kept its initial private agenda,
revised that agenda after a new event, and retained the revision after the
memory store and Actor/reviewer clients were recreated. The
[machine-readable results](actor-memory-model-probe-results.json) record 23
checks against saved native evidence. This is a component result, not a new
seven-day experiment or a complete Actor/Agent task.

## Frozen scope

- Run: `31c2cf02-98b5-419a-bd02-9aa8a6642961`.
- Runtime source-v30:
  `0a238c1737a7cd578f39f0f94c605f098d9999c6f11835fcbfd131672b6abfb8`.
- Protocol:
  `b8a9f4d458a8ada3b8a86ebaf71825ced332e04ee07231a0af80101007b6c7e2`.
- Actor and reviewer: unchanged `openai/gpt-4.1` and existing OpenRouter route.
- Personal Agents: unchanged `deepseek/deepseek-v3.2` configuration, idle.
- Only Alice's Actor is dispatched. Bob's Actor also remains idle.
- The native timing rules remain 600 output tokens per simulated minute,
  one second per graph-tool batch and seven seconds per transfer. This probe
  uses a two-hour maximum horizon and pauses after the third application.
- An additional USD1 and 18-HTTP-attempt guard applies within the existing
  USD25 cumulative campaign. It does not replace the campaign guard.

## Observed outcomes

| Cue at simulated second | Accepted Actor result | Applied at second |
| --- | --- | --- |
| 0 | Private agenda: 14:00–15:00, Birch Room. No booking, payment or friend message requested. | 48 |
| 1800 | The initial session is cancelled. Agenda now contains only 16:00–17:00, Willow Room. The prior intention is history. | 1858 |
| 3600 | After client/store recreation, the Actor reports 16:00–17:00, Willow Room, without receiving a new time in the cue. | 3648 |

The original notice remains in the audit store with expiry at second 600.
It is absent from the fresh retrieval contexts at seconds 1800 and 3600.
All selected memories are valid as of their snapshot and belong to Alice in
this run. The third context includes the current Willow Room agenda and the
Actor's evidence-linked thought about the replacement. All three agenda
versions and all accepted decisions remain saved.

There were three Actor generations and six reviewer calls: one cue
classification and one candidate review per turn. All nine attempts have
reported costs. No repair generation, transport retry or new uncertain charge
was needed. The Actor remained the owner, and its dialogue did not claim a
booking or completed reading activity.

No personal Agent graph ran. Native payments, board messages, peer mail and
booking requests are zero; both simulated balances remain 100. The final
owner message remains pending for the intentionally idle Agent. The Clock is
paused at second 3648. The native full-task success field remains unset.

## Cost and preservation

| Same campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` | USD |
| --- | ---: |
| Occupied before | 19.940507899600 |
| New reported cost | 0.084232000000 |
| Occupied after | 20.024739899600 |
| Remaining under the unchanged USD25 limit | 4.975260100400 |
| Historical uncertain charges retained within occupancy | 2.823346400000 |

The verified backup preceded additive migration `025_actor_memory.sql`.
Four memory tables were added; no pre-existing row in the 83 old tables was
changed or removed. All 973 trajectories present before this probe have the
same hashes afterward. All three API files and the Clock/runtime source
hashes also match. A second backup restored and matched all 87 public tables.
Earlier backups and private archive chains are retained.

The exact probe driver and its USD1/18-attempt guard passed two free checks in
disposable PostgreSQL before model generation. Earlier private driver-precheck
failures remain saved; they caused no provider call. Runtime source was not
changed. The prior source-v30 full regression remains 1489 passes; it was not
rerun or counted as new evidence in this component probe.

Private local evidence:

- `.local/actor-memory-model/live/final/trajectory.json`
- `.local/actor-memory-model/live/final/actor-memory.json`
- `.local/actor-memory-model/live/report.json`
- `.local/actor-memory-model/protocol.json`
- `.local/actor-memory-model/preservation-after.json`

## Limits and next experiment design

Expiry is enforced by retrieval. This does not show that a model can receive
expired records and reject them unaided. The ten-minute observation TTL is a
declared compressed probe setting; the default TTL remains seven days.

Recent native dialogue and the current agenda are still visible. Therefore,
this probe does not isolate the benefit of memory over dialogue alone. A
controlled comparison should vary the memory profile while keeping cues,
visible dialogue, model settings and budget rules fixed.

One Actor and three responses do not establish long-run stability, reflection
quality or naturalness. Live recovery here recreates clients and the store;
the separate free evidence covers independent-process reads and response loss.
This probe does not test agenda wakeups, overnight expiry, activity execution,
personal Agent decisions or the three-friend mixed medium-chaos setting.
Those remain separate acceptance requirements. No new seven-day experiment
is authorized or started by this result.
