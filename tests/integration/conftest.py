"""A platform to test: a real database (HG_TEST_DATABASE_URL names one that may be emptied; without it these tests
are skipped, never passed), the real stores and APIs, and the stand-in driver in place of Docker.

The extensions HG_TEST_EXTENSIONS names are loaded, as an installation loads them: it is how an edition runs these
tests on itself, to show that it changes nothing the core does."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from harnessgateway.api.app import public_app
from harnessgateway.domain.principal import Principal
from harnessgateway.ids import DEFAULT_ORG
from harnessgateway.platform import Platform
from harnessgateway.store.db import Database
from harnessgateway.store.turns import Turn
from harnessgateway.testing import emptied, extensions_under_test, package, platform_on, run_next, settings_for

ORG = DEFAULT_ORG
SAMPLE = "tests.integration.edition:Edition"
WHO = Principal(ORG, "key_tests")
BY = WHO.subject
LINE = "default/echo/none/none"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    url = os.environ.get("HG_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HG_TEST_DATABASE_URL is not set: these tests need a database they may empty")
    database = await emptied(url)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def platform(db: Database, tmp_path: Path) -> Platform:
    return await platform_on(db, settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path,
                                              HG_EXTENSIONS=",".join(extensions_under_test())))


def person(platform: Platform, name: str) -> httpx.AsyncClient:
    """A caller the sample edition signs in: `member…` reaches only what is their own, anyone else everyone's."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(platform)), base_url="http://hg", timeout=30,
                             headers={"x-sample-user": f"{ORG}/{name}"})


async def ready(platform: Platform, **environment) -> None:
    """The smallest setup a turn can run on: the echo harness, in an environment that carries it."""
    await platform.resources.add_package(ORG, "echo", package(), by=BY)
    await platform.resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["echo"], **environment}, by=BY)


async def run(platform: Platform, **request) -> Turn:
    """Send a request and run its turn to the end."""
    from harnessgateway.api.responses import accept
    await accept(platform, WHO, {"model": LINE, "input": "hi", **request})
    ran = await run_next(platform)
    assert ran is not None
    return ran


async def work_files(platform: Platform, turn: Turn) -> dict[str, bytes]:
    manifest = await platform.snapshots.manifest(ORG, turn.snapshot_id)
    return {path: b"".join([piece async for piece in platform.snapshots.content(ORG, entry)])
            for path, entry in manifest.files("work/").items()}
