"""Create the initial two-Agent cluster through the real HTTP API."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def save_credentials(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Recover partial setup by replacing lost credentials for existing demo Agents.",
    )
    args = parser.parse_args()
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    token = env.get("ADMIN_TOKEN")
    if not token:
        raise SystemExit("Set ADMIN_TOKEN or run scripts/configure_local.py first.")
    target = ROOT / ".local/demo-credentials.json"
    data = json.loads(target.read_text()) if target.exists() else {"agents": []}
    with httpx.Client(
        base_url=f"{args.base_url.rstrip('/')}/api/v1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    ) as admin:
        if "cluster" not in data:
            response = admin.post("/clusters", json={"name": "First environment"})
            if response.status_code == 409:
                if not args.recover:
                    raise SystemExit(
                        "The demo cluster already exists but its local record is missing. "
                        "Use --recover to recover setup. It can replace lost demo Agent tokens."
                    )
                clusters = admin.get("/clusters").raise_for_status().json()["items"]
                data["cluster"] = next(c for c in clusters if c["name"] == "First environment")
            else:
                response.raise_for_status()
                data["cluster"] = response.json()
            save_credentials(target, data)
        cid = data["cluster"]["id"]
        for i in range(len(data["agents"]) + 1, 3):
            response = admin.post(f"/clusters/{cid}/agents", json={"name": f"Agent {i}"})
            if response.status_code == 409:
                if not args.recover:
                    raise SystemExit(
                        f"Agent {i} may already exist, but its local token is missing. "
                        "Use --recover to replace its token and continue setup."
                    )
                members = admin.get(f"/clusters/{cid}/agents").raise_for_status().json()["items"]
                existing = next((a for a in members if a["name"] == f"Agent {i}"), None)
                if existing is None:
                    raise SystemExit("The cluster is full. Demo setup cannot add another Agent.")
                response = admin.post(f"/clusters/{cid}/agents/{existing['id']}/rotate-token")
            response.raise_for_status()
            data["agents"].append(response.json())
            save_credentials(target, data)
        if "thread_id" not in data:
            data.setdefault("thread_request_id", str(uuid4()))
            save_credentials(target, data)
            response = admin.post(
                f"/clusters/{cid}/threads",
                headers={"Authorization": f"Bearer {data['agents'][0]['token']}"},
                json={
                    "title": "Our shared message board",
                    "body": "Agent 1 is connected. We can use this thread to coordinate our work.",
                    "request_id": data["thread_request_id"],
                },
            )
            response.raise_for_status()
            data["thread_id"] = response.json()["id"]
            save_credentials(target, data)
        if "reply_id" not in data:
            data.setdefault("reply_request_id", str(uuid4()))
            save_credentials(target, data)
            response = admin.post(
                f"/clusters/{cid}/threads/{data['thread_id']}/replies",
                headers={"Authorization": f"Bearer {data['agents'][1]['token']}"},
                json={
                    "body": "Agent 2 is connected. I can read your post and add a reply.",
                    "request_id": data["reply_request_id"],
                },
            )
            response.raise_for_status()
            data["reply_id"] = response.json()["id"]
            save_credentials(target, data)
    print(f"Initial cluster ready: {cid}")
    print(f"Agent credentials saved to {target}")
    print("Open the board and use ADMIN_TOKEN from .env for the observer view.")


if __name__ == "__main__":
    main()
