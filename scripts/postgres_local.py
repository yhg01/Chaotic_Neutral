"""Install and manage a project-local PostgreSQL server without Docker."""

import argparse
import hashlib
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import httpx
import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
INSTALL = LOCAL / "postgres"
DATA = LOCAL / "pgdata"
VERSION = "18.6"
SHA256 = "555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f"


def install() -> None:
    if (INSTALL / "bin/pg_ctl").exists():
        print(f"PostgreSQL is already installed in {INSTALL}")
        return
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    archive = LOCAL / f"postgresql-{VERSION}.tar.bz2"
    url = f"https://ftp.postgresql.org/pub/source/v{VERSION}/{archive.name}"
    if archive.exists():
        with archive.open("rb") as downloaded:
            if hashlib.file_digest(downloaded, "sha256").hexdigest() != SHA256:
                archive.unlink()
    if not archive.exists():
        print(f"Downloading PostgreSQL {VERSION} from postgresql.org", flush=True)
        partial = archive.with_suffix(".part")
        try:
            with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
                response.raise_for_status()
                with partial.open("wb") as target:
                    for block in response.iter_bytes():
                        target.write(block)
            with partial.open("rb") as downloaded:
                checksum = hashlib.file_digest(downloaded, "sha256").hexdigest()
            if checksum != SHA256:
                raise SystemExit(
                    "PostgreSQL archive checksum does not match. Installation stopped."
                )
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    source = LOCAL / f"postgresql-{VERSION}"
    if not source.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(LOCAL, filter="data")
    log = LOCAL / "postgres-build.log"
    print(f"Building PostgreSQL. Build log: {log}", flush=True)
    commands = [
        [
            "./configure",
            f"--prefix={INSTALL}",
            "--without-icu",
            "--without-readline",
            "--without-zlib",
        ],
        ["make", f"-j{min(os.cpu_count() or 2, 8)}"],
        ["make", "install"],
    ]
    with log.open("w") as output:
        for command in commands:
            result = subprocess.run(command, cwd=source, stdout=output, stderr=output, check=False)
            if result.returncode:
                raise SystemExit(f"Build failed. Read {log} for details.")
    shutil.rmtree(source)
    archive.unlink()
    print(f"Installed PostgreSQL {VERSION} in {INSTALL}")


def data_directory(env: dict) -> Path:
    """Use the same configured path for every server control action."""
    value = env.get("AGENTRPG_PGDATA")
    if value is None or value == "":
        return DATA
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise SystemExit("AGENTRPG_PGDATA must be a valid directory path.")
    try:
        selected = Path(value).expanduser()
        if not selected.is_absolute():
            selected = ROOT / selected
        # Keep the final path intact so preparation can reject a symbolic link.
        selected = Path(os.path.abspath(selected))
    except (OSError, RuntimeError, ValueError):
        raise SystemExit("AGENTRPG_PGDATA must be a valid directory path.") from None
    if selected == Path(selected.anchor) or selected == ROOT:
        raise SystemExit("AGENTRPG_PGDATA must select a dedicated database directory.")
    return selected


def prepare_data_directory(data: Path) -> bool:
    """Create private directories; return whether initdb is required."""
    if data.is_symlink() or (data.exists() and not data.is_dir()):
        raise SystemExit("The selected PostgreSQL data path must be a directory, not a link.")
    if data.exists():
        if (data / "PG_VERSION").is_file():
            return False
        if any(data.iterdir()):
            raise SystemExit(
                "The selected data directory is not an initialized PostgreSQL cluster "
                "and is not empty. Inspect it before retrying."
            )
        if data.stat().st_uid != os.geteuid():
            raise SystemExit("The selected empty data directory must belong to the current user.")
        data.chmod(0o700)
        return True
    missing = []
    current = data
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    return True


def control(action: str) -> None:
    try:
        _control(action)
    except (OSError, ValueError, psycopg.Error, subprocess.CalledProcessError):
        # Connection parsing and provider errors can contain private configuration.
        raise SystemExit(
            "PostgreSQL control failed. Check the selected data path, permissions, "
            "local configuration, and PostgreSQL log. Existing data was not moved or deleted."
        ) from None


def _control(action: str) -> None:
    if action not in {"start", "stop", "status"}:
        raise SystemExit("Choose start, stop, or status for PostgreSQL control.")
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    data = data_directory(env)
    executable = INSTALL / "bin/pg_ctl"
    if not executable.exists():
        raise SystemExit("Run scripts/postgres_local.py install first.")
    command = [str(executable), "-D", str(data)]
    if action == "status":
        result = subprocess.run([*command, "status"], check=False)
        raise SystemExit(result.returncode)
    if action == "stop":
        subprocess.run([*command, "stop", "-m", "fast", "-w"], check=True)
        return
    parameters = conninfo_to_dict(env.get("DATABASE_URL", ""))
    required = ("user", "password", "dbname", "host", "port")
    if not all(parameters.get(key) for key in required):
        raise SystemExit("DATABASE_URL must include user, password, database, host, and port.")
    if parameters["host"] != "127.0.0.1":
        raise SystemExit("The project-local server requires DATABASE_URL host 127.0.0.1.")
    port = int(parameters["port"])
    if not 1 <= port <= 65535:
        raise SystemExit("DATABASE_URL must select a port from 1 through 65535.")
    if prepare_data_directory(data):
        fd, temporary = tempfile.mkstemp(prefix=".agentrpg-init-", dir=data.parent)
        password_file = Path(temporary)
        try:
            with os.fdopen(fd, "w") as output:
                output.write(parameters["password"] + "\n")
            subprocess.run(
                [
                    str(INSTALL / "bin/initdb"),
                    "-D",
                    str(data),
                    "-U",
                    parameters["user"],
                    "--auth-local=scram-sha-256",
                    "--auth-host=scram-sha-256",
                    f"--pwfile={password_file}",
                    "--encoding=UTF8",
                    "--locale=C",
                ],
                check=True,
            )
        finally:
            password_file.unlink(missing_ok=True)
    running = subprocess.run([*command, "status"], capture_output=True, check=False)
    if running.returncode != 0:
        options = shlex.join(["-h", "127.0.0.1", "-p", str(port), "-k", ""])
        subprocess.run(
            [*command, "start", "-l", str(LOCAL / "postgres.log"), "-o", options, "-w"],
            check=True,
        )
    database = parameters["dbname"]
    maintenance = make_conninfo(**{**parameters, "dbname": "postgres"})
    with psycopg.connect(maintenance, autocommit=True) as connection:
        if not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
        ).fetchone():
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    print(f"PostgreSQL is ready on 127.0.0.1:{port}. Data: {data}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "start", "stop", "status"])
    args = parser.parse_args()
    if args.action == "install":
        install()
    else:
        control(args.action)


if __name__ == "__main__":
    main()
