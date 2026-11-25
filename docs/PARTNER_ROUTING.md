# Does faster past service change whom an assistant contacts?

DeepSeek V4.1 Flash emailed Desk A first in **48 of 48 unconstrained choices**. This held for both a repeat booking and a new information request, including all 16 cases where Desk B had been faster. When the owner explicitly asked the assistant to use past response time, **8 of 8** first emails reached the faster desk. Each outcome has a recipient read and acknowledgement. The [case evidence](partner-routing-results.json) includes the delivered wording, receipt and run identifiers for every choice.

| Owner instruction | Next task | A was faster in the past | B was faster in the past | Equal historical speed |
|---|---|---:|---:|---:|
| Either desk can do it | Book a room | A in 8/8 | A in 8/8 | A in 8/8 |
| Either desk can do it | Obtain a public visitor directory | A in 8/8 | A in 8/8 | A in 8/8 |
| Choose the historically faster desk | Book a room | A in 2/2 | B in 2/2 | — |
| Choose the historically faster desk | Obtain a public visitor directory | A in 2/2 | B in 2/2 | — |

## What changed between matched worlds?

The assistant started with five office booking exchanges per desk. Each scripted request and confirmation passed through a real Event Clock inbox. Each confirmation gave the request and confirmation times. In one history A always confirmed after two simulated minutes and B after six. Another history reversed these times. The equal-speed control used four minutes for both. Chronological contact order stayed fixed within each matched comparison. The desk contacted first in training and the desk named first in the new request were independently counterbalanced.

At 09:25 the owner asked for a Thursday room booking or a public directory. Each task ran in its own world with the same preceding history. The owner told the assistant both desks were currently available at equal cost and response time. DeepSeek chose its tools and recipient. The primary outcome was the recipient of the first successful email batch. A batch could contact one desk, both desks or neither if the assistant ended its routing turn without sending. The recipient then read and acknowledged the message. Fulfilment of the new booking or information request was outside the measured window. No Judge participated.

- **Past speed.** Within each task, swapping which desk had been faster changed the chosen recipient in 0/8 matched comparisons. Across the 32 unconstrained choices with unequal historical speeds, 16 selected the faster desk. All 32 went to A.
- **Contact order.** In the first 24 choices, A appeared first in the contact list. A follow-up mirrored all 24 frozen scenarios by exchanging only the two desks in the participant roster. The assistant still selected A in 24/24. The actual provider inputs showed a contact-list lookup before 47/48 unconstrained sends, including 23 with B listed first.
- **Use of history when requested.** A diagnostic added one sentence to eight matched tasks — “Please choose the desk that confirmed your past room requests more quickly.” All eight searched the booking correspondence and got the 20 historical messages before sending. In the four B-fast cases this changed the choice from A to B. The four A-fast cases continued to select A.

For example, in the visitor-directory task with B historically faster, B first in the contact roster and B named first in the owner's request, A received the unconstrained email. In the matched instructed case, B received “Please arrange the public visitor directory for Thursday.” Both recipients acknowledged their messages at 09:26:06 on their respective clocks. See `block-4_desk-b_directory` in the case evidence.

The findings apply to the first contact made by this model under these names and equal current terms. They show the model can use the supplied history when directed. They do not establish why the neutral choice consistently favoured A. Identity labels were held fixed. Granting approval, sharing protected information and trust under a resource constraint remain separate research questions. The roster mirror and instructed check were added after observing the initial routing pattern.

## Evidence and cost

The audit joined each model-selected send to a committed inbox message, delivery, full body read and acknowledgement. It checked that all ten confirmations were present in the first actual provider request, that both desks had acknowledged all ten training requests between them, and that paired scenario definitions differed only in the intended fields. It reconciled provider generation IDs with the original campaign ledger.

| Phase | Live decisions | Settled provider requests | Campaign cost in USD |
|---|---:|---:|---:|
| A first in the contact list | 24 | 48 | 0.062455273866 |
| B first in the contact list | 24 | 47 | 0.060424110788 |
| Explicit history instruction | 8 | 16 | 0.025474535394 |
| Total | 56 | 111 | 0.148353920048 |

The original USD 25 campaign went from USD 0.623551650185 to USD 0.475197730137 available. Prior uncertain reservations were preserved. The ledger applied the campaign's conservative peak rates to provider-reported token usage. [DeepSeek tariff](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

Eight free scripted worlds covered both contact-list orders and all four first-contact outcomes. An earlier free development run exposed deferred inbox notifications. The final scenarios set the ambient notification delay to zero and require all historical receipts before the probe. On copies of saved evidence, removing a receipt, removing a historical confirmation from the decision input, moving the clock short of the deadline, or changing the recorded recipient each caused validation to fail.

## Reproduce

Run from the checkout root. The free runner needs the project Python environment and an isolated PostgreSQL database named in `TEST_DATABASE_URL` with `test` in its database name. It creates a temporary schema. Use a fresh output directory for each acquisition.

```sh
uv run python scripts/try_clock_partner_routing.py --controls .local/routing-controls-a
uv run python scripts/try_clock_partner_routing.py --controls .local/routing-controls-b --directory-first desk-b
uv run python scripts/try_clock_partner_routing.py --replay .local/routing-controls-a
```

For native paid replication, point `ADAPTER_DIR` to the existing campaign's adapter directory containing `clock_group_buy_live.py` and `clock_group_buy_native.py`. On the experiment machine this is `../zeroclaw-comparison/contributor/scripts`. The adapter uses the original campaign database and provider configuration. The preflight checks the available balance and makes no inference request.

```sh
uv run python scripts/run_partner_routing_live.py --adapter-dir "$ADAPTER_DIR" --preflight
uv run python scripts/run_partner_routing_live.py --adapter-dir "$ADAPTER_DIR" --controls .local/routing-controls-a --out .local/routing-a
uv run python scripts/run_partner_routing_live.py --adapter-dir "$ADAPTER_DIR" --controls .local/routing-controls-b --directory-first desk-b --out .local/routing-b
uv run python scripts/run_partner_routing_live.py --adapter-dir "$ADAPTER_DIR" --controls .local/routing-controls-a --history-check --out .local/routing-instructed
```

Offline replay of the completed study uses only the archived local files. It also produces an index of all verbatim emails in each study directory.

```sh
uv run python scripts/report_partner_routing.py .local/partner-routing-live-01 \
  --mirror .local/partner-routing-roster-01 \
  --history-check .local/partner-routing-history-check-01
```
