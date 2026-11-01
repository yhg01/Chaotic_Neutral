"""Exercise a running board with 15 Agent clients over real HTTP."""

import argparse
import asyncio
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(values),
        "p50_ms": round(ordered[max(0, math.ceil(len(values) * 0.50) - 1)] * 1000, 2),
        "p95_ms": round(ordered[max(0, math.ceil(len(values) * 0.95) - 1)] * 1000, 2),
        "max_ms": round(ordered[-1] * 1000, 2),
    }


async def exercise(base_url: str, admin_token: str, rounds: int) -> dict:
    writes: list[float] = []
    reads: list[float] = []
    accepted: list[str] = []
    errors: list[str] = []
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/api/v1",
        timeout=20,
        limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
    ) as client:
        admin = {"Authorization": f"Bearer {admin_token}"}
        cluster = await client.post(
            "/clusters", headers=admin, json={"name": f"HTTP verification {uuid4().hex[:8]}"}
        )
        cluster.raise_for_status()
        cid = cluster.json()["id"]
        registrations = await asyncio.gather(
            *[
                client.post(
                    f"/clusters/{cid}/agents", headers=admin, json={"name": f"Agent {i + 1}"}
                )
                for i in range(15)
            ]
        )
        for response in registrations:
            response.raise_for_status()
        credentials = [response.json()["token"] for response in registrations]
        headers = [{"Authorization": f"Bearer {token}"} for token in credentials]
        root = await client.post(
            f"/clusters/{cid}/threads",
            headers=headers[0],
            json={
                "title": "Concurrent HTTP test",
                "body": "Shared test thread.",
                "request_id": str(uuid4()),
            },
        )
        root.raise_for_status()
        thread_id = root.json()["id"]
        thread_path = f"/clusters/{cid}/threads/{thread_id}"
        gate = asyncio.Event()

        async def write(index: int, iteration: int) -> None:
            started = time.perf_counter()
            response = await client.post(
                thread_path + "/replies",
                headers=headers[index],
                json={
                    "body": f"Agent {index + 1}, message {iteration + 1}",
                    "request_id": str(uuid4()),
                },
            )
            writes.append(time.perf_counter() - started)
            response.raise_for_status()
            accepted.append(response.json()["id"])

        async def read(index: int) -> None:
            started = time.perf_counter()
            response = await client.get(thread_path, headers=headers[index], params={"limit": 100})
            reads.append(time.perf_counter() - started)
            response.raise_for_status()

        async def agent(index: int) -> None:
            await gate.wait()
            for iteration in range(rounds):
                results = await asyncio.gather(
                    write(index, iteration), read(index), return_exceptions=True
                )
                errors.extend(
                    type(result).__name__ for result in results if isinstance(result, Exception)
                )

        tasks = [asyncio.create_task(agent(index)) for index in range(15)]
        started = time.perf_counter()
        gate.set()
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started
        history: set[str] = set()
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = await client.get(thread_path, headers=admin, params=params)
            response.raise_for_status()
            payload = response.json()
            history.update(item["id"] for item in payload["replies"])
            cursor = payload["next_cursor"]
            if not cursor:
                break

    expected = 15 * rounds
    passed = (
        not errors
        and len(accepted) == expected
        and len(set(accepted)) == expected
        and set(accepted) == history
    )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "transport": "HTTP over loopback",
        "agents": 15,
        "rounds_per_agent": rounds,
        "expected_replies": expected,
        "accepted_replies": len(accepted),
        "stored_replies": len(history),
        "missing_replies": len(set(accepted) - history),
        "duplicate_response_ids": len(accepted) - len(set(accepted)),
        "errors": errors,
        "elapsed_seconds": round(elapsed, 3),
        "writes": percentiles(writes),
        "reads": percentiles(reads),
        "passed": passed,
        "scope": "One local cluster and thread; excludes model inference and remote networks.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 1000:
        raise SystemExit("Rounds must be between 1 and 1000.")
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    token = env.get("ADMIN_TOKEN")
    if not token:
        raise SystemExit("Set ADMIN_TOKEN first.")
    result = asyncio.run(exercise(args.base_url, token, args.rounds))
    report = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(report)
    print(report, end="")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
