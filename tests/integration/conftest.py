"""Integration tests use a real PostgreSQL. They run only when HG_TEST_DATABASE_URL is set (spec §24).

Each run makes its own database on that server and drops it afterwards. Each test gets its own organization,
so tests share the database without seeing each other's rows.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from harnessgateway.ids import new_id
from harnessgateway.store.blobs import DirectoryBlobs
from harnessgateway.store.db import Database

SERVER = os.environ.get("HG_TEST_DATABASE_URL")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if not SERVER:
        skip = pytest.mark.skip(reason="set HG_TEST_DATABASE_URL to run tests that need PostgreSQL")
        for item in items:
            if "/integration/" in str(item.path):
                item.add_marker(skip)


async def _migrate(url: str) -> None:
    db = Database(url)
    await db.open()
    try:
        await db.migrate()
    finally:
        await db.close()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    assert SERVER
    name = f"hg_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(SERVER, autocommit=True) as server:
        server.execute(f'CREATE DATABASE "{name}"')
        url = make_conninfo(SERVER, dbname=name)
        try:
            asyncio.run(_migrate(url))
            yield url
        finally:
            server.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[Database]:
    db = Database(database_url)
    await db.open()
    yield db
    await db.close()


async def make_org(db: Database) -> str:
    org_id = new_id("org")
    async with db.system() as conn:
        await conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", [org_id, "Test"])
    return org_id


@pytest.fixture
async def org(db: Database) -> str:
    return await make_org(db)


@pytest.fixture
def blobs(tmp_path: Path) -> DirectoryBlobs:
    return DirectoryBlobs(tmp_path / "blobs")
