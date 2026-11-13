"""The console in a real browser (spec §20): console.mjs, run against a whole platform on the local driver.

Skipped where there is no Node.js with Playwright, and (like every test that needs it) where there is no
PostgreSQL server.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from harnessgateway.domain.principal import Principal
from harnessgateway.extensions import Extension, Extensions
from harnessgateway.stores import Stores
from tests.integration.api import running

SCRIPT = Path(__file__).with_name("console.mjs")
SIGN_IN_SCRIPT = Path(__file__).with_name("signin.mjs")
NODE = shutil.which("node")
PLAYWRIGHT = bool(NODE) and subprocess.run(
    [NODE or "node", "-e", "require('playwright')"], capture_output=True, check=False).returncode == 0

pytestmark = pytest.mark.skipif(not PLAYWRIGHT, reason="needs Node.js with Playwright to run the console in a browser")


async def in_a_browser(script: Path, **variables: str) -> None:
    assert NODE
    process = await asyncio.create_subprocess_exec(
        NODE, str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **variables})
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


async def test_the_console_does_its_work_in_a_browser(stores: Stores, org: str, tmp_path: Path) -> None:
    async with running(stores, org, tmp_path) as api:
        await in_a_browser(SCRIPT, HG_TEST_URL=api.url, HG_TEST_KEY=api.keys["admin"],
                           HG_TEST_SCREENSHOT=str(tmp_path / "console-failure.png"))


class OtherWayIn(Extension):
    """An extension that signs people in its own way: a cookie it sets, and reads on every request."""

    def __init__(self, org: str, files: Path) -> None:
        self._org, self._files = org, files
        files.mkdir()
        (files / "extensions.js").write_text(
            'import { post } from "/api.js";\n'
            "export const pages = [];\n"
            'export const signIns = [{ title: "Sign in the other way", href: "/other/login" }];\n'
            'export const signOut = () => post("/other/logout", {});\n')

    async def authenticate(self, request: Request) -> Principal | None:
        return Principal(self._org, "user_test", "developer") if request.cookies.get("other") == "in" else None

    def routers(self, platform: Any) -> list[APIRouter]:
        router = APIRouter()

        @router.get("/other/login")
        async def login() -> RedirectResponse:
            response = RedirectResponse("/", status_code=303)
            response.set_cookie("other", "in", httponly=True, samesite="strict")
            return response

        @router.post("/other/logout")
        async def logout() -> JSONResponse:
            response = JSONResponse({})
            response.delete_cookie("other")
            return response

        return [router]

    def console(self) -> list[Path]:
        return [self._files]


async def test_an_extension_s_own_way_of_signing_in_works_in_the_console(
    stores: Stores, org: str, tmp_path: Path,
) -> None:
    extensions = Extensions([OtherWayIn(org, tmp_path / "console")])
    async with running(stores, org, tmp_path, extensions) as api:
        await in_a_browser(SIGN_IN_SCRIPT, HG_TEST_URL=api.url)
