"""Real PostgreSQL fixtures, isolated from local development records."""

import os
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agentrpg.config import Settings
from agentrpg.main import create_app
from agentrpg.migrate import migrate

ADMIN_TOKEN = "test-admin-token-32-characters-minimum"


@pytest_asyncio.fixture
async def test_database_url():
    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("TEST_DATABASE_URL must identify a separate PostgreSQL test database.")
    database_name = conninfo_to_dict(configured).get("dbname", "")
    if "test" not in database_name.lower():
        pytest.fail("TEST_DATABASE_URL must name a database containing 'test'.")
    schema = "test_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(configured, autocommit=True) as control:
        await control.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        # A test owns only this generated schema. Never reset the whole database.
        connection_url = make_conninfo(configured, options=f"-c search_path={schema}")
        try:
            await migrate(connection_url)
            yield connection_url
        finally:
            await control.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def settings(test_database_url):
    return Settings(
        _env_file=None,
        database_url=test_database_url,
        admin_token=ADMIN_TOKEN,
    )


@pytest_asyncio.fixture
async def app(settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as http:
        yield http


@pytest.fixture
def db_pool(app):
    return app.state.pool


@pytest.fixture
def pool(db_pool):
    return db_pool


@pytest.fixture
def provision(client):
    async def create(name="Cluster", count=2):
        cluster_response = await client.post("/api/v1/clusters", json={"name": name})
        assert cluster_response.status_code == 201, cluster_response.text
        cluster = cluster_response.json()
        agents = []
        for index in range(count):
            response = await client.post(
                f"/api/v1/clusters/{cluster['id']}/agents",
                json={"name": f"Agent {index + 1}"},
            )
            assert response.status_code == 201, response.text
            agents.append(response.json())
        return cluster, agents

    return create
