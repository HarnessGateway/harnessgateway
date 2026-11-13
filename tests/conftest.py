"""Fixtures shared by every level of test, and the rule for which levels run (spec §24).

Unit tests always run. Integration tests need a PostgreSQL server (HG_TEST_DATABASE_URL). End-to-end tests
need Docker (HG_TEST_DOCKER=1), and most need the server too. A level that is not enabled is skipped, never
passed.

Each run makes its own database on the server and drops it afterwards. Each test gets its own organization,
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

from harnessgateway.extensions import Extensions
from harnessgateway.ids import new_id
from harnessgateway.settings import Settings
from harnessgateway.store.blobs import DirectoryBlobs
from harnessgateway.store.db import MIGRATIONS, Database
from harnessgateway.stores import Stores

SERVER = os.environ.get("HG_TEST_DATABASE_URL")
DOCKER = os.environ.get("HG_TEST_DOCKER") == "1"


def loaded_extensions() -> Extensions:
    """The extensions HG_TEST_EXTENSIONS names (`module:Class`, comma-separated); none by default. Another
    edition runs this whole suite with itself loaded: every test must pass as it does without (spec §19)."""
    names = [name.strip() for name in os.environ.get("HG_TEST_EXTENSIONS", "").split(",") if name.strip()]
    return Extensions.load(names)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    no_server = pytest.mark.skip(reason="set HG_TEST_DATABASE_URL to run tests that need PostgreSQL")
    no_docker = pytest.mark.skip(reason="set HG_TEST_DOCKER=1 to run tests that need Docker")
    for item in items:
        path = str(item.path)
        if "/e2e/" in path and not DOCKER:
            item.add_marker(no_docker)
        elif not SERVER and ("/integration/" in path or "database_url" in getattr(item, "fixturenames", ())):
            item.add_marker(no_server)


async def _migrate(url: str) -> None:
    db = Database(url)
    await db.open()
    try:
        await db.migrate([MIGRATIONS, *loaded_extensions().migrations()])
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


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> Settings:
    return Settings.from_env({
        "HG_DATABASE_URL": database_url, "HG_SECRET_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "HG_PROXY_KEY": "test-proxy-key", "HG_DATA": str(tmp_path / "blobs"), "HG_SANDBOX_IMAGE": "sandbox:test",
        "HG_PLATFORM_IMAGE": "platform:test", "HG_INTERNAL_URL": "http://internal.test",
    })


@pytest.fixture
async def stores(settings: Settings, db: Database, blobs: DirectoryBlobs, org: str) -> Stores:
    """The stores, with the organization's default environment in place."""
    stores = Stores.of(settings, db, blobs)
    await stores.environments.ensure_default(org)
    return stores
