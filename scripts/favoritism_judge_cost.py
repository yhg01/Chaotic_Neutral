"""Price the prepared payloads from archived tariffs with the existing budget implementation."""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path


def estimate(study, adapters, snapshot):
    sys.path.insert(0, str(adapters.resolve()))
    from clock_group_buy_live import load_main_budget

    budget = load_main_budget()
    tariffs = json.loads(snapshot.read_bytes())
    for judge, section in (("flash", "agent"), ("astra", "reviewer")):
        s = tariffs[section]
        price = budget.ModelPrice(
            s["model"],
            Decimal(s["prompt_per_token_usd"]),
            Decimal(s["completion_per_token_usd"]),
            Decimal(s["per_request_usd"]),
            s["context_length"],
            s["fetched_at"],
        )
        costs, holds = {}, []
        for path in sorted((study / judge).glob("*.json")):
            payload = json.loads(path.read_bytes())
            size = len(json.dumps(payload["messages"], ensure_ascii=False).encode())
            _, bound, output = budget.prepare_request(payload, {price.model: price})
            holds.append(price.maximum(bound, output))
            costs[path.stem] = price.maximum((size + 3) // 4, 700 if judge == "flash" else 900)
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
    args = p.parse_args()
    estimate(args.study, args.adapter_dir, args.snapshot)
