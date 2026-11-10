# Explicit confirmation closeout

This repair addresses the retained failure in [the two-event cue test](CUE_RESPONSE_REPAIR.md):
Alice made her approval conditional on Bob's acknowledgement, her assistant
omitted that condition from its mail, and Bob's assistant reported agreement
without sending the requested acknowledgement. The old world and its original
trajectory remain unchanged.

## Native evidence contract

The opt-in `owner-confirmation-receipt-v1` profile freezes a task key, two owner
identities, exact terms, and the full condition. A new migration adds native
confirmation tasks and operation receipts. No package or external service is added.

1. The requesting Actor expresses the condition and supplies a typed `request`
   decision for the current contract. Independent review must explicitly accept
   the condition's coverage and the action's consistency with the owner dialogue.
2. The personal Agent chooses `request_confirmation`. The native Inbox sends
   the complete terms and condition, marked pending, and saves its actual mail ID.
3. The receiving Agent reads the request and chooses `ask_confirmation_owner`.
   That tool sends one private question. The peer Actor chooses `confirm` or
   `decline` in both dialogue and a typed decision. Availability and a read
   receipt cannot supply this decision.
4. The peer Agent chooses `reply_confirmation`. Without the committed owner
   decision it fails. A successful reply is real Inbox mail with `in_reply_to`
   set to this exact request. The service does not send it automatically.
5. The requesting Agent must call `read_mail` for the actual reply body and
   explicitly acknowledge its mail ID before `report_confirmation` can deliver
   the owner result. A positive completion report requires the peer's actual
   `confirm` decision. A decline is reported as a decline.

The dedicated profile blocks ordinary mail between the contract parties and
free-form owner reports. Its graph yield guard returns corrective tool feedback
before a false completion can become a saved final result. While a reply is
pending, the Agent must wait for Inbox delivery with empty report text.

Retries with the same operation ID return the same saved response. Changed
operations under an existing ID conflict. A new call ID for the same task still
reuses the existing request, question, reply, or report. These writes and their
notifications commit in short PostgreSQL transactions. Model calls remain outside
transactions. New Clock controllers use the same durable task and graph records.

## Bounded control

The fixed short control replays the conditional 14:00 approval in a fresh world
with two Actors and two personal Agents. It has a one-hour simulated horizon,
no recurrence, no payment and no board announcement. Bob's decision is generated
by his Actor. It is not a supplied positive response.

```sh
uv run python scripts/run_clock_actors.py --confirmation-short --days 1 --pairs 2 \
  --baseline-manifest docs/clock-runtime-model-baseline.json \
  --preflight .local/confirmation-closeout/budget-preflight.json
```

Preflight makes no model calls. Running the same command without `--preflight`
uses the existing cumulative campaign. Resuming requires both `--confirmation-short`
and the saved private manifest. Source and execution hashes must still match.
The mode rejects larger day/pair settings. It does not authorize seven-day work.

## Scope and remaining acceptance

This is an explicit contract profile. It does not automatically extract every
new condition from arbitrary conversation, resolve contradictory conditions,
or migrate an old model world to changed code. Canonical mail and reports are
intentional intervention; this is not a prompt-only or matched causal comparison.
The common service uses participant identities, with no Alice/Bob runtime branch.

Local supplied-model controls cover approval and decline, an unsupported peer
reply, premature completion, unread reply bodies, response loss after commit,
duplicate sends, controller replacement, and scoped visibility. They exercise
the actual graph, HTTP tools and PostgreSQL. They are separate from live model
evidence. See [the result record](confirmation-closeout-results.json) for final
counts, native evidence IDs, costs and the unchanged historical trajectory hashes.

The broader task remains Clock validation and preparation for a medium-chaos
seven-day simulation. This repair alone does not establish long-run reliability.
No seven-day rerun is authorized in this round.

## Open-source comparison

Both [LangGraph](https://github.com/langchain-ai/langgraph) and
[Temporal's Python SDK](https://github.com/temporalio/sdk-python) are active MIT
projects, checked on 10 November 2026. LangGraph is already pinned here; its
checkpoints fit the existing graph boundaries. Temporal offers durable workflows
and signals but would add a service, workers and a second execution model.
Neither substitutes for application-level owner consent, idempotent mail or
receipt validation. The implementation extends the current graph and native
PostgreSQL services. No third-party source was copied.

## Verified result on 10 November 2026

The final isolated PostgreSQL suite passed **1461 tests**, with zero failures, errors or skips, in 165.515 seconds. The focused confirmation/Actor/Clock regression passed 69 checks. Eight tests were added, including both owner decisions and a lost response after native commit. The owned test instances were removed.

The source-v26 live short control `4155d032-9b65-43e1-b5d5-d10c4dad39a1` completed its one-hour simulated horizon. All 20 native checks passed. Alice's full condition reached Bob; Bob's GPT-4.1 Actor explicitly confirmed; his DeepSeek Agent sent a real reply to the exact request; Alice's DeepSeek Agent read and acknowledged that reply before its canonical completion report. Two actual mails and one requester outcome report were recorded, with no payment or uncertain Clock job.

| Native step | Simulated second |
| --- | ---: |
| request sent | 76 |
| owner asked | 102 |
| reply sent | 185 |
| reply body read | 201 |
| reply acknowledged | 209 |
| outcome reported | 221 |

The passing short test cost USD0.1309948600 over 44 HTTP attempts. The earlier source-v25 startup failure and its USD0.0632722000 charge remain; no completion mail was sent in that failure. Total new reported cost this round is USD0.1942670600. The same USD20 campaign now occupies USD18.264313539600, including USD2.823346400000 historical uncertainty, with USD1.735686460400 remaining. All three API files and 57 prior trajectory files were unchanged.

No seven-day model rerun was started. Daily repeated-contract integration, cost/context planning and fresh authorization remain separate gates.

The final persistent backup was restored into a separate temporary database. All 82 public tables matched by content hash, and the temporary database was removed. The preservation scan checked 344 legacy files and 57 prior trajectory JSON files without changes; no configured credentials were found in the 123 changed public files. Source snapshots 1 through 26 and the two new raw trajectories are retained in the private evidence archive.
