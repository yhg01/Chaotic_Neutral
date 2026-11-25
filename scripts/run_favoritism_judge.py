"""Inspect one exact prepared step locally, or execute it after explicit chat approval."""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from functools import partial
from pathlib import Path

import httpx
from dotenv import dotenv_values
from favoritism_judge import RUBRIC, episode, load
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.console_execution import save_private


def requests(study, judge, ids):
    keys = load(study, "private-key")
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Supply distinct blind IDs.")
    result = {}
    for cid in ids:
        key = keys[cid]
        card = {
            "case_id": cid,
            "episodes": {tag: episode(Path(m["path"]))[0] for tag, m in key["mapping"].items()},
        }
        payload = load(study / judge, cid)
        turns = payload["messages"]
        assert len(turns) == 2 and turns[0] == {"role": "system", "content": RUBRIC}
        assert turns[1]["role"] == "user" and json.loads(turns[1]["content"]) == card
        result[cid] = payload
    return result


async def execute(args, packets):
    sys.path.insert(0, str(args.adapter_dir.resolve()))
    from clock_group_buy_live import CAMPAIGN, CEILING, ROOT, load_main_budget
    from clock_group_buy_native import NativeDeepSeek, native_price

    budget = load_main_budget()
    flash = args.judge == "flash"
    if flash:
        price = native_price(budget)
        upstream = partial(NativeDeepSeek, price)
        key = dotenv_values(ROOT / ".env.personal-agent.local", interpolate=False)[
            "PERSONAL_AGENT_API_KEY"
        ]
    else:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as http:
            price = (await budget.fetch_model_prices({budget.ASTRA_CRITIC}, client=http))[
                budget.ASTRA_CRITIC
            ]
        upstream = httpx.AsyncHTTPTransport
        key = dotenv_values(ROOT / ".env.actor-reviewer.local", interpolate=False)[
            "ACTOR_REVIEWER_API_KEY"
        ]
    prices = {price.model: price}
    holds = []
    for payload in packets.values():
        assert payload["model"] == price.model
        _, bound, output = budget.prepare_request(dict(payload), prices)
        holds.append(price.maximum(bound, output))
    main = dotenv_values(ROOT / ".env")["DATABASE_URL"]
    async with AsyncConnectionPool(main, kwargs={"row_factory": dict_row}, timeout=5) as pool:
        ledger = budget.CampaignLedger(pool, expected_limit_usd=CEILING)
        before = await ledger.summary(CAMPAIGN, recent_limit=0)
        assert (
            CEILING == 25
            and before["limit_usd"] == CEILING
            and not before["halted"]
            and before["reserved_usd"] == 0
        )
        estimate = Decimal("0.003" if flash else "0.17") * len(packets)
        if before["remaining_usd"] < max(holds) + estimate:
            raise ValueError("Step exceeds the existing campaign headroom.")
        args.out.mkdir(parents=True, mode=0o700, exist_ok=False)
        try:
            for cid, payload in packets.items():
                role = f"favoritism-v1-{args.judge}-{cid}"
                guard = budget.BudgetBinding(ledger, CAMPAIGN, prices, max_role_attempts=1)
                async with httpx.AsyncClient(
                    transport=budget.BudgetTransport(guard, role, transport=upstream()),
                    timeout=90,
                    follow_redirects=False,
                ) as client:
                    r = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json=payload,
                    )
                    r.raise_for_status()
                    response = r.json()
                    save_private(args.out / (cid + "-provider.json"), response)
                    save_private(
                        args.out / (cid + ".json"),
                        json.loads(response["choices"][0]["message"]["content"]),
                    )
                current = await ledger.summary(CAMPAIGN, recent_limit=0)
                if current["uncertain_usd"] != before["uncertain_usd"] or current["halted"]:
                    raise ValueError("Unsettled cost or campaign pause.")
        finally:
            after = await ledger.summary(CAMPAIGN, recent_limit=2 * len(packets) + 2)
            save_private(args.out / "budget.json", {"before": before, "after": after})


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Checks require assertions.")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("study", type=Path)
    p.add_argument("judge", choices=("flash", "astra"))
    p.add_argument("cases", nargs="+")
    p.add_argument(
        "--adapter-dir", type=Path, default=Path("../zeroclaw-comparison/contributor/scripts")
    )
    p.add_argument("--out", type=Path)
    p.add_argument(
        "--execute", action="store_true", help="requires advance approval for this exact step"
    )
    args = p.parse_args()
    packets = requests(args.study, args.judge, args.cases)
    if args.execute:
        if args.out is None:
            p.error("Provide a new output directory.")
        asyncio.run(execute(args, packets))
    else:
        print(f"Verified {len(packets)} prepared {args.judge} requests offline.")
