"""Apply checked, versioned SQL migrations without deleting data."""

import asyncio
import hashlib
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from agentrpg.config import Settings

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def load_migrations(directory: Path) -> list[tuple[str, str]]:
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("[0-9]*_*.sql"))
    ]


async def migrate(database_url: str, directory: Path = MIGRATIONS) -> None:
    files = await asyncio.to_thread(load_migrations, directory)
    if not files:
        raise RuntimeError("No database migrations were found.")
    async with await psycopg.AsyncConnection.connect(
        database_url, row_factory=dict_row
    ) as connection:
        # This lock exists only during schema changes, never during board writes.
        await connection.execute("SELECT pg_advisory_xact_lock(679132104257)")
        await connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )"""
        )
        cursor = await connection.execute("SELECT version, checksum FROM schema_migrations")
        applied = {row["version"]: row["checksum"] for row in await cursor.fetchall()}
        available = {name for name, source in files}
        if applied.keys() - available:
            raise RuntimeError("The database has migrations absent from this application.")
        for name, source in files:
            checksum = hashlib.sha256(source.encode()).hexdigest()
            if name in applied:
                if applied[name] != checksum:
                    raise RuntimeError(f"Applied migration was changed: {name}")
                continue
            await connection.execute(source, prepare=False)
            await connection.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                (name, checksum),
            )


def main() -> None:
    settings = Settings()
    try:
        asyncio.run(migrate(settings.database_url.get_secret_value()))
    except psycopg.Error:
        raise SystemExit(
            "Database migration failed. Check the local database configuration."
        ) from None
    print("Database migrations are current.")


if __name__ == "__main__":
    main()
