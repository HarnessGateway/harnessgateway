"""The console in a real browser (spec §20): console.mjs, run against a whole platform on the local driver.

Skipped where there is no Node.js with Playwright, and (like every test that needs it) where there is no
PostgreSQL server.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harnessgateway.stores import Stores
from tests.integration.api import running

SCRIPT = Path(__file__).with_name("console.mjs")
NODE = shutil.which("node")
PLAYWRIGHT = bool(NODE) and subprocess.run(
    [NODE or "node", "-e", "require('playwright')"], capture_output=True, check=False).returncode == 0

pytestmark = pytest.mark.skipif(not PLAYWRIGHT, reason="needs Node.js with Playwright to run the console in a browser")


async def test_the_console_does_its_work_in_a_browser(stores: Stores, org: str, tmp_path: Path) -> None:
    async with running(stores, org, tmp_path) as api:
        assert NODE
        process = await asyncio.create_subprocess_exec(
            NODE, str(SCRIPT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "HG_TEST_URL": api.url, "HG_TEST_KEY": api.keys["admin"],
                 "HG_TEST_SCREENSHOT": str(tmp_path / "console-failure.png")})
        output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()
