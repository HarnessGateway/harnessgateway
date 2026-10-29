"""The object store port: package archives, request bodies, files, snapshots, captures and logs (SPEC §10.3)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def put_file(self, key: str, path: Path) -> None: ...

    async def get(self, key: str) -> bytes: ...

    def stream(self, key: str) -> AsyncIterator[bytes]: ...

    async def size(self, key: str) -> int | None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    def link_get(self, key: str, ttl_s: int) -> str:
        """A URL a node can GET the object from, with no other credential, until the TTL passes."""
        ...

    def link_put(self, key: str, ttl_s: int) -> str:
        """A URL a node can PUT the object to, with no other credential, until the TTL passes."""
        ...
