"""End-to-end tests run real containers. They run only when HG_TEST_DOCKER=1 (spec §24)."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from harnessgateway.drivers.docker import DockerDriver

IMAGE = os.environ.get("HG_TEST_IMAGE", "node:22-slim")      # any image with sh; steps stand in with it too


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("HG_TEST_DOCKER") != "1":
        skip = pytest.mark.skip(reason="set HG_TEST_DOCKER=1 to run tests that need Docker")
        for item in items:
            if "/e2e/" in str(item.path):
                item.add_marker(skip)


@pytest.fixture
def unique() -> str:
    """A suffix that keeps one test's containers and volumes apart from another's."""
    return uuid.uuid4().hex[:12]


@pytest.fixture
async def driver() -> AsyncIterator[DockerDriver]:
    driver = DockerDriver(platform_image=IMAGE, runtime=os.environ.get("HG_TEST_RUNTIME", "runc"))
    yield driver
    await driver.reap(keep=set())
