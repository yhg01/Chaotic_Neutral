# Local browser shopping and continuous execution

The browser-shopping pipeline works in the existing FastAPI/PostgreSQL project.
The current model acceptance uses a GPT-4.1-mini buyer with supplied owner Actors
and a supplied seller Agent. Longer behavior with model Actors remains open.
The user deferred the separate requirement to add several task types.
The [model-owner continuation](BROWSER_SHOP_MODEL_RESULTS.md) records later
model failures, typed setup decisions, paced execution and process resume.
The five-run table below is the original E-36 snapshot, not the latest campaign.

## What changed

- `/shop` is a local storefront with product search, details, task selection,
  quantities, cart review and order history. The human observer can inspect the
  shop through `?run=<id>&observer=1&owner=alice` and follow its trajectory link.
- A buyer can use `open_shop`, `inspect_shop_page`, `shop_fill` and `shop_click`.
  These tools control installed Chrome through Playwright. The buyer profile
  has no direct market-buy, transfer or world-action tools.
- Each personal Agent has its own ephemeral browser context. A transport adapter
  adds that Agent's credential only to allowed loopback requests. URLs, arbitrary
  JavaScript, external sites, observer APIs and peer runs are unavailable to the
  model. The page and its snapshots contain no credential.
- Actors submit typed shopping tasks through the Director. Task quantity and
  total caps do not grant spending authority. Existing `commerce_consent`,
  balances, inventory, listing versions and settlement remain authoritative.
- A checkout and all its market intents commit in one transaction. Order lines
  reference the existing market receipts. A pending order is not payment.
  The Director settles intents and derives each task's next state from receipts.
- Carts, orders, task state and operation records persist in migration 015.
  Stable graph call IDs recover a committed form submission after a lost browser
  response. A unique cart version and task allocation caps prevent repeated buys.
- The normal Actor/Agent/settlement cycle handles later independent requests.
  Runtime and browser reconstruction preserves sessions and graph checkpoints.
  Operator pauses remain pauses. Only an explicitly injected checkout failure
  is recovered automatically by the validation runner.
- Shopping tables are included in complete turn snapshots and isolated child
  copies. Old shop boundaries without these tables cannot be branched silently.
  Live browser state is recreated, not copied into another Agent's context.

The existing diagram remains unchanged: shopping implements the declared
Actor requests, Agent tools, Director, Resource, database and Clock links.

## Reproduce

Use an installed Google Chrome and the optional browser dependencies:

```sh
uv sync --extra browser --locked
uv run python -m agentrpg.migrate
uv run python -m agentrpg
```

In another terminal, run local control decisions without provider calls:

```sh
uv run python scripts/try_browser_shop.py --mode control --rounds 30 \
  --restart-every 6 --inject-checkout-interruption
```

For a model buyer, use the existing authorized campaign and local `.env` key:

```sh
uv run python scripts/try_browser_shop.py --mode model --rounds 3 \
  --restart-every 4 --campaign <existing-campaign-uuid>
```

This command never creates a campaign or raises its ceiling. It reserves every
provider request in the same ledger as the prior transfer experiments. Original
spend and uncertain exposure remain charged. The supplied Actors and merchant
do not make provider requests. Use `--origin` for another loopback API port.

To include model owners and a model seller, use the explicit separate profile:

```sh
uv run python scripts/try_browser_shop.py --mode model-actors --rounds 3 \
  --restart-every 3 --campaign <existing-campaign-uuid>
```

This uses GPT-4.1 for owner generation, review and later stimulus decisions,
and GPT-4.1-mini for both assistants. It is a paid model experiment, not the
supplied control. The saved configuration records every selected policy.

To check paced execution and a separate process resume without provider cost:

```sh
uv run python scripts/try_browser_shop.py --mode control --rounds 30 \
  --restart-every 6 --inject-checkout-interruption \
  --step-interval-seconds 2 --stop-after-windows 16
uv run python scripts/try_browser_shop.py \
  --resume .local/browser-shop-runs/<run-id>/credentials.json --restart-every 6
```

The interval is frozen in the original manifest. Waiting uses the saved database
deadline and checks operator pause every second. Each invocation preserves its
own immutable report, including an intentional partial stop. The complete
30-order process-resume result is in the model-owner continuation.

Private manifests and complete reports are in
`.local/browser-shop-runs/<run-id>/`. Use `--resume <credentials.json>` to resume
a saved paused run with the same identities, original instructions and campaign.
Do not edit a completed run into a new experiment. The general console worker
does not execute this browser profile; use this runner. Console replay remains
available. The observer is read-only and cannot place an order.

## Evidence on 3 November 2026

[Safe results](browser-shop-results.json) preserve five runs, original checks,
current stricter checks, conversations, orders, tool calls, failures, budget and
SHA-256 hashes of full local reports. PostgreSQL and the ignored run folders
retain the complete original trajectories. The exporter is
`scripts/report_browser_shop.py`.

| Run | Scope | Result |
| --- | --- | --- |
| `1b84b3c5-ae19-450c-96f2-953b79b319eb` | Three-round supplied control | Paused on the second request. The control policy repeatedly selected an already selected UI task instead of adding its item. Fixed the control policy; the failed run remains intact. |
| `40c10bfd-ffd3-43c0-93fd-d8f756215c21` | Thirty-round supplied control | Completed 26 orders, then invalid at window 80: all historical tasks exceeded one 12,000-character observation. This was a simulator input failure. |
| `0b133f6d-af6c-4d68-8506-3082659cd2c7` | Thirty-round supplied control after repair | 30 orders and reports; all 11 current checks pass. 92 completed logical windows in 86.055 seconds. 16 runtime/browser recreations, including one recovery after committed checkout. Buyer/seller balances 560/540; total 1,100 conserved. No provider calls. |
| `4eb2308e-3868-4c66-8bae-0b124585acb5` | Real model buyer, three requests | Three purchases settled correctly, balances 956/144, but reports omitted order IDs. Strict reporting checks fail. 20 calls, 0.0774176 USD. The report saved locally, then a UUID JSON encoding error prevented its database report write; fixed serialization. |
| `2d258295-b8af-4e4f-8e08-22e6880fb910` | Real model buyer with explicit receipt-ID report format | Three browser purchases and exact owner reports pass all 11 current checks. 11 completed logical windows in 32.390 seconds; two runtime recreations. 20 calls, 333,438 input + 562 output tokens, 0.0797464 USD. Balances 956/144. |

The input repair keeps every unresolved task and the five latest closed tasks,
with one bounded observation per task. The Director reserves envelope slots for
these records. Complete history remains available in persisted orders and the
storefront; it is not placed in one ever-growing observation.

The model buyer used the webpage to submit each purchase. After settlement it
reported from the actual accepted Director receipt events. It did not reopen the
order page after settlement, despite that instruction. This is a recorded limit
of workflow adherence, not an unsupported payment claim. The browser integration
test separately verifies refreshing and reading settled orders.

The original 4 USD campaign now has 1,178 attempts: 3.6454496 USD confirmed,
0.2156256 USD uncertain, zero live reservations and 0.1389248 USD available.
These two browser-model runs added 40 attempts and 0.1571640 USD confirmed.

## Verification and limits

The isolated PostgreSQL suite passed 1,088 tests in 155.45 seconds. Focused
shopping/browser/branch checks passed 26 tests before that run. They cover
concurrent checkout, payload conflicts, task and consent separation, stale stock
versions, owner boundaries, real Chrome, graph replay after committed checkout,
repeated legitimate purchases and isolated copied receipts. The final focused rerun passed 26 tests in 22.40 seconds after the last code
edits. Ruff, formatting, JavaScript syntax and whitespace checks pass.

Browser inspection covers desktop, a 390 × 844 viewport, settled orders, empty
search results, invalid run input and read-only observer controls. Screenshots:
[desktop orders](images/browser-shop-orders.png) and
[mobile orders](images/browser-shop-mobile.png), and
[the shopping trajectory](images/browser-shop-trajectory.png).

These checks establish the local browser pipeline and bounded continuous
recovery. They do not establish multi-hour uptime, natural model-Actor behavior,
15 simultaneous browsers, varied external sites or broad model reliability.
The 30-order control uses 92 logical windows in under two minutes; it is not a
multi-hour model experiment. Further long model runs need budget headroom in the
same campaign and a separate stated duration and acceptance criterion.
