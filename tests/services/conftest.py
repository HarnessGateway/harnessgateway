"""The services on a real Postgres (a scratch database from tests/conftest.py), with the file object store in a temporary
directory and no node: what the platform guarantees, without a deploy. The database is migrated once; each test opens
its own Platform on it and works in organizations of its own."""
from __future__ import annotations

import asyncio
import io
import secrets
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio

from harnessgateway.config import Settings
from harnessgateway.domain.access import ADMIN, DEVELOPER, Principal
from harnessgateway.services.platform import Platform

SHELL = Path(__file__).resolve().parents[2] / "packages" / "shell"


def token_of(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


def pack(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


@pytest.fixture(scope="session")
def database_url(scratch_database) -> str:
    return scratch_database()


@pytest.fixture(scope="session")
def settings(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    blobs = tmp_path_factory.mktemp("blobs")
    s = Settings.load(("api",), env={"HG_DATABASE_URL": database_url, "HG_BLOB_URL": f"file://{blobs}",
                                     "HG_SECRET_KEY": "dGVzdC1zZWNyZXQta2V5LW9mLXRoaXJ0eS10d28tYnl0ZXM=",
                                     "HG_OPERATOR_KEY": "hgo_test", "HG_CONSOLE_URL": "http://console.test:8800"})

    async def migrate() -> None:
        plat = Platform(s)
        await plat.db.open()
        try:
            await plat.migrate()
        finally:
            await plat.db.close()
    asyncio.run(migrate())
    return s


@pytest_asyncio.fixture
async def plat(settings: Settings) -> AsyncIterator[Platform]:
    plat = Platform(settings)
    await plat.open()
    try:
        yield plat
    finally:
        await plat.close()


class Org:
    """An organization made for one test: its id and three principals (an admin key, a developer key, a run-only key).
    Tests about people create accounts of their own."""

    def __init__(self, org_id: str):
        self.id = org_id
        self.admin = Principal(org_id, "key:admin", ADMIN)
        self.developer = Principal(org_id, "key:dev", DEVELOPER)
        self.app = Principal(org_id, "key:app", DEVELOPER, run_only=True)


@pytest_asyncio.fixture
async def org(plat: Platform) -> Org:
    created = await plat.orgs.create(f"org-{secrets.token_hex(4)}")
    return Org(str(created["id"]))


@pytest_asyncio.fixture
async def shell(plat: Platform, org: Org) -> dict:
    """The shell package installed in the test's organization."""
    return await plat.harnesses.install(org.admin, pack(SHELL))
