"""What the tests share: a smallest package, the lines a run prints, and a driver whose folders are real (the
Docker driver's own file work, on this machine) and whose sandbox is this process: it writes the files and prints
the lines a test tells it to."""

import asyncio
import json
from pathlib import Path

from harnessgateway.domain import contract
from harnessgateway.domain.package import PackageFile, write_archive
from harnessgateway.domain.sandbox import SandboxPlan
from harnessgateway.driver.base import DriverError, SandboxExit
from harnessgateway.driver.folders import LocalFolders

MANIFEST = {"contract": 1, "name": "echo", "version": "1.0", "apis": []}
TEXT = [
    {"type": "response.output_item.added",
     "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}},
    {"type": "response.output_text.delta", "item_id": "msg_1", "content_index": 0, "delta": "hel", "logprobs": []},
    {"type": "response.output_item.done",
     "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "hello", "annotations": []}]}},
]
DONE = {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 2}}}


def package(manifest: dict = MANIFEST, **more: bytes) -> bytes:
    return write_archive([PackageFile("harness.json", json.dumps(manifest).encode()),
                          PackageFile("install", b"#!/bin/sh\n", True), PackageFile("run", b"#!/bin/sh\n", True),
                          *(PackageFile(name, content) for name, content in more.items())])


def lines(*objects: dict) -> list[str]:
    return [json.dumps(each) for each in objects]


class FakeDriver(LocalFolders):
    """What each sandbox was given (its plan, and the files under /turn) is kept for a test to look at."""

    name = "fake"

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.plans: list[SandboxPlan] = []
        self.found: list[dict[str, str]] = []      # for each turn's sandbox: the files under /turn, by path
        self.prints = lines(*TEXT, DONE)
        self.writes: dict[str, bytes | int] = {}   # path under /work -> content, or the size of a file of zeros
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

    async def run(self, plan: SandboxPlan, on_stdout, on_stderr) -> SandboxExit:
        self.plans.append(plan)
        if plan.command.endswith("/install"):
            await on_stderr("installing")
            return self.install_exit
        turn, work = self.folder(plan, contract.TURN), self.folder(plan, contract.WORK)
        self.found.append({path.relative_to(turn).as_posix(): path.read_text() for path in turn.rglob("*") if path.is_file()})
        for path, content in self.writes.items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, int):
                with (work / path).open("wb") as file:
                    file.truncate(content)
            else:
                (work / path).write_bytes(content)
        for text in self.prints:
            await on_stdout(text)
        if self.hold:
            await self.stopped.wait()
        return self.exit

    async def stop(self, name: str, grace_s: int) -> None:
        self.stopped.set()

    async def discard(self, name: str) -> None:
        self.discarded.append(name)
