"""Keep one clocked run going through crashes and outages. Makes no model calls itself.

It starts scripts/run_clocked.py, finds the saved manifest, and resumes after a failure or a
temporary pause, with a pause between tries. It stops when the run completes, when the budget
is used up, or after the retry limit. Run it outside the app session, under caffeinate.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINAL = {"completed", "invalid", "incomplete"}


def stamp() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--retries", type=int, default=40)
    parser.add_argument("--delay", type=float, default=120.0)
    args, passed = parser.parse_known_args()
    folder = ROOT / ".local/overnight"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"{args.name}.log"
    state = folder / f"{args.name}.json"
    manifest = json.loads(state.read_text()).get("resume") if state.exists() else None
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    for attempt in range(args.retries + 1):
        command = [sys.executable, "scripts/run_clocked.py"]
        command += ["--resume", manifest] if manifest else passed
        with log.open("a") as out:
            out.write(f"\n=== {stamp()} attempt {attempt}: {' '.join(command[1:4])}\n")
            out.flush()
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in process.stdout:
                out.write(line)
                out.flush()
                if line.startswith("Resume: ") and not manifest:
                    manifest = line.split(" ", 1)[1].strip()
                    state.write_text(json.dumps({"resume": manifest}))
            code = process.wait()
        status, reason = None, None
        if manifest:
            report = Path(manifest).parent / "report.json"
            try:
                saved = json.loads(report.read_text())
                status = saved.get("status")
                reason = (saved.get("result") or {}).get("reason")
            except (OSError, ValueError):
                pass
        with log.open("a") as out:
            out.write(f"=== {stamp()} exit {code}, status {status}, reason {reason}\n")
        if status in FINAL:
            return 0
        if reason in {"budget", "advance_limit", "operator_pause"}:
            return 0
        if manifest is None and code != 0:
            with log.open("a") as out:
                out.write("=== no manifest was saved, so there is nothing to resume\n")
            return 1
        time.sleep(args.delay)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
