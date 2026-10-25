"""Backend: where a turn runs. A `Node` prepares the base image, builds a package's tools layer once
per commit, and hands out a `Runtime` (see `kernel.execution`) for one turn with the organization's
own uid and ceilings. `docker` is the sandbox (a sandboxed Docker runtime
such as gVisor's runsc is the same backend with `runtime` set); `process` runs turns as plain
processes for developing packages and is never for anyone else's code.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
from pathlib import Path
from typing import AsyncIterator, Protocol, Sequence

from ...contract.package import Config
from ...kernel.limits import Limits
from ...kernel.execution import Event, Outcome

MARKER = ".hg-installed"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
UID_BASE, UID_RANGE = 100_000, 100_000


class InstallFailed(RuntimeError):
    pass


class Node(Protocol):
    """One worker machine's capabilities."""
    gateway_url: str

    async def prepare(self) -> str:
        """The base image, ready; its id, which keys the tools layers."""
        ...

    async def recover(self) -> None:
        """Remove whatever runs a crash cut off."""
        ...

    def runtime_for(self, scope: str, limits: Limits, *, read_only: Sequence[Path] = (), writable: Sequence[Path] = (),
                    egress: Sequence[str] = (), gateway_url: str = "", token: str = ""):
        """A runtime for one turn of `scope`, the organization whose turn it is."""
        ...

    async def tools_for(self, scope: str, limits: Limits, folder: Path, config: Config, root: Path, image_id: str) -> Path:
        """The package's tools layer, installed once per commit and image."""
        ...


def uid_for(scope: str) -> int:
    """The unprivileged uid a scope's runs use on a node: the same for the same scope everywhere."""
    return UID_BASE + int(hashlib.sha256(scope.encode()).hexdigest()[:8], 16) % UID_RANGE


def path_with(tools: Path) -> str:
    return f"{tools}/bin:{SYSTEM_PATH}"


def describe(outcome: Outcome) -> str:
    if outcome.kind == "failed":
        return f"could not start: {outcome.reason}"
    if outcome.kind == "timed_out":
        return "ran out of time"
    return f"exited {outcome.code}" if outcome.code is not None else "was killed"


@contextlib.asynccontextmanager
async def locked(path: Path) -> AsyncIterator[None]:
    """Hold an exclusive lock on `path`, waiting for it without blocking the loop."""
    with open(path, "a") as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def log_line(event: Event) -> bytes:
    if event.stream != "door":
        return event.data
    return b"".join(b"door: " + line + b"\n" for line in event.data.splitlines())


def open_node(kind: str, **kw) -> Node:
    """A node by the name `Settings.sandbox` gives it."""
    if kind == "docker":
        from .docker import DockerNode
        return DockerNode(**kw)
    if kind == "process":
        from .process import ProcessNode
        return ProcessNode(**kw)
    raise ValueError(f"a sandbox backend is docker or process, not {kind!r}")
