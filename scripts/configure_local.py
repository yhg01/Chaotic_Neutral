"""Create local configuration without replacing existing credentials."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = ROOT / ".env"
    if target.exists():
        print("Existing .env preserved.")
        return
    password = secrets.token_urlsafe(32)
    token = secrets.token_urlsafe(40)
    content = (
        f"DATABASE_URL=postgresql://agentrpg:{password}@127.0.0.1:55432/agentrpg\n"
        "POSTGRES_USER=agentrpg\n"
        f"POSTGRES_PASSWORD={password}\n"
        "POSTGRES_DB=agentrpg\n"
        "POSTGRES_PORT=55432\n"
        f"ADMIN_TOKEN={token}\n"
        "HOST=127.0.0.1\n"
        "PORT=8000\n"
    )
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    print("Created .env with random local credentials (owner access only).")
    print("Start PostgreSQL with: docker compose up -d --wait db")


if __name__ == "__main__":
    main()
