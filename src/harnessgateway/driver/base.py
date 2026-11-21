"""What a driver must be able to do: keep the folders sandboxes are given, move a session's files in and out of
them, and run a sandbox. The core's driver is Docker on one host; an edition supplies others (a cluster).

A folder is named by the plan that mounts it (`sessions/<id>/work`, `turns/<id>`, …). Where and how a driver
keeps it is the driver's own business, which is what lets the same turn run on any of them.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from harnessgateway.domain.sandbox import SandboxPlan
from harnessgateway.domain.snapshot import Manifest

OnLine = Callable[[str], Awaitable[None]]


class DriverError(Exception):
    """The infrastructure refused or failed. The message is what it said."""


class ChunkStore(Protocol):
    """Where a session's files are kept between turns, a chunk at a time, each under its hash."""

    async def missing(self, hashes: Sequence[str]) -> set[str]: ...

    async def put(self, digest: str, data: bytes) -> None: ...

    async def get(self, digest: str) -> bytes: ...


@dataclass(frozen=True)
class Measured:
    """What a sandbox used while it ran. Zero where a driver cannot measure."""

    cpu_seconds: float = 0.0                   # processor time, all processors added up
    memory_byte_seconds: float = 0.0           # the memory it held, times how long it held it


@dataclass(frozen=True)
class SandboxExit:
    started: bool                              # False: the sandbox never ran; `error` says why
    exit_code: int | None = None
    out_of_memory: bool = False
    error: str = ""
    measured: Measured = Measured()


class Driver(Protocol):
    name: str

    async def isolation(self) -> str:
        """`gvisor`, `none`, …: what separates a sandbox from the host's kernel."""

    async def image_id(self, ref: str) -> str:
        """The immutable id of an image reference, fetching the image if need be."""

    async def fill(self, folder: str, files: Mapping[str, bytes], *, executable: Collection[str] = (),
                   streams: Mapping[str, AsyncIterator[bytes]] | None = None) -> None:
        """Make a folder hold exactly these files, by path, readable by the sandbox's user. `streams` are files
        too large to hold in memory."""

    async def restore(self, folders: Mapping[str, str], manifest: Manifest, chunks: ChunkStore) -> None:
        """Make a session's folders (by the root a manifest knows each as) hold exactly what a manifest names."""

    async def save(self, folders: Mapping[str, str], parent: Manifest, leave_out: frozenset[str],
                   chunks: ChunkStore) -> Manifest:
        """A session's folders as a manifest, with every chunk it names kept. `parent` is what the folders were
        restored from: a file it explains need not be read again. `leave_out` are paths not worth saving."""

    async def measure(self, folders: Mapping[str, str]) -> int:
        """The bytes these folders hold now, while a sandbox may be writing to them."""

    async def remove(self, folder: str) -> None:
        """Forget a folder. Forgetting one that is not there is not an error."""

    async def run(self, plan: SandboxPlan, on_stdout: OnLine, on_stderr: OnLine) -> SandboxExit:
        """Run a sandbox to its end, handing over each line it prints as it prints it."""

    async def stop(self, name: str, grace_s: int) -> None:
        """Stop a sandbox by its plan's name. Stopping what is not running is not an error."""

    async def reap(self) -> list[str]:
        """Remove every sandbox this installation left behind. Their names."""
