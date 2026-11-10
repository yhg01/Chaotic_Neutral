# Daily confirmation: one seven-day simulation

**Completed from the original checkpoint under the authorized USD25 ceiling.** The same run reached **604800 seconds**, with **7/7 requests closed**, 14 actual mails and no uncertain jobs. There were four confirmations and three refusals, giving one effective change and three reconfirmations. Source-v28, models, APIs and historical costs/trajectories were retained; no new run or paid short test was started. The full outcome is in [the result record](daily-confirmation-seven-day-results.json).

Run `3e4a89a9-974a-4e92-87c3-46477ed5cef7` uses two friends, two independent
personal Agents, and one meeting-time change each simulated day. Its Clock
horizon is **604800 seconds**. This is one persistent world, rather than seven
fresh one-day trials. The final outcome and per-day evidence are recorded in
`daily-confirmation-seven-day-results.json` when the worker stops.

## Configuration and free verification

Personal Agents remain `deepseek/deepseek-v3.2`; Actors and independent review
remain `openai/gpt-4.1`. The three API configuration files and original Actor
generation/review prompt strings are unchanged. The declared scenario, typed
validation and confirmation-tool policy are explicit experimental interventions.
No additional paid short test was started in this round.

Each day has its own task ID, activation event, exact terms, request, peer decision,
reply and reports. A future task is hidden until its addressed Clock cue is
delivered. Old 14:00/16:00 approvals cannot satisfy another day's request.
The profile begins with no new startup request or status report. It alternates
16:00 and 14:00 for the future free board-game meeting on 24 November 2026.

The initial free full regression passed **1466 tests**. After the observed schema
recovery described below, **1468 tests** passed, with zero failures, errors or
skips. Free controls exercise the actual PostgreSQL, Clock, Agent graph, HTTP
tools and Inbox through seven simulated days: seven approvals, fourteen actual
mails and requester body reads before final reports. They also exercise
response loss after commit, controller replacement, a budget checkpoint and
the same-world schema recovery. Supplied model choices in these controls are
infrastructure evidence, rather than evidence of real-model reliability.

## Initial USD20 budget policy

The same cumulative campaign is `635aa42e-386f-43dc-b679-b73cd0cd276f`, with an
initial USD20 ceiling, later raised to USD25 by explicit user authorization. At the initial preflight it occupied USD18.2643135396, including
USD2.8233464 historical uncertain cost, leaving USD1.7356864604.

The planning estimate was USD1.83392804: the measured USD0.13099486 short control
cost, multiplied by seven and a factor of two for context growth, checked against
current model prices. This is a planning allowance, not a statistical guarantee
or an upper bound for all possible behavior. It exceeded the available amount.
The user explicitly authorized a funded start followed by a checkpoint if the
next request cannot be reserved. Every real request still passes the unchanged
native campaign reservation check. No budget increase or historical-cost release
is permitted.

## Observed failure and same-world recovery

Source-v27 reached the first daily cue at second 43200. The Actor expressed the
correct request and full condition but omitted `confirmation_actions` in both
generation attempts. Native validation rejected both before owner-message or
mail delivery. The original invocation, two rejected candidates and USD0.10816
remain. This was the start of the declared seven-day experiment, not a new
short-test cohort.

Source-v28 makes each current task field required in the generated JSON schema,
with exact task IDs. The peer still chooses `confirm` or `decline`; code does not
supply that decision or fill in an omitted model output. Local checks confirm
both choices and reject missing, empty and foreign task fields.

An operator recovery receipt preserves the rejected output, diagnostic history,
old and new source/configuration hashes and the original input hash. It permits
only this fully observed schema rejection; uncertain provider output cannot be
regenerated. It returns the same job and request to pending without changing
the world, roles, API routes, scenario, Clock contract or campaign. The 535
already generated output tokens are carried into the accepted job's elapsed
time. A replay of the recovery receipt cannot reset an applied job.

The original world then continues from that checkpoint. Report this source
transition as **one operator intervention**; completion would not establish
uninterrupted, zero-intervention autonomy. Original source-v27 records remain
separate from later accepted output.

## Scope

This is the simplified daily-change scenario, not the original four-perturbation
medium-chaos calibration. Confirmation terms and conditions are declared and
frozen; arbitrary newly invented conversation conditions are not automatically
extracted. Canonical mail and owner reports preserve the declared contract.
Clock completion, seven actual agreements, budget compliance and long-term
autonomy must be assessed separately.

## Retained USD20 checkpoint

Status: **budget_checkpoint_saved**. The Clock reached 561756 of 604800 seconds. **6/7 daily requests** have a real owner decision, exact peer reply, requester body read/receipt and honest final report. There were 4 confirmations and 3 refusals. Among fully closed tasks, reconstructing the agreed time from the initial 14:00 plan gives 1 effective changes and 2 reconfirmations; the latest agreed time is 16:00. Refusals are not relabeled as agreement.

| Daily task | Peer decision | Native closeout complete | Effective change |
| --- | --- | --- | --- |
| meeting-day-01 | confirm | True | True |
| meeting-day-02 | decline | True | False |
| meeting-day-03 | confirm | True | False |
| meeting-day-04 | decline | True | False |
| meeting-day-05 | confirm | True | False |
| meeting-day-06 | decline | True | False |
| meeting-day-07 | confirm | False | False |

The single world made 216 actual HTTP attempts, including the retained first invocation, and reported USD1.58144460. No paid short test or replacement world was started. The same campaign now occupies USD19.845758139600, including USD2.823346400000 uncertain cost, leaving USD0.154241860400 under USD20. All configured API files and historical trajectory hashes passed preservation checks.

The next request could not obtain its required native cost reservation. The run stopped before dispatching that unfunded request. The current world, input, original generated candidates and graph/Clock records are saved. The remaining amount is not automatically spent or increased. A saved job may require explicit reconciliation; this report does not claim automatic resume of uncertain output.

The original rejected Actor outputs and the source transition remain in native immutable recovery records and the first invocation export. This is one recorded operator intervention, so this result is not zero-intervention autonomy. Machine-readable native IDs, per-day checks, costs and raw hashes are in [the result record](daily-confirmation-seven-day-results.json).

The Clock was explicitly paused at the same saved time after the budget stop. The next unfunded node belongs to Bob's Actor. The seventh reply has been read, but Alice's explicit Inbox acknowledgement and final owner report remain pending. All 216 actual HTTP attempts in this world have reported costs; the budget refusal sent no new provider request. The USD2.8233464 uncertain amount is historical and was not released. Alice refused all three 14:00 proposals; later 16:00 proposals reconfirmed the existing time, so this is one effective rescheduling rather than seven.

The final private checkpoint backup was restored into an owned temporary database. All 83 public tables matched by content hash; the temporary database was removed. Preservation checks cover 344 legacy files, 61 earlier raw/materialized trajectories, the original first-day failure and all three API files. No configured credentials were found in the 131 changed public files. Source snapshots 1 through 28, immutable recovery evidence and both real invocation exports remain in the private archive.

## Completion after authorized budget resume

The user increased the same campaign ceiling from USD20 to USD25. The existing budget-denied Actor node had generated zero tokens and sent no HTTP request. Its original failure/input were preserved in an immutable receipt, the same job was requeued, and only the operator configuration ceiling changed. The tracked runtime source remains `5088da598a245e025b33dd762baf9bce61048a9091094e1021ac3ec163ffcf34`. An operator launcher sets the authorized ceiling to USD25 while refusing new-world creation. The old USD20 checkpoint report and raw trajectory remain unchanged.

This continuation used 7 real HTTP attempts and reported USD0.0947497600. Alice explicitly acknowledged the existing seventh reply and delivered the final owner report; the Clock then reached its full seven-day horizon. The same world now has seven source requests, seven real peer decisions, fourteen mails, seven requester reports and no uncertain jobs. No prior mail or task was repeated.

| Task | Actual peer decision | Native closeout complete | Effective change |
| --- | --- | --- | --- |
| meeting-day-01 | confirm | True | True |
| meeting-day-02 | decline | True | False |
| meeting-day-03 | confirm | True | False |
| meeting-day-04 | decline | True | False |
| meeting-day-05 | confirm | True | False |
| meeting-day-06 | decline | True | False |
| meeting-day-07 | confirm | True | False |

Final cumulative reported cost is USD17.1171614996; retained historical uncertain cost is USD2.823346400000; budget occupancy is USD19.940507899600 under USD25, leaving USD5.059492100400. The complete world has 223 reported HTTP attempts costing USD1.67619436. There was one earlier schema recovery and one user-authorized funding pause/resume. This completed controlled trajectory does not establish uninterrupted or general long-term autonomy.

The final completion backup was restored and all 83 public tables matched by content hash. All 64 trajectories present before the USD25 resume, all three API files and the tracked runtime source remain unchanged. All 223 HTTP attempts in the original world have reported costs; no new uncertain provider attempt was introduced. The new private archive retains every earlier checkpoint and source snapshot.
