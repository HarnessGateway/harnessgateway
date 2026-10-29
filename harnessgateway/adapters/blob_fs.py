"""BlobStore on a local directory (`file:///var/lib/harnessgateway/blobs`), for one-host installs and tests. Writes land
atomically; links point at the API's own blob route, authorized by a signed token."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

CHUNK = 1024 * 1024


class FileBlobs:
    def __init__(self, url: str, link: Callable[[str, str, int], str]):
        parts = urlsplit(url)
        if parts.scheme != "file" or parts.netloc or not parts.path.startswith("/"):
            raise ValueError("a file blob URL is file:///absolute/path")
        self.root = Path(unquote(parts.path)).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._link = link

    def path(self, key: str) -> Path:
        segments = key.split("/")
        if not key or "\0" in key or key.startswith("/") or any(s in ("", ".", "..") for s in segments):
            raise ValueError(f"not a blob key: {key!r}")
        return self.root.joinpath(*segments)

    def _write(self, key: str, fill: Callable[[int], None]) -> None:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".put-")
        try:
            fill(fd)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def put(self, key: str, data: bytes) -> None:
        def fill(fd: int) -> None:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        await asyncio.to_thread(self._write, key, fill)

    async def put_file(self, key: str, path: Path) -> None:
        def fill(fd: int) -> None:
            with os.fdopen(fd, "wb") as out, path.open("rb") as src:
                shutil.copyfileobj(src, out, CHUNK)
        await asyncio.to_thread(self._write, key, fill)

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self.path(key).read_bytes)
        except FileNotFoundError:
            raise KeyError(key) from None

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        f = await asyncio.to_thread(self.path(key).open, "rb")
        try:
            while chunk := await asyncio.to_thread(f.read, CHUNK):
                yield chunk
        finally:
            f.close()

    async def size(self, key: str) -> int | None:
        p = self.path(key)
        return p.stat().st_size if p.is_file() else None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.path(key).unlink, missing_ok=True)

    async def delete_prefix(self, prefix: str) -> None:
        assert prefix.endswith("/"), "a prefix names a folder"
        await asyncio.to_thread(shutil.rmtree, self.path(prefix.rstrip("/")), True)

    def link_get(self, key: str, ttl_s: int) -> str:
        return self._link(key, "GET", ttl_s)

    def link_put(self, key: str, ttl_s: int) -> str:
        return self._link(key, "PUT", ttl_s)
