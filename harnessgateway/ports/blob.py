"""The object store port (SPEC §7.3): blobs, manifests, requests, responses, traces, install archives, files."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def put_file(self, key: str, path: Path) -> None: ...

    async def get(self, key: str) -> bytes:
        """KeyError when the object is not there."""
        ...

    def stream(self, key: str) -> AsyncIterator[bytes]: ...

    async def size(self, key: str) -> int | None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> None: ...
