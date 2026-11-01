"""Recovery checks for local dependency setup."""

import importlib.util
from pathlib import Path

import httpx
import pytest


def test_interrupted_download_does_not_leave_a_completed_archive(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/postgres_local.py"
    specification = importlib.util.spec_from_file_location("postgres_local", path)
    installer = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(installer)
    monkeypatch.setattr(installer, "LOCAL", tmp_path)
    monkeypatch.setattr(installer, "INSTALL", tmp_path / "postgres")

    class InterruptedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"partial archive"
            raise httpx.ReadError("Connection interrupted")

    monkeypatch.setattr(installer.httpx, "stream", lambda *args, **kwargs: InterruptedResponse())
    for _ in range(2):
        with pytest.raises(httpx.ReadError):
            installer.install()
        assert not (tmp_path / f"postgresql-{installer.VERSION}.tar.bz2").exists()
        assert not list(tmp_path.glob("*.part"))


@pytest.fixture
def local_postgres(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/postgres_local.py"
    specification = importlib.util.spec_from_file_location("postgres_local", path)
    installer = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(installer)
    root = tmp_path / "repository"
    local = root / ".local"
    install = local / "postgres"
    (install / "bin").mkdir(parents=True)
    (install / "bin/pg_ctl").touch()
    for key, value in [
        ("ROOT", root),
        ("LOCAL", local),
        ("INSTALL", install),
        ("DATA", local / "pgdata"),
    ]:
        monkeypatch.setattr(installer, key, value)
    monkeypatch.delenv("AGENTRPG_PGDATA", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    configuration = {"DATABASE_URL": "postgresql://local:private-password@127.0.0.1:55432/board"}
    monkeypatch.setattr(installer, "dotenv_values", lambda path: configuration.copy())

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            return self

        def fetchone(self):
            return (1,)

    monkeypatch.setattr(installer.psycopg, "connect", lambda *args, **kwargs: Connection())
    calls = []

    def run(command, **kwargs):
        from types import SimpleNamespace

        calls.append(command)
        if Path(command[0]).name == "initdb":
            data = Path(command[command.index("-D") + 1])
            password = Path(next(value[9:] for value in command if value.startswith("--pwfile=")))
            assert password.read_text() == "private-password\n"
            assert password.stat().st_mode & 0o777 == 0o600
            assert data.stat().st_mode & 0o777 == 0o700
            assert "private-password" not in repr(command)
            (data / "PG_VERSION").write_text("18\n")
        return SimpleNamespace(returncode=3 if "status" in command else 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    return installer, configuration, calls


@pytest.mark.parametrize("action", ["start", "stop", "status"])
@pytest.mark.parametrize("source", ["default", "dotenv", "environment"])
def test_native_commands_select_same_data_directory(
    local_postgres, tmp_path, monkeypatch, action, source
):
    installer, configuration, calls = local_postgres
    expected = installer.DATA
    if source != "default":
        expected = tmp_path / "external storage" / "pgdata"
        configuration["AGENTRPG_PGDATA"] = str(expected)
    if source == "environment":
        expected = tmp_path / "environment storage" / "pgdata"
        monkeypatch.setenv("AGENTRPG_PGDATA", str(expected))
    if action == "status":
        with pytest.raises(SystemExit) as result:
            installer.control(action)
        assert result.value.code == 3
    else:
        installer.control(action)
    assert calls
    assert all(command[command.index("-D") + 1] == str(expected) for command in calls)
    if action == "start":
        assert (expected / "PG_VERSION").read_text() == "18\n"
        assert not list(expected.parent.glob(".agentrpg-init-*"))
    else:
        assert not expected.exists()


def test_native_relative_path_and_home_expansion(local_postgres):
    installer, _, _ = local_postgres
    assert (
        installer.data_directory({"AGENTRPG_PGDATA": "custom/data"})
        == installer.ROOT / "custom/data"
    )
    assert (
        installer.data_directory({"AGENTRPG_PGDATA": "~/external/data"})
        == Path.home() / "external/data"
    )
    assert installer.data_directory({"AGENTRPG_PGDATA": ""}) == installer.DATA


@pytest.mark.parametrize("path", ["/", ".", "  ", "bad\x00path"])
def test_native_invalid_data_paths_have_safe_errors(local_postgres, path):
    installer, configuration, calls = local_postgres
    configuration["AGENTRPG_PGDATA"] = path
    with pytest.raises(SystemExit, match="AGENTRPG_PGDATA"):
        installer.control("start")
    assert not calls


def test_native_existing_cluster_is_not_initialized_or_modified(local_postgres):
    installer, _, calls = local_postgres
    installer.DATA.mkdir(mode=0o700)
    (installer.DATA / "PG_VERSION").write_text("18\n")
    sentinel = installer.DATA / "existing-record"
    sentinel.write_bytes(b"preserve database data")
    installer.control("start")
    assert not any(Path(command[0]).name == "initdb" for command in calls)
    assert sentinel.read_bytes() == b"preserve database data"


def test_native_nonempty_uninitialized_directory_is_preserved(local_postgres):
    installer, _, calls = local_postgres
    installer.DATA.mkdir(mode=0o700)
    sentinel = installer.DATA / "unrelated-file"
    sentinel.write_text("retain")
    with pytest.raises(SystemExit, match="not empty"):
        installer.control("start")
    assert sentinel.read_text() == "retain"
    assert not calls


def test_native_empty_directory_and_new_parents_are_private(local_postgres, tmp_path):
    installer, _, _ = local_postgres
    nested = tmp_path / "private-parent" / "private-child" / "pgdata"
    assert installer.prepare_data_directory(nested)
    assert all(
        path.stat().st_mode & 0o777 == 0o700
        for path in (nested, nested.parent, nested.parent.parent)
    )
    nested.chmod(0o755)
    assert installer.prepare_data_directory(nested)
    assert nested.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("is_link", [False, True])
def test_native_file_and_symlink_paths_are_rejected(local_postgres, tmp_path, is_link):
    installer, _, calls = local_postgres
    if is_link:
        target = tmp_path / "other-data"
        target.mkdir()
        installer.DATA.symlink_to(target, target_is_directory=True)
    else:
        installer.DATA.write_text("unrelated")
    with pytest.raises(SystemExit, match="directory, not a link"):
        installer.control("start")
    assert not calls


def test_native_init_failure_removes_only_temporary_password(local_postgres, monkeypatch):
    installer, _, calls = local_postgres
    original = installer.subprocess.run

    def fail_init(command, **kwargs):
        if Path(command[0]).name == "initdb":
            (installer.DATA / "partial-data").write_text("retain for inspection")
            raise installer.subprocess.CalledProcessError(1, ["private-password"])
        return original(command, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", fail_init)
    with pytest.raises(SystemExit) as result:
        installer.control("start")
    assert "private-password" not in str(result.value)
    assert (installer.DATA / "partial-data").read_text() == "retain for inspection"
    assert not list(installer.DATA.parent.glob(".agentrpg-init-*"))
    assert not calls


def test_native_connection_failure_has_no_raw_configuration(local_postgres, monkeypatch):
    installer, _, _ = local_postgres

    def fail_connection(*args, **kwargs):
        raise installer.psycopg.OperationalError("private-password in connection failure")

    monkeypatch.setattr(installer.psycopg, "connect", fail_connection)
    with pytest.raises(SystemExit) as result:
        installer.control("start")
    assert "private-password" not in str(result.value)
    assert (installer.DATA / "PG_VERSION").is_file()
