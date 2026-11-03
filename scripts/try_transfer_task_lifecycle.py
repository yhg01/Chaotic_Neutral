"""Save three bounded local lifecycle checks, with no external model calls."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.main import create_app
from agentrpg.migrate import migrate
from agentrpg.transfer_task_demo import run_lifecycle_case

ROOT = Path(__file__).resolve().parents[1]


async def main():
    settings = Settings()
    await migrate(settings.database_url.get_secret_value())
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        reports = [await run_lifecycle_case(app, case) for case in ("ack", "amount", "cancel")]
    path = ROOT / ".local/transfer-task-lifecycle" / str(uuid4()) / "report.json"
    result = {
        "mode": "deterministic_local_transfer_task_lifecycle",
        "model_calls": 0,
        "passed": all(r["passed"] for r in reports),
        "runs": reports,
    }
    save_private(path, result)
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "report": str(path),
                "runs": [
                    {k: r[k] for k in ("case", "run_id", "checks", "passed", "replay_url")}
                    for r in reports
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
