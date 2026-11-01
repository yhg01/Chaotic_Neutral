"""Local operator access uses the connection and browser origin, not Agent identity."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import ADMIN_TOKEN
from test_console_execution import create_run

from agentrpg import __main__ as server
from agentrpg.director import Director, row


def local_client(app, *, peer="127.0.0.1", base_url="http://127.0.0.1:8000", headers=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 12345)),
        base_url=base_url,
        headers=headers,
    )


async def test_local_operator_reads_and_writes_without_token(app, pool, tmp_path):
    app.state.console_root = tmp_path
    manifest = await create_run(pool, tmp_path)
    run_id = manifest["run_id"]
    prompt = "Please explain the available options before you act."
    async with local_client(app) as client:
        for path in (
            "/api/v1/observer/runs",
            f"/api/v1/observer/runs/{run_id}",
            f"/api/v1/observer/runs/{run_id}/turns",
            f"/api/v1/console/runs/{run_id}/execution",
            f"/api/v1/console/runs/{run_id}/boundaries",
        ):
            response = await client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert ADMIN_TOKEN not in response.text
            assert all(p["token"] not in response.text for p in manifest["participants"])
        intervention = await client.post(
            f"/api/v1/console/runs/{run_id}/interventions",
            headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
            json={
                "request_id": str(uuid4()),
                "owner_id": "owner-1",
                "target": "agent",
                "prompt": prompt,
                "expected_window": 0,
            },
        )
        assert intervention.status_code == 201, intervention.text
        assert intervention.json()["status"] == "pending"
        branch = await client.post(
            f"/api/v1/console/runs/{run_id}/branches",
            headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
            json={"request_id": str(uuid4()), "window": 0},
        )
        assert branch.status_code == 200, branch.text
        child_id = UUID(branch.json()["run_id"])
        child = await Director(pool).get_run(child_id)
        assert child["status"] == "paused"
        assert child_id != UUID(run_id)
    async with pool.connection() as conn:
        saved = await row(
            conn,
            "SELECT owner_id,target,prompt FROM console_interventions WHERE run_id=%s",
            (UUID(run_id),),
        )
        assert saved == {"owner_id": "owner-1", "target": "agent", "prompt": prompt}
        assert (await row(conn, "SELECT count(*) AS n FROM model_spend_attempts"))["n"] == 0


@pytest.mark.parametrize(
    ("peer", "base_url", "bind_host", "headers"),
    [
        ("127.0.0.1", "http://127.0.0.1:8000", "127.0.0.1", {}),
        ("127.0.0.2", "http://127.0.0.2:8000", "127.0.0.1", {}),
        ("127.0.0.1", "http://localhost:8000", "localhost", {}),
        ("::1", "http://[::1]:8000", "::1", {"Origin": "http://[::1]:8000"}),
        (
            "127.0.0.1",
            "http://localhost:8000",
            "127.0.0.1",
            {"Origin": "http://localhost:8000", "Sec-Fetch-Site": "same-origin"},
        ),
        ("127.0.0.1", "http://localhost:8000", "127.0.0.1", {"Sec-Fetch-Site": "none"}),
    ],
)
async def test_loopback_forms_are_accepted(app, peer, base_url, bind_host, headers):
    app.state.settings.host = bind_host
    async with local_client(app, peer=peer, base_url=base_url, headers=headers) as client:
        response = await client.get("/api/v1/observer/runs")
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("peer", "base_url", "bind_host", "headers"),
    [
        ("203.0.113.10", "http://127.0.0.1:8000", "127.0.0.1", {}),
        ("127.0.0.1", "http://hostile.example:8000", "127.0.0.1", {}),
        ("127.0.0.1", "http://localhost.hostile.example:8000", "127.0.0.1", {}),
        ("127.0.0.1", "http://127.0.0.1:8000", "0.0.0.0", {}),
        ("::1", "http://[::1]:8000", "::", {}),
        (
            "127.0.0.1",
            "http://127.0.0.1:8000",
            "127.0.0.1",
            {"Origin": "https://hostile.example"},
        ),
        ("127.0.0.1", "http://127.0.0.1:8000", "127.0.0.1", {"Origin": "null"}),
        (
            "127.0.0.1",
            "http://127.0.0.1:8000",
            "127.0.0.1",
            {"Origin": "http://127.0.0.1:8001"},
        ),
        (
            "127.0.0.1",
            "http://127.0.0.1:8000",
            "127.0.0.1",
            {"Origin": "https://127.0.0.1:8000"},
        ),
        (
            "127.0.0.1",
            "http://127.0.0.1:8000",
            "127.0.0.1",
            {"Origin": "http://localhost:8000"},
        ),
        (
            "127.0.0.1",
            "http://127.0.0.1:8000",
            "127.0.0.1",
            {"Sec-Fetch-Site": "cross-site"},
        ),
        (
            "127.0.0.1",
            "http://127.0.0.1:8000",
            "127.0.0.1",
            {"Sec-Fetch-Site": "same-site"},
        ),
        (
            "203.0.113.10",
            "http://127.0.0.1:8000",
            "127.0.0.1",
            {
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Host": "127.0.0.1:8000",
                "Forwarded": "for=127.0.0.1;host=127.0.0.1:8000;proto=http",
                "Origin": "http://127.0.0.1:8000",
                "Sec-Fetch-Site": "same-origin",
            },
        ),
    ],
)
async def test_untrusted_requests_cannot_read_or_insert_prompts(
    app, pool, tmp_path, peer, base_url, bind_host, headers
):
    app.state.console_root = tmp_path
    app.state.settings.host = bind_host
    manifest = await create_run(pool, tmp_path)
    run_id = manifest["run_id"]
    async with local_client(app, peer=peer, base_url=base_url, headers=headers) as client:
        read = await client.get(f"/api/v1/observer/runs/{run_id}")
        assert read.status_code == 401, read.text
        write = await client.post(
            f"/api/v1/console/runs/{run_id}/interventions",
            json={
                "request_id": str(uuid4()),
                "owner_id": "owner-1",
                "target": "agent",
                "prompt": "Unauthorized prompt.",
                "expected_window": 0,
            },
        )
        assert write.status_code == 401, write.text
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM console_interventions"))["n"] == 0


async def test_explicit_credentials_keep_their_role_and_invalid_tokens_fail(app, pool, tmp_path):
    app.state.console_root = tmp_path
    manifest = await create_run(pool, tmp_path)
    path = f"/api/v1/console/runs/{manifest['run_id']}/execution"
    async with local_client(app) as client:
        for authorization, expected in (
            (f"Bearer {manifest['participants'][0]['token']}", 403),
            ("Bearer invalid-token", 401),
            ("Basic invalid-token", 401),
            ("", 401),
        ):
            response = await client.get(path, headers={"Authorization": authorization})
            assert response.status_code == expected, response.text
    async with local_client(
        app, peer="203.0.113.10", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text


async def test_local_operator_is_not_an_agent_or_general_administrator(app, pool, tmp_path):
    manifest = await create_run(pool, tmp_path)
    cluster_id = manifest["cluster_id"]
    async with local_client(app) as client:
        for method, path, body in (
            ("GET", "/api/v1/me", None),
            ("GET", "/api/v1/clusters", None),
            ("POST", "/api/v1/clusters", {"name": "Unauthorized cluster"}),
            ("GET", f"/api/v1/runs/{manifest['run_id']}", None),
            (
                "POST",
                f"/api/v1/clusters/{cluster_id}/threads",
                {"title": "Local operator", "body": "Cannot post.", "request_id": str(uuid4())},
            ),
        ):
            response = await client.request(method, path, json=body)
            assert response.status_code == 401, (path, response.text)
    async with pool.connection() as conn:
        assert (await row(conn, "SELECT count(*) AS n FROM messages"))["n"] == 0


def test_server_does_not_trust_forwarded_peer_addresses(monkeypatch):
    settings = SimpleNamespace(host="127.0.0.1", port=8000)
    application = object()
    launched = []
    monkeypatch.setattr(server, "Settings", lambda: settings)
    monkeypatch.setattr(server, "create_app", lambda configured: application)
    monkeypatch.setattr(
        server.uvicorn, "run", lambda *args, **kwargs: launched.append((args, kwargs))
    )
    server.main()
    assert launched == [
        ((application,), {"host": "127.0.0.1", "port": 8000, "proxy_headers": False})
    ]
