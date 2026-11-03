"""Save funded/unfunded local controls. No model calls or real money."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from agentrpg.config import Settings
from agentrpg.console_execution import save_private
from agentrpg.funding_control import run_funding_control
from agentrpg.main import create_app
from agentrpg.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]


async def main():
    settings = Settings()
    await migrate(settings.database_url.get_secret_value())
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runs = [await run_funding_control(app, deal=deal) for deal in (True, False)]
    path = ROOT / ".local/funding-controls" / str(uuid4()) / "report.json"
    report = {"mode": "supplied_control", "runs": runs, "passed": all(r["passed"] for r in runs)}
    save_private(path, report)
    print(json.dumps({"report": str(path), "passed": report["passed"], "runs": runs}, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
