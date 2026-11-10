# Current cue response repair and repeated-event short test

Verified on 10 November 2026. **The declared-cue response repair passed local
verification and a two-event real-model short test.** Both new needs reached
the personal Agents and received real peer decisions. **Complete social closeout
did not pass:** the second exchange still lacks a requested acknowledgment.
No seven-day model experiment was rerun.

## What changed

Task-bearing Director cues can declare `owner_response_required=true`.
The Director carries this strict boolean into the addressed owner's private
observation. It does not expose peer cues or give Agents a new tool.
The daily-meeting scene is now versioned as `friends-community-daily-meeting-v4`.
Its task-bearing cues declare this requirement; ordinary notifications can still
permit silence. Original scene versions and their evidence remain separate.

The opt-in `require_current_cue_review` protocol works in three steps:

1. The existing independent reviewer classifies the current cue before seeing
   an Actor candidate. A model cannot cancel a declared communication requirement
   by saying that earlier work is complete. Missing or substituted cue IDs fail.
2. The Actor generates its own dialogue. Actor validation and the Clock conductor
   reject silent output for a declared instruction before delivery or effects.
3. Independent review checks each required cue's coverage in actual delivered
   dialogue. Missing, duplicate or foreign cue IDs, or `covered=false`, cannot be
   overridden by an overall `accepted=true` result.

The classifier and owner-output stages each have at most one repair. Original
Actor/reviewer prompt strings, model IDs, API files, token limits and transport
retry settings remain unchanged. The new classifier prompt, validation contract
and extra review calls are explicit protocol changes:
`independent-current-cue-v2-declared-response`. Source and execution hashes bind
this protocol. Changing source cannot silently resume an older model world.

The requirement forces communication of a declared need. It does not write the
owner's speech, choose tools, supply a friend's agreement or create a world effect.
Unmarked informational cues retain ordinary model classification and may wait.
The same implementation supports all owner identities; no special two-person
runtime branch was added. No dependency or database migration was added.

## Verification and retained failure

The final isolated PostgreSQL regression passed **1453 tests**, with zero
failures, errors or skips, in 158.92 seconds. Thirty checks were added across this
repair. They cover new needs after old completion, false global acceptance,
exact cue coverage, classification repair, legitimate silence, spending boundaries,
private delivery and a saved rejection that neither advances nor regenerates.
The owned test database was removed; the user's database was not reset.

An earlier model-only classifier version passed 1447 local checks but still
returned silence on the saved failed context. That failed component and its
USD0.040232 charge remain. It was not relabeled as success.

The final component check preserved the old role, chat and cue words and added
the new declared-response metadata. GPT-4.1 then generated the actual 14:00 request,
with coverage accepted. It made three HTTP attempts and cost USD0.032690.
No output was inserted into the original world. This adapted input is not a
byte-identical replay or a matched causal model comparison.

## Two-event real-model short test

| Item | Result |
| --- | --- |
| Run | `9d6a6aaf-7c3e-4014-a628-ce0cb7a14623` |
| Profile | Two friends, separate Actors and personal Agents; one cue per day |
| Horizon | 172800 simulated seconds, 245.078 wall seconds |
| Personal model | `deepseek/deepseek-v3.2` |
| Actor and independent reviewer | `openai/gpt-4.1` |
| Cue at 43200 | Alice's16:00 request reached the Agent; Bob actually agreed |
| Cue at 129600 | Bob's14:00 request reached the Agent; Alice actually agreed |
| Actual HTTP attempts | 80 |
| Reported short-test cost | USD0.39825036 |
| Native effects | Four Agent mails; zero payments; zero board posts |
| New unknown fees or uncertain jobs | Zero |

At the second cue the classifier again initially said that no response was needed.
The typed requirement rejected that decision. One bounded repair identified the
new need, after which the Actor expressed it and the Agent acted on it. The
rejected classification and accepted repair are both preserved. This verifies
the guard on an actual failure mode, rather than counting cue delivery alone.

## Remaining closeout issue

Alice's second approval said to consider 14:00 final once Bob acknowledged it.
Her Agent omitted that condition from its reply and asserted that the time was
already final. It then ended in `waiting` for an Inbox reply. Bob's Agent reported
both parties' agreement but sent no further acknowledgment. The four native
mails and final activation states establish this gap.

Thus two new requests and two real peer approvals are verified, while only the
first exchange has established complete closeout. This repair does not establish
condition-preserving assistant reporting, universal Actor fidelity or long-term
autonomous completion. No calendar or booking record exists. Inspection was
assistant-led, without independent human signoff. Original Chinese messages remain.
See [structured evidence](cue-response-repair-results.json).

## Budget and preservation

The same campaign `635aa42e-386f-43dc-b679-b73cd0cd276f` retains its USD20 ceiling.
This repair used 86 actual HTTP attempts and added **USD0.47117236** reported:
the failed component, the repaired component and the short test. Historical
unknown exposure remains USD2.8233464; no reservation was released or reset.

Current reported charges are USD15.2467000796, live reservations are zero, total
occupied is **USD18.0700464796**, and **USD1.9299535204 remains**. All original
fees and trajectories remain. The short-test start record is consumed, and the
seven-day gate is closed. Further cost/context planning must include effective
work and the unresolved confirmation path; this sample is not a seven-day forecast.

Original readable trajectory:

```text
.local/clock-model-runs/9d6a6aaf-7c3e-4014-a628-ce0cb7a14623/8ab7e48183544e519c833ada076593f3/trajectory.md
```

Its adjacent JSON retains native evidence and 148 checkpoint blob versions.
SHA-256: `a53ffbcc84bf96d88d7b8e08530d5fccea5cc33fe882ce3954961816e464abc7`.
Runtime source: `b569eb300598b3c1657a7e3ee5b8cd58845314907f00b053af49bb3d099a2e31`.
Private traces and archives are not publication assets.
The new database backup was independently restored and all 80 public tables
matched by row count and content. All 344 historical files and all 22 original
model-world traces retain their verified hashes. All three API files are
unchanged, and public changed files passed a credential scan. Older backups remain.

## Implementation references

The existing repository's independent shopping-stimulus and bounded output
validation patterns were reused. Two maintained, non-archived MIT projects were
checked: [Pydantic AI output validation](https://github.com/pydantic/pydantic-ai/blob/main/docs/output.md#output-validator-functions)
and [LangGraph validation](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_validator.py).
Pydantic AI would add another agent framework; LangGraph's older ValidationNode
is deprecated. Neither was added, and no external code was copied.
