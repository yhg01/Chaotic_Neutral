"""Price the prepared payloads from archived tariffs with the existing budget implementation."""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

DEFAULT_ADAPTERS = Path("../zeroclaw-comparison/contributor/scripts")
SNAPSHOT = Path("../judge-evaluations/research-group-live/live-v41-01/protocol.json")


def archived_price(budget, data):
    return budget.ModelPrice(
        data["model"],
        Decimal(data["prompt_per_token_usd"]),
        Decimal(data["completion_per_token_usd"]),
        Decimal(data["per_request_usd"]),
        data["context_length"],
        data["fetched_at"],
    )


def amounts(budget, price, payload, planned_output):
    size = len(json.dumps(payload["messages"], ensure_ascii=False).encode())
    _, bound, output = budget.prepare_request(dict(payload), {price.model: price})
    return price.maximum((size + 3) // 4, min(planned_output, output)), price.maximum(bound, output)


def quote(judge, packets, adapters=DEFAULT_ADAPTERS, snapshot=SNAPSHOT):
    sys.path.insert(0, str(adapters.resolve()))
    from clock_group_buy_live import load_main_budget

    budget = load_main_budget()
    key = "agent" if judge == "flash" else "reviewer"
    price = archived_price(budget, json.loads(snapshot.read_bytes())[key])
    return {
        cid: amounts(budget, price, payload, 700 if judge == "flash" else 900)
        for cid, payload in packets.items()
    }


def alternatives(study, ids, adapters=DEFAULT_ADAPTERS):
    """Replay saved fetch_model_prices snapshots through the original hold calculation."""
    sys.path.insert(0, str(adapters.resolve()))
    from clock_group_buy_live import load_main_budget

    budget = load_main_budget()
    catalog = json.loads(Path("docs/model-comparison-results.json").read_bytes())["plan"]["prices"]
    results = {}
    for model, tariff in sorted(catalog.items()):
        if model.startswith("deepseek/"):
            continue
        price = archived_price(budget, tariff)
        values = []
        for cid in ids:
            payload = json.loads((study / "flash" / (cid + ".json")).read_bytes())
            payload.update(model=model, max_tokens=900)
            payload.pop("reasoning")
            values.append(amounts(budget, price, payload, 700))
        results[model] = (
            sum(v[0] for v in values),
            min(v[1] for v in values),
            max(v[1] for v in values),
        )
    return results


def estimate(study, adapters, snapshot):
    for judge in ("flash", "astra"):
        packets = {
            p.stem: json.loads(p.read_bytes()) for p in sorted((study / judge).glob("*.json"))
        }
        prices = quote(judge, packets, adapters, snapshot)
        costs = {k: pair[0] for k, pair in prices.items()}
        holds = [pair[1] for pair in prices.values()]
        print(
            judge,
            "pairs",
            len(costs),
            "pilot C001+C007 USD",
            costs["C001"] + costs["C007"],
            "full estimated USD",
            sum(costs.values()),
            "hold range USD",
            min(holds),
            max(holds),
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("study", type=Path)
    p.add_argument(
        "--adapter-dir", type=Path, default=Path("../zeroclaw-comparison/contributor/scripts")
    )
    p.add_argument(
        "--snapshot",
        type=Path,
        default=Path("../judge-evaluations/research-group-live/live-v41-01/protocol.json"),
    )
    p.add_argument("--alternatives", nargs="+", metavar="CASE")
    args = p.parse_args()
    if args.alternatives:
        for m, row in alternatives(args.study, args.alternatives, args.adapter_dir).items():
            print(m, *(str(n) for n in row))
    else:
        estimate(args.study, args.adapter_dir, args.snapshot)
