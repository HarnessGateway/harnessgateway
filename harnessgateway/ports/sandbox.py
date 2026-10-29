"""The sandbox runtime port (SPEC §6.1): boxes, each with its own door, on one node."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

UID = 10001


@dataclass(frozen=True)
class Mount:
    host: Path
    target: str
    readonly: bool = True


@dataclass(frozen=True)
class BoxSpec:
    name: str
    org_id: str
    vcpus: float
    memory_mb: int
    pids: int
    tmp_mb: int
    mounts: tuple[Mount, ...]
    door_dir: Path                  # the door's private directory: its rules, CA, counters and connection log
    env: dict[str, str] = field(default_factory=dict)


class Process(Protocol):
    async def read(self) -> bytes: ...

    def stderr(self) -> AsyncIterator[bytes]: ...

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


class Box(Protocol):
    spec: BoxSpec

    async def start(self) -> None: ...

    async def alive(self) -> bool: ...

    async def sh(self, script: str, *args: str, timeout: float = 300) -> bytes:
        """Run a bash script inside the box as its user; its stdout."""
        ...

    async def exec(self, argv: list[str], env: dict[str, str]) -> Process: ...

    async def tar_out(self, paths: list[str], dest: Path, timeout: float) -> None: ...

    async def tar_in(self, src: Path, timeout: float) -> None: ...

    async def terminate(self, pidfile: str) -> None: ...

    async def kill_all(self) -> None: ...

    async def destroy(self) -> None: ...


class Runtime(Protocol):
    async def prepare(self) -> str:
        """Make sure the images are present; the sandbox image's id (tools are installed per image)."""
        ...

    async def sweep(self) -> int:
        """Remove what an earlier run of this node left behind; how many boxes."""
        ...

    async def ca_bundle(self) -> bytes:
        """The sandbox image's own trusted CA bundle."""
        ...

    def box(self, spec: BoxSpec) -> Box: ...
