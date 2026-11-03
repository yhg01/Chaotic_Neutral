# Model owners and durable shopping execution

This continues [E-36 browser integration](BROWSER_SHOP_RUNTIME.md) on 3 November
2026. The user deferred testing several unrelated task types. This work extends
the local storefront and continuous execution requirements. All goods, accounts
and payments remain inside the simulation.

## Current result: three purchases completed

The user approved a same-campaign increase to 5.5 USD on 3 November 2026.
Run `cf53ba50-1506-4b17-8b49-d0a79bdf9af9` resumed unchanged and now passes
all 11 checks with three accepted purchases. The new reading-lamp order is
`6bf513e3-9803-56e8-9157-b7fa8eef2b3d` for 24 credits. Both earlier orders
are unchanged; balances are Alice 956 and Bob 144. The owner report follows
settlement and includes the actual order ID.

The continuation made 19 successful logical model requests, cost 0.1164656 USD,
and completed four additional windows in 22.939 seconds. The full frozen manifest
is unchanged. Final confirmed campaign cost is 4.6219496 USD; uncertain exposure
remains 0.4079752 USD, leaving 0.4700752 USD under 5.5 USD. Earlier failures and
the earlier budget stop remain recorded below as historical snapshots. See the
[closure report](TASK_CLOSURE.md) and [authorization](task-closure-budget-authorization.json).
This closes bounded three-purchase acceptance; naturalness and multi-hour model
reliability remain open.

## Model profile

`--mode model-actors` uses two model owners and their two independent model
assistants. GPT-4.1 generates owner dialogue and reviews it. A separate GPT-4.1
call decides whether a later private stimulus requires an owner response and
identifies new purchases. GPT-4.1-mini runs both assistant tool loops. The buyer
uses Chrome; the seller uses the existing scoped Resource and market tools.
No supplied owner or seller decision is used in this mode.

The frozen scene exposes prices, stock and the total standing budget in each
owner's permitted request and Resource view. Initial setup grants no purchase
task. Later `shop-round-N` cues request independent purchases. These are declared
scene inputs; the model must express each request and submit its typed task.

The new opt-in checks address failures found in actual runs:

- An open shopping command exposes all required fields in its JSON schema,
  including its description. Cancel commands have a separate schema.
- Existing authority persists with null consent. A later status reply cannot
  silently restore spent authority or relist stock.
- A private-view stimulus decision is fixed while that candidate is repaired.
  A reviewer cannot reverse a wait instruction because it sees a different reply.
- If that model chooses silence, the Actor returns a typed, effect-free wait.
  The trace labels this `stimulus_yield` with `stimulus_source=model`. It does
  not run another dialogue generator, invent a reply, create a task or grant
  spending permission. Contradictory silence plus a new purchase is rejected.
- In this scene's declared setup phase, owner dialogue and a typed commerce
  decision are both required. The model supplies the terms. Code does not
  extract permission from prose or fill it from `max_spend`. Empty permission
  remains an explicit refusal; it cannot execute a purchase.
- The ordinary semantic review and all owner, budget, inventory and settlement
  checks still apply. Failed candidates and provider errors remain recorded.

These options default off for older profiles. Resume retains the saved scene,
profile and campaign. It does not upgrade an invalid historical run into a
passing experiment.

## Recorded model failures

The [safe JSON](browser-shop-results.json) contains per-invocation scope, model
usage, original checks, candidate diagnostics, conversations and source hashes.
The ignored local run folders and PostgreSQL retain full original records.

| Run | Recorded result |
| --- | --- |
| `13ba2f57-8e32-486b-b219-4f64ed7b3406` | Both mini-model owners omitted new purchase intent or repeated old status. Worker pauses and both invocations remain saved. |
| `ae979d2a-8db7-41a6-8a92-bdb8e3ecf72b` | Resumed under the approved same-campaign increase. A repeated standing grant was rejected before a new effect. |
| `6e0d1d88-25f6-421e-babb-6722df460a2b` | Initial seller work was confused with a purchase, then the coupled review incorrectly required silence at initial setup. |
| `10deddb8-3604-4825-a1a9-48614171226b` | Stronger owner model still omitted a required task description. The old schema had presented it as optional. |
| `7c389f1e-97e3-48bd-8009-20f436f0c234` | The coupled reviewer first requested a wait, then rejected that repaired wait. |
| `20a3c5e5-09dc-4ce8-99c5-d9396a606d05` | Mini stimulus classification contradicted initial setup. The schema failure and resumed failure remain saved. |
| `fc65d5a8-bd43-4216-98ae-37bf26bddb42` | Mini classifier incorrectly treated an explicit new purchase as silence. |
| `1d5bb515-ef36-4b51-a1aa-723ea20e04a5` | Strong classifier correctly chose seller silence. A second owner generator produced nonempty wait text twice, so validation stopped the run. |
| `cfe4bc56-bad5-4078-90fc-eccc5031bf06` | Model owners expressed all three requests and avoided repeated replies, but both initial typed commerce decisions were missing. No listings, orders or debit occurred. The semantic reviewer had accepted prose without permission. This is a failed shopping loop, despite 11 completed windows and no simulator error. |

## Preserved checkpoint before the final continuation

Run `cf53ba50-1506-4b17-8b49-d0a79bdf9af9` uses the v11 typed-setup profile.
Both owners generated their initial trade permissions. The seller listed the
three products. Two actual browser purchases settled and the buyer reported
their exact order IDs:

| Task | Item | Paid credits | Order ID |
| --- | --- | ---: | --- |
| `purchase-0` | Ceramic mug | 12 | `d0c9ce12-16d7-5382-bbae-fc46e8a33b8a` |
| `purchase-1` | Linen notebook | 8 | `c1a1cd3e-7ed3-53da-8f40-74f66bdd847d` |

Balances are Alice 980 and Bob 120. Remaining purchase authority is 24 credits.
The model owner has already opened `purchase-2` for one reading lamp at a total
cap of 24. It has not been purchased. Both assistant requests at logical window
8 failed to complete, without a provider generation ID or confirmed cost. The
runner preserved their checkpoints and retained both maximum cost reservations
as uncertain exposure. It did not advance that failed window.

This invocation completed eight windows in 102.940 seconds, with two runtime
recreations. It made 54 logical model calls: 52 completed and two failed.
Confirmed cost was 0.2447096 USD. The failed requests retained another
0.1923496 USD of uncertain exposure. **The three-purchase acceptance has not
passed.** Earlier failed invocations remain separate.

Observed behavior also remains imperfect. The buyer attempted page controls
before a purchase was active and needed a later turn to select the task. It
issued unnecessary owner questions and did not reopen settled orders before
reporting from actual Director receipts. The seller tried listing remaining
stock again after a sale. These are retained assistant behavior limits; two
correct settlements do not establish natural or stable behavior.

The local observer shows the saved failure at
`/console?run=cf53ba50-1506-4b17-8b49-d0a79bdf9af9&turn=8`.
The read-only order page is
`/shop?run=cf53ba50-1506-4b17-8b49-d0a79bdf9af9&observer=1&owner=alice#orders`.
Both were inspected in the in-app browser. The order page shows the two accepted
receipts, the pending new lamp task, remaining authority and paused status.
[Saved screenshot](images/browser-shop-model-paused.png).

The ready resume command, after sufficient budget is explicitly authorized, is:

```sh
uv run python scripts/try_browser_shop.py \
  --resume .local/browser-shop-runs/cf53ba50-1506-4b17-8b49-d0a79bdf9af9/credentials.json \
  --restart-every 3 --campaign 635aa42e-386f-43dc-b679-b73cd0cd276f
```

This resumes the same scene, permissions, task, session, call counters and
ledger. It does not replay completed purchases or replace model owners with
supplied decisions.

## Paced process-resume control

Run `370bc9d3-2923-4213-b5bd-87a573df1c40` uses supplied control decisions and
actual Agent graphs, Chrome, HTTP, Director settlement and PostgreSQL. It has
zero provider calls and makes no claim about model behavior.

The first process stopped intentionally after 16 completed windows. Its saved
report records 40.844 seconds, three runtime/browser recreations and one
recovery after a committed checkout lost its response. A separate CLI process
resumed the same credential manifest and completed all 92 windows and 30 orders.
That invocation took 220.641 seconds and had 13 runtime/browser recreations.
All 11 shopping checks pass. The buyer ends at 560 credits and the seller at
540; total credits remain 1,100. Active invocation time totals 261.485 seconds.
The elapsed time between invocations includes an operator pause and is not
reported as active processing.

`--step-interval-seconds 2` uses the last committed PostgreSQL completion time.
A recreated process uses that existing deadline. Waiting checks operator pause
at least once per second, holds no database transaction and makes no model call.
`--stop-after-windows 16` saves an intentional pause. A later invocation needs
explicit `--resume`; it cannot auto-resume an unrelated operator pause.

Each CLI invocation gets an immutable report. Earlier reports are retained
before `report.json` is replaced. Historical exported checks and traces use that
invocation's recorded state; current-run checks are labeled separately.

This is a paced recovery check lasting minutes. It is not multi-hour uptime or
a 15-browser load test. See the runtime guide for reproduction commands.

## Budget before the final continuation · historical snapshot

The human approved increasing the same campaign from 4 to 5 USD. The direct
reply and before/after ledger snapshots are in
[the authorization record](browser-shop-budget-authorization.json).
All roles, repairs, retries and resumed invocations share campaign
`635aa42e-386f-43dc-b679-b73cd0cd276f`. Earlier confirmed spending and the
0.2156256 USD uncertain exposure remain counted. The runner never creates a
replacement campaign or raises its ceiling. The latest safe JSON includes
confirmed cost, outstanding reservations and available budget.

The saved final ledger has 1,392 attempts, 4.5054840 USD confirmed,
0.4079752 USD uncertain, zero live reservations and 0.0865408 USD available.
The next unchanged buyer request requires a maximum reservation of 0.1144952
USD. No further paid call is started under the present 5 USD ceiling. The human
explicitly chose “保持 5 美元，暂不继续付费测试” after reviewing the saved
checkpoint. The proposed 5.5 USD increase was declined. Paid testing is stopped;
any later paid resume needs new human authorization. The run and all earlier
evidence remain intact.

## Verification

The isolated PostgreSQL full suite passed 1,107 tests in 155.23 seconds after the
model-yield and classification-recovery fixes. The later typed-setup requirement
passed four direct role checks and then the focused 129-test role, Agent,
browser, checkpoint and branch suite in 29.10 seconds. These checks overlap;
their counts are not added. Ruff, formatting and whitespace checks passed.
No test resets the development database.

New checks cover missing purchase intent, declared initial prices and stock,
provider failure without a supplied fallback, required task fields, fixed
view-only stimulus review, effect-free model silence, contradictory classification,
recorded schema repair, missing initial typed authority, durable pacing and
operator pause. The 30-order control additionally exercised real process resume
and a lost response after checkout committed.
