"""Where bytes are kept: snapshots' chunks and manifests, package archives, uploaded files. The core keeps them in
a directory; an edition supplies a store for another place (object storage)."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

_PIECE = 1024 * 1024


class BlobMissing(Exception):
    """There is no blob under that key."""


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None:
        """Keep bytes under a key, whole: they are there complete, or not at all."""

    async def get(self, key: str) -> bytes:
        """The bytes under a key. BlobMissing when there are none."""

    async def write(self, key: str, parts: AsyncIterator[bytes]) -> int:
        """Keep a blob too large to hold in memory, as it arrives. How many bytes it was."""

    def read(self, key: str) -> AsyncIterator[bytes]:
        """A blob, a piece at a time."""

    async def delete(self, key: str) -> None:
        """Forget a key. Forgetting one that is not there is not an error."""


class DirectoryBlobs:
    """Blobs as files under one directory. A write goes to a file beside its place and is moved in when whole."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / key

    def _partial(self, key: str) -> Path:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.with_name(f".{path.name}.{os.getpid()}.{id(asyncio.current_task())}.part")

    async def put(self, key: str, data: bytes) -> None:
        partial = self._partial(key)
        await asyncio.to_thread(partial.write_bytes, data)
        os.replace(partial, self._path(key))

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError:
            raise BlobMissing(key) from None

    async def write(self, key: str, parts: AsyncIterator[bytes]) -> int:
        partial, size = self._partial(key), 0
        try:
            with partial.open("wb") as out:
                async for part in parts:
                    size += await asyncio.to_thread(out.write, part)
            os.replace(partial, self._path(key))
        finally:
            partial.unlink(missing_ok=True)
        return size

    async def read(self, key: str) -> AsyncIterator[bytes]:
        try:
            file = self._path(key).open("rb")
        except FileNotFoundError:
            raise BlobMissing(key) from None
        with file:
            while piece := await asyncio.to_thread(file.read, _PIECE):
                yield piece

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
