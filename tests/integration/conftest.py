"""A platform to test: a real database (HG_TEST_DATABASE_URL names one that may be emptied; without it these tests
are skipped, never passed), the real stores and APIs, and the fake driver in place of Docker."""

import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from harnessgateway.domain.principal import Principal
from harnessgateway.ids import DEFAULT_ORG
from harnessgateway.platform import Platform
from harnessgateway.settings import Settings
from harnessgateway.store.db import Database
from harnessgateway.store.turns import Turn
from tests.helpers import FakeDriver, package

ORG = DEFAULT_ORG
WHO = Principal(ORG, "key_tests")
LINE = "default/echo/none/none"


def settings_for(url: str, data: Path, **more: str) -> Settings:
    return Settings.from_env({"HG_DATABASE_URL": url, "HG_SECRET_KEY": base64.b64encode(bytes(range(32))).decode(),
                              "HG_PROXY_KEY": "proxy-key", "HG_DATA": str(data), **more})


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    url = os.environ.get("HG_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HG_TEST_DATABASE_URL is not set: these tests need a database they may empty")
    database = Database(url)
    await database.open()
    async with database.tx() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def platform(db: Database, tmp_path: Path) -> Platform:
    await db.migrate()
    made = Platform.of(settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path), db,
                       driver=FakeDriver(tmp_path / "folders"))
    await made.orgs.ensure(ORG, "Tests")
    return made


async def ready(platform: Platform, **environment) -> None:
    """The smallest setup a turn can run on: the echo harness, in an environment that carries it."""
    await platform.resources.add_package(ORG, "echo", package())
    await platform.resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["echo"], **environment})


async def run(platform: Platform, **request) -> Turn:
    """Send a request and run its turn to the end."""
    from harnessgateway.api.responses import accept
    from harnessgateway.runner.turn import TurnRunner
    await accept(platform, WHO, {"model": LINE, "input": "hi", **request})
    claimed = await platform.turns.claim("runner-tests")
    assert claimed is not None
    return await TurnRunner(platform).run(claimed)


async def work_files(platform: Platform, turn: Turn) -> dict[str, bytes]:
    manifest = await platform.snapshots.manifest(ORG, turn.snapshot_id)
    return {path: b"".join([piece async for piece in platform.snapshots.content(ORG, entry)])
            for path, entry in manifest.files("work/").items()}
