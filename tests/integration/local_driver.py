"""A driver for tests that need no Docker: volumes are folders, steps run in this process against the real
internal API, and a sandbox is the package's `run` started as a local process.

It keeps the driver's promises that matter to the turn runner: plans are run as written, lines arrive as they
are printed, a stop ends the process, a missing program is a sandbox that never started. It enforces no
limits: those are the end-to-end tests' business.
"""

import asyncio
import os
import shutil
import signal
from pathlib import Path

import httpx
from fastapi import FastAPI

from harnessgateway.domain.plan import Mount, SandboxPlan, StepPlan
from harnessgateway.drivers.base import OnLine, SandboxExit, StepResult
from harnessgateway.drivers.docker import LineSplitter
from harnessgateway.steps.errors import StepError
from harnessgateway.steps.http import HttpStepClient
from harnessgateway.steps.restore import restore
from harnessgateway.steps.save import save
from harnessgateway.steps.tree import measure
from harnessgateway.steps.unpack import unpack
from harnessgateway.steps.watch import OVER_LIMIT

_MIB = 1024 * 1024


async def _pump(stream: asyncio.StreamReader, on_line: OnLine) -> None:
    splitter = LineSplitter()
    while chunk := await stream.read(65536):
        for line in splitter.feed(chunk):
            await on_line(line)
    for line in splitter.finish():
        await on_line(line)


class LocalDriver:
    name = "local"

    def __init__(self, base: Path, internal: FastAPI) -> None:
        self._base = base
        self._internal = internal
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Event] = {}
        self.sandboxes: list[SandboxPlan] = []                 # every sandbox plan that was run, in order
        self.steps: list[str] = []                             # every step that was run, by name
        self.images: dict[str, str] = {}                       # image reference -> id, when a test wants to choose

    def volume(self, name: str) -> Path:
        return self._base / name

    def _at(self, mounts: list[Mount], path: str) -> str:
        """A path inside the container, as the place it has on this machine."""
        for mount in mounts:
            if path == mount.path or path.startswith(mount.path + "/"):
                return str(self.volume(mount.volume)) + path[len(mount.path):]
        return path

    async def isolation(self) -> str:
        return "none"

    async def image_id(self, ref: str) -> str:
        return self.images.get(ref, f"sha256:{ref}")

    async def describe(self, plan: SandboxPlan) -> str:
        return f"local {plan.name} {' '.join(plan.command)}"

    async def ensure_volume(self, name: str) -> None:
        self.volume(name).mkdir(parents=True, exist_ok=True)

    async def remove_volume(self, name: str) -> None:
        shutil.rmtree(self.volume(name), ignore_errors=True)

    async def run_step(self, step: StepPlan) -> StepResult:
        self.steps.append(step.name)
        _, _, kind, target, *extra = step.command
        places = {mount.path: self.volume(mount.volume) for mount in step.mounts}
        roots = {"mind": places.get("/mind", self._base / "none"), "work": places.get("/work", self._base / "none")}
        if kind == "watch":
            return await self._watch(step.name, roots, int(extra[1]) * _MIB)
        client = HttpStepClient(step.env["HG_API"], step.env["HG_STEP_TOKEN"],
                                transport=httpx.ASGITransport(app=self._internal))
        try:
            if kind == "restore":
                await restore(client, target, roots, places["/turn"])
            elif kind == "save":
                saved = await save(client, target, roots)
                return StepResult(0, f"saved as {saved.snapshot_id}: {saved.uploaded} new chunks")
            else:
                await unpack(client, target, places["/harness"])
        except StepError as e:
            return StepResult(1, f"{kind} failed: {e}")
        finally:
            await client.close()
        return StepResult(0, "")

    async def _watch(self, name: str, roots: dict[str, Path], limit: int) -> StepResult:
        stopped = self._watchers.setdefault(name, asyncio.Event())
        try:
            while not stopped.is_set():
                if measure(roots) > limit:
                    return StepResult(OVER_LIMIT, "over the limit")
                await asyncio.sleep(0.05)
            return StepResult(137, "stopped")
        finally:
            del self._watchers[name]

    async def run_sandbox(self, plan: SandboxPlan, on_stdout: OnLine, on_stderr: OnLine) -> SandboxExit:
        self.sandboxes.append(plan)
        env = {name: self._at(plan.mounts, value) for name, value in plan.env.items()}
        program = self._at(plan.mounts, plan.command[0])
        try:
            process = await asyncio.create_subprocess_exec(
                program, *plan.command[1:], cwd=self._at(plan.mounts, plan.workdir),
                env={"PATH": os.environ["PATH"], **env}, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except OSError as e:
            return SandboxExit(started=False, error=str(e))
        self._processes[plan.name] = process
        try:
            assert process.stdout is not None
            assert process.stderr is not None
            await asyncio.gather(_pump(process.stdout, on_stdout), _pump(process.stderr, on_stderr))
            return SandboxExit(started=True, exit_code=await process.wait())
        finally:
            del self._processes[plan.name]

    async def stop(self, name: str, grace_s: int) -> None:
        if name in self._watchers:
            self._watchers[name].set()
        process = self._processes.get(name)
        if process is None or process.returncode is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), grace_s)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)

    async def reap(self, keep: set[str]) -> list[str]:
        gone = [name for name in self._processes if name not in keep]
        for name in gone:
            await self.stop(name, 0)
        return gone
