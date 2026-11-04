"""Read saved transfer/shopping evidence. No execution, recovery or model requests."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentrpg.autonomy_validation import acceptance_report, collect_run, markdown_report
from agentrpg.config import Settings


async def main(args):
    selections = []
    for entry in args.run:
        try:
            profile, case, run = entry.split(":")
            if profile not in {"transfer", "shopping"}:
                raise ValueError
            selections.append((profile, case, UUID(run)))
        except ValueError:
            raise SystemExit("Use transfer:CASE:UUID or shopping:ROUNDS:UUID.") from None
    if len({r[2] for r in selections}) != len(selections):
        raise SystemExit("Duplicate run IDs cannot count as independent trials.")
    async with AsyncConnectionPool(
        Settings().database_url.get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"row_factory": dict_row},
    ) as pool:
        runs = [
            await collect_run(pool, run, profile=profile, case=case)
            for profile, case, run in selections
        ]
    result = acceptance_report(runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown_report(result))
    print(
        json.dumps(
            {
                "report": str(args.output),
                "unique_runs": result["unique_runs"],
                "model_repeat_gate_passed": result["model_repeat_gate_passed"],
                "long_term_autonomy_verified": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="PROFILE:CASE:RUN_UUID")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
