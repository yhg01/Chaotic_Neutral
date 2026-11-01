"""Run integration tests in a separate PostgreSQL test database."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Configure DATABASE_URL first.")
    parameters = conninfo_to_dict(database_url)
    test_url = env.get("TEST_DATABASE_URL")
    if not test_url:
        test_database = f"{parameters['dbname']}_test"
        with psycopg.connect(database_url, autocommit=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (test_database,)
            ).fetchone()
            if not exists:
                connection.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_database))
                )
        parameters["dbname"] = test_database
        test_url = make_conninfo(**parameters)
    if conninfo_to_dict(test_url).get("dbname") == conninfo_to_dict(database_url).get("dbname"):
        raise SystemExit("TEST_DATABASE_URL must refer to a separate test database.")
    test_env = {key: str(value) for key, value in env.items() if value is not None}
    test_env["TEST_DATABASE_URL"] = test_url
    # Cloud-synced checkouts can stall reads of generated .pyc files. Keep test
    # caches in local application storage and preserve any explicit override.
    if "PYTHONPYCACHEPREFIX" not in test_env:
        cache_root = (
            Path.home() / "Library/Application Support"
            if sys.platform == "darwin"
            else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        )
        cache_path = cache_root / "agentRPG/pycache-tests"
        cache_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        test_env["PYTHONPYCACHEPREFIX"] = str(cache_path)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *sys.argv[1:]],
        cwd=ROOT,
        env=test_env,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
