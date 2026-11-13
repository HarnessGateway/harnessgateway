"""What a step asks of the platform (spec §15.1). Steps depend on this, not on how it is reached."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class InputFile:
    """An uploaded file a request refers to."""

    id: str
    filename: str
    key: str                                   # where its content is stored


@dataclass(frozen=True)
class RestorePlan:
    manifest_key: str | None                   # the parent snapshot's manifest; None: start from nothing
    warm: bool                                 # the volumes already hold exactly that snapshot
    request: dict[str, Any]                    # the request body, as the caller sent it
    history: list[dict[str, Any]]              # the turns this harness has not seen (spec §8.6)
    handover: str                              # the handover line, or ""
    files: tuple[InputFile, ...]


class StepClient(Protocol):
    """Every method raises `Refused` when the platform refuses what was asked."""

    async def restore_plan(self, turn_id: str) -> RestorePlan: ...

    async def save_plan(self, turn_id: str) -> str | None:
        """The key of the parent snapshot's manifest, if the turn has a parent snapshot."""

    async def record(self, turn_id: str, manifest: str) -> str:
        """Record the turn's snapshot. Returns its id."""

    def object(self, key: str) -> AsyncIterator[bytes]:
        """A manifest, an uploaded file or a package archive, in pieces."""

    async def missing(self, hashes: list[str]) -> list[str]: ...

    async def chunk(self, digest: str) -> bytes: ...

    async def put_chunk(self, digest: str, data: bytes) -> None: ...


async def read_manifest_text(client: StepClient, key: str) -> str:
    return b"".join([piece async for piece in client.object(key)]).decode()
