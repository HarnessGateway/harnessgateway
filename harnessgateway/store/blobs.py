"""The blob store: bytes by key. The core's store is a directory (spec §12.1); others plug in (§19)."""

import asyncio
import os
import re
import tempfile
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

PIECE = 1024 * 1024
_KEY = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*")


class BlobMissing(KeyError):
    """Nothing is stored under this key."""


def check_key(key: str) -> str:
    """A key is parts of letters, digits, '_', '-' and '.' joined by '/'. No part starts with a dot, so no key
    can climb out of a store."""
    if not _KEY.fullmatch(key):
        raise ValueError(f"not a blob key: {key[:120]!r}")
    return key


class BlobStore(ABC):
    @abstractmethod
    async def write(self, key: str, parts: AsyncIterable[bytes]) -> int:
        """Store a stream under a key, replacing what was there. All of it or none. Returns the byte count."""

    @abstractmethod
    def read(self, key: str) -> AsyncIterator[bytes]:
        """What is stored under a key, in pieces. Raises BlobMissing."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a key. Removing one that is not there is not an error."""

    async def put(self, key: str, data: bytes) -> None:
        async def once() -> AsyncIterator[bytes]:
            yield data
        await self.write(key, once())

    async def get(self, key: str) -> bytes:
        return b"".join([piece async for piece in self.read(key)])


class DirectoryBlobs(BlobStore):
    """One file per key under a directory. A write lands under a temporary name and is moved into place."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / check_key(key)

    async def write(self, key: str, parts: AsyncIterable[bytes]) -> int:
        path = self._path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        handle, temporary = await asyncio.to_thread(tempfile.mkstemp, dir=path.parent, prefix=".part-")
        size = 0
        try:
            with os.fdopen(handle, "wb") as file:
                async for part in parts:
                    await asyncio.to_thread(file.write, part)
                    size += len(part)
            await asyncio.to_thread(os.replace, temporary, path)
        except BaseException:
            await asyncio.to_thread(Path(temporary).unlink, missing_ok=True)
            raise
        return size

    async def read(self, key: str) -> AsyncIterator[bytes]:
        try:
            file = await asyncio.to_thread(self._path(key).open, "rb")
        except FileNotFoundError:
            raise BlobMissing(key) from None
        with file:
            while piece := await asyncio.to_thread(file.read, PIECE):
                yield piece

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)
