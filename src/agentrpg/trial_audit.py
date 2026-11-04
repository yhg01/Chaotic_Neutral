"""Credential-free invocation boundaries for repeatability and duration evidence."""

import asyncio
from pathlib import Path
from uuid import uuid4

from agentrpg.director import Director, emit, fingerprint


def source_fingerprint(root):
    root = Path(root)
    files = sorted(root.glob("src/agentrpg/**/*.py")) + sorted(root.glob("scripts/*.py"))
    files += sorted(p for p in root.glob("src/agentrpg/static/*") if p.is_file())
    files += sorted(root.glob("migrations/*.sql"))
    files += [p for p in (root / "pyproject.toml", root / "uv.lock") if p.is_file()]
    if not files:
        raise ValueError("No runtime sources were found.")
    return fingerprint({str(p.relative_to(root)): p.read_text() for p in files})


async def begin_trial(pool, run_id, *, profile, configuration, root):
    invocation = uuid4()
    # Only hashes leave the runner. Do not save prompts or credentials in audit events.
    configuration = {
        k: v for k, v in configuration.items() if k not in {"campaign_id", "budget_usd"}
    }
    run = await Director(pool).get_run(run_id)
    source_hash = await asyncio.to_thread(source_fingerprint, root)
    async with pool.connection() as conn:
        await emit(
            conn,
            run_id,
            "trial_started",
            run["current_window"],
            {
                "revision": 1,
                "invocation_id": str(invocation),
                "profile": profile,
                "decision_mode": (
                    "control"
                    if configuration.get("mode") == "control"
                    or configuration.get("decisions") == "supplied"
                    else "model"
                ),
                "configuration_hash": fingerprint(configuration),
                "source_hash": source_hash,
            },
            f"trial_started:{invocation}",
        )
    return invocation


async def end_trial(pool, run_id, invocation, *, root, returned_report):
    run = await Director(pool).get_run(run_id)
    source_hash = await asyncio.to_thread(source_fingerprint, root)
    async with pool.connection() as conn:
        await emit(
            conn,
            run_id,
            "trial_finished",
            run["current_window"],
            {
                "invocation_id": str(invocation),
                "source_hash": source_hash,
                "returned_report": returned_report,
            },
            f"trial_finished:{invocation}",
        )
