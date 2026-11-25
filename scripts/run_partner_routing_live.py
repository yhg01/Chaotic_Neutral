"""Run the matched routing pilot using the existing native DeepSeek campaign adapter."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import httpx
from dotenv import dotenv_values
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from try_clock_control import _isolated_schema
from try_clock_partner_routing import audit, load, scenario, schedule, trial

from agentrpg.console_execution import save_private
from agentrpg.migrate import migrate


async def run(adapters, out, preflight, control_dir):
    # Use the installed campaign's ledger and tariff adapter, including its uncertain reserves.
    sys.path.insert(0, str(adapters.resolve()))
    from clock_group_buy_live import CAMPAIGN, CEILING, ROOT, bind_models, load_main_budget
    from clock_group_buy_native import CNY_PER_USD, DEEPSEEK, MODEL, NativeDeepSeek, native_price

    tokens, call_cap = 900, 6
    budget = load_main_budget()
    price = native_price(budget)
    reserve = price.maximum(price.context_length, tokens)
    key = dotenv_values(ROOT / ".env.personal-agent.local", interpolate=False)[
        "PERSONAL_AGENT_API_KEY"
    ]
    main_url = dotenv_values(ROOT / ".env")["DATABASE_URL"]
    test_url = os.environ.get("TEST_DATABASE_URL") or make_conninfo(
        main_url, dbname="agentrpg_test"
    )
    async with httpx.AsyncClient(
        base_url=DEEPSEEK,
        headers={"Authorization": f"Bearer {key}"},
        timeout=15,
        follow_redirects=False,
    ) as client:
        models, balance = await asyncio.gather(client.get("/models"), client.get("/user/balance"))
        for reply in (models, balance):
            reply.raise_for_status()
        wallet = balance.json()
        cny = sum(
            Decimal(v["total_balance"]) for v in wallet["balance_infos"] if v["currency"] == "CNY"
        )
        assert wallet["is_available"] and MODEL in [m["id"] for m in models.json()["data"]]

    class Recorded(NativeDeepSeek):
        def __init__(self, folder):
            super().__init__(price)
            self.folder = folder
            self.count = 0

        async def handle_async_request(self, request):
            self.count += 1
            payload = json.loads(await request.aread())
            assert payload["temperature"] == 0
            response = await super().handle_async_request(request)
            record = {"http_status": response.status_code, "request_messages": payload["messages"]}
            if response.is_success:
                data = response.json()
                record.update({k: data[k] for k in ("id", "model", "usage", "choices")})
            save_private(self.folder / f"provider-{self.count:02}.json", record)
            return response

    async with AsyncConnectionPool(main_url, kwargs={"row_factory": dict_row}, timeout=5) as pool:
        ledger = budget.CampaignLedger(pool, expected_limit_usd=CEILING)
        before = await ledger.summary(CAMPAIGN, recent_limit=0)
        assert CEILING == 25 and not before["halted"] and before["limit_usd"] == CEILING
        assert before["reserved_usd"] == 0 and before["remaining_usd"] >= reserve
        assert cny / CNY_PER_USD >= reserve
        print(
            f"Original campaign available USD {before['remaining_usd']}; provider CNY {cny}; "
            f"maximum single reservation USD {reserve}",
            flush=True,
        )
        if preflight:
            return
        controls = load(control_dir, "results")
        assert {r["first"] for r in controls} == {"desk-a", "desk-b", "both", "none"}
        for control in controls:
            assert audit(control_dir / control["policy"]) == control
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        plan = list(schedule())
        scripts = [
            Path(__file__),
            Path(__file__).with_name("try_clock_partner_routing.py"),
            adapters / "clock_group_buy_live.py",
            adapters / "clock_group_buy_native.py",
        ]
        protocol = {
            "campaign": str(CAMPAIGN),
            "starting_budget": before,
            "provider_cny": cny,
            "tariff": price.snapshot(),
            "maximum_per_request_usd": reserve,
            "model": MODEL,
            "temperature": 0,
            "thinking": "disabled",
            "judge": None,
            "tokens": tokens,
            "max_calls_per_case": call_cap,
            "outcome": "First successful email batch, with both and neither retained.",
            "training": (
                "Ten scripted booking exchanges with read receipts. Within a fixed contact order, "
                "only the confirmation times vary."
            ),
            "schedule": plan,
            "hashes": {s.name: hashlib.sha256(s.read_bytes()).hexdigest() for s in scripts},
            "scenario_hashes": {
                r["case"]: scenario(
                    r["history"], r["task"], r["history_first"], r["listed_first"]
                ).content_hash()
                for r in plan
            },
        }
        save_private(out / "protocol.json", protocol)

        class Guard:
            async def reserve(self, *a, **kw):
                state = await ledger.summary(CAMPAIGN, recent_limit=0)
                if state["uncertain_usd"] != before["uncertain_usd"]:
                    raise budget.BudgetExceeded("New unsettled cost; inspect the ledger.")
                return await ledger.reserve(*a, **kw)

            async def finish(self, *a, **kw):
                return await ledger.finish(*a, **kw)

        roles, results = [], []
        try:
            async with _isolated_schema(test_url) as db:
                await migrate(db)
                for row in plan:
                    folder = out / row["case"]
                    role = f"{out.name}-{row['case']}"
                    roles.append(role)
                    model = bind_models(
                        budget,
                        Guard(),
                        {price.model: price},
                        price.model,
                        key,
                        tokens,
                        call_cap,
                        role,
                    )
                    transport = model._budget_clients[1]._transport
                    await transport.transport.aclose()
                    transport.transport = Recorded(folder)
                    try:
                        item = await trial(db, row, folder, live=model)
                    finally:
                        await model.aclose()
                    results.append(item)
                    save_private(out / "results.json", results)
                    print(f"{len(results)}/{len(plan)} {row['case']} {item['first']}", flush=True)
        finally:
            after = await ledger.summary(CAMPAIGN, recent_limit=0)
            async with pool.connection() as conn:
                charges = await (
                    await conn.execute(
                        "SELECT id,role,model,status,reserved_usd,reported_usd,generation_id "
                        "FROM model_spend_attempts WHERE campaign_id=%s AND role=ANY(%s) "
                        "ORDER BY created_at,id",
                        (CAMPAIGN, roles),
                    )
                ).fetchall()
            save_private(
                out / "budget.json", {"before": before, "after": after, "attempts": charges}
            )
            print("Remaining USD", after["remaining_usd"], "requests", len(charges), flush=True)


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Evidence checks require assertions.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-dir", required=True, type=Path)
    parser.add_argument("--controls", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out and not args.controls:
        parser.error("--out requires --controls with verified free controls")
    os.umask(0o077)
    asyncio.run(run(args.adapter_dir, args.out, args.preflight, args.controls))
