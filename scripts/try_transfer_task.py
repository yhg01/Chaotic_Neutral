"""Run the fixed local payment check. No model calls and no real money."""

import asyncio
import json
import os
from pathlib import Path

from agentrpg.config import Settings
from agentrpg.main import create_app
from agentrpg.migrate import migrate
from agentrpg.transfer_demo import run_local_check

ROOT = Path(__file__).resolve().parents[1]


async def main():
    settings = Settings()
    await migrate(settings.database_url.get_secret_value())
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        report = await run_local_check(app)
    folder = ROOT / ".local" / "transfer-tasks" / report["run_id"]
    folder.mkdir(parents=True, mode=0o700)
    path = folder / "report.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "mode",
                    "model_calls",
                    "run_id",
                    "initial_balances",
                    "final_balances",
                    "checks",
                    "passed",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Report: {path}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
