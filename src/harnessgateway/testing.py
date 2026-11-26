"""What tests of the platform, and of anything built on it, are written with: the smallest package, the lines a run
prints, a driver whose folders are real and whose sandbox is the test's own process, and a platform on a database
a test may empty. Nothing in the platform imports this."""

import asyncio
import base64
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from harnessgateway.api.app import internal_app, public_app
from harnessgateway.domain import contract
from harnessgateway.domain.package import PackageFile, write_archive
from harnessgateway.domain.sandbox import SandboxPlan
from harnessgateway.driver.base import DriverError, OnLine, SandboxExit
from harnessgateway.driver.folders import LocalFolders
from harnessgateway.platform import Platform
from harnessgateway.runner.turn import TurnRunner
from harnessgateway.serve import prepared
from harnessgateway.settings import Settings
from harnessgateway.store.db import Database
from harnessgateway.store.turns import Turn

TEXT: list[dict[str, Any]] = [
    {"type": "response.output_item.added",
     "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}},
    {"type": "response.output_text.delta", "item_id": "msg_1", "content_index": 0, "delta": "hel", "logprobs": []},
    {"type": "response.output_item.done",
     "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "hello", "annotations": []}]}},
]
DONE: dict[str, Any] = {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 2}}}


def package(**more: bytes) -> bytes:
    """The smallest package: its two programs, and whatever else is given."""
    return write_archive([PackageFile("install", b"#!/bin/sh\n", True), PackageFile("run", b"#!/bin/sh\n", True),
                          *(PackageFile(name, content) for name, content in more.items())])


def lines(*objects: Mapping[str, Any]) -> list[str]:
    return [json.dumps(each) for each in objects]


class FakeDriver(LocalFolders):
    """A driver whose folders are the Docker driver's own file work, on this machine, and whose sandbox is this
    process: it writes the files and prints the lines a test tells it to. What each sandbox was given (its plan,
    and the files under /turn) is kept for a test to look at."""

    name = "fake"

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.plans: list[SandboxPlan] = []
        self.found: list[dict[str, str]] = []      # for each turn's sandbox: the files under /turn, by path
        self.prints = lines(*TEXT, DONE)
        self.writes: dict[str, bytes | int] = {}   # path under /work -> content, or the size of a file of zeros
        self.remembers: dict[str, bytes] = {}      # path under /mind -> content
        self.exit, self.install_exit = SandboxExit(True, 0), SandboxExit(True, 0)
        self.hold = False
        self.stopped = asyncio.Event()
        self.discarded: list[str] = []

    def folder(self, plan: SandboxPlan, path: str) -> Path:
        return self.path(next(mount.folder for mount in plan.mounts if mount.path == path))

    async def isolation(self) -> str:
        return "none"

    async def image_id(self, ref: str) -> str:
        if ref.startswith("missing"):
            raise DriverError(f"docker pull failed: {ref} not found")
        return f"sha256:{ref}"

    async def run(self, plan: SandboxPlan, on_stdout: OnLine, on_stderr: OnLine) -> SandboxExit:
        self.plans.append(plan)
        if plan.command.endswith("/install"):
            await on_stderr("installing")
            return self.install_exit
        turn, work = self.folder(plan, contract.TURN), self.folder(plan, contract.WORK)
        self.found.append({path.relative_to(turn).as_posix(): path.read_text()
                           for path in turn.rglob("*") if path.is_file()})
        for path, content in self.writes.items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, int):
                with (work / path).open("wb") as file:
                    file.truncate(content)
            else:
                (work / path).write_bytes(content)
        for path, content in self.remembers.items():
            (self.folder(plan, contract.MIND) / path).parent.mkdir(parents=True, exist_ok=True)
            (self.folder(plan, contract.MIND) / path).write_bytes(content)
        for text in self.prints:
            await on_stdout(text)
        if self.hold:
            await self.stopped.wait()
        return self.exit

    async def stop(self, name: str, grace_s: int) -> None:
        self.stopped.set()

    async def discard(self, name: str) -> None:
        self.discarded.append(name)


def settings_for(url: str, data: Path, **more: str) -> Settings:
    """Settings for a platform under test: a sealing key that is no secret, and its data in a folder of the test's."""
    return Settings.from_env({"HG_DATABASE_URL": url, "HG_SECRET_KEY": base64.b64encode(bytes(range(32))).decode(),
                              "HG_PROXY_KEY": "proxy-key", "HG_DATA": str(data), **more})


def extensions_under_test() -> Sequence[str]:
    """The extensions the platform's own tests are run with: `HG_TEST_EXTENSIONS`, as `HG_EXTENSIONS` names them.
    An edition runs the layers below it this way, to show that it changes nothing they do."""
    return tuple(os.environ.get("HG_TEST_EXTENSIONS", "").replace(",", " ").split())


async def emptied(url: str) -> Database:
    """The database at `url`, open and with nothing in it. It is the caller's to close."""
    database = Database(url)
    await database.open()
    async with database.tx() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    return database


async def platform_on(db: Database, settings: Settings) -> Platform:
    """A platform as `serve` makes one, on an open database, with the stand-in driver for its sandboxes unless the
    settings name a driver of an extension's."""
    stand_in = FakeDriver(settings.data / "folders") if settings.driver == "docker" else None
    return await prepared(settings, db, driver=stand_in)


async def run_next(platform: Platform) -> Turn | None:
    """Run the turn that has waited longest to its end, as a runner does, with the platform's own driver. None
    when no turn is waiting."""
    claimed = await platform.turns.claim("runner-tests")
    return await TurnRunner(platform).run(claimed) if claimed is not None else None


def public(platform: Platform, key: str = "") -> httpx.AsyncClient:
    """A caller of the platform's public API as `serve` serves it, with no network between; with `key`, as whoever
    holds that credential."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(platform)), base_url="http://hg", timeout=30,
                             headers={"Authorization": f"Bearer {key}"} if key else {})


def internal(platform: Platform) -> httpx.AsyncClient:
    """The egress proxy's side of the platform's internal API, with the key the two share."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_app(platform)), base_url="http://internal",
                             timeout=30, headers={"Authorization": f"Bearer {platform.settings.proxy_key}"})
