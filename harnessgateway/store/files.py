"""Uploaded files (spec §13.9)."""

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from harnessgateway.errors import Invalid, NotFound, TooLarge
from harnessgateway.ids import new_id
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import Database

FILE_MAX = 512 * 1024 * 1024
_COLUMNS = "id, filename, purpose, bytes, object_key, created_at"


@dataclass(frozen=True)
class File:
    id: str
    filename: str
    purpose: str
    bytes: int
    object_key: str
    created_at: datetime


def check_filename(value: object) -> str:
    """A file's name is one plain name: it is written into a folder as it is."""
    if (not isinstance(value, str) or not 0 < len(value) <= 255 or "/" in value or "\x00" in value
            or value in (".", "..")):
        raise Invalid(f"a file name is 1 to 255 characters with no '/': {str(value)[:80]!r}", param="file")
    return value


async def _limited(parts: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    size = 0
    async for part in parts:
        size += len(part)
        if size > FILE_MAX:
            raise TooLarge(f"a file is at most {FILE_MAX >> 20} MiB")
        yield part


class Files:
    def __init__(self, db: Database, blobs: BlobStore) -> None:
        self._db = db
        self._blobs = blobs

    async def create(self, org_id: str, *, filename: str, purpose: str, parts: AsyncIterable[bytes]) -> File:
        check_filename(filename)
        if not purpose or len(purpose) > 64:
            raise Invalid("purpose is 1 to 64 characters", param="purpose")
        file_id = new_id("file")
        key = f"orgs/{org_id}/files/{file_id}"
        size = await self._blobs.write(key, _limited(parts))
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "INSERT INTO files (id, org_id, filename, purpose, bytes, object_key) "
                f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                [file_id, org_id, filename, purpose, size, key])).fetchone()
        assert row is not None
        return File(**row)

    async def get(self, org_id: str, file_id: str) -> File:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM files WHERE org_id = %s AND id = %s", [org_id, file_id])).fetchone()
        if row is None:
            raise NotFound(f"there is no file {file_id}")
        return File(**row)

    async def page(self, org_id: str, *, after: str | None = None, limit: int = 20) -> list[File]:
        """Files, newest first; those before `after` when it is given."""
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM files WHERE org_id = %s AND (%s::text IS NULL OR id < %s) "
                "ORDER BY id DESC LIMIT %s", [org_id, after, after, limit])).fetchall()
        return [File(**row) for row in rows]

    def content(self, file: File) -> AsyncIterator[bytes]:
        return self._blobs.read(file.object_key)

    async def delete(self, org_id: str, file_id: str) -> None:
        file = await self.get(org_id, file_id)
        async with self._db.org(org_id) as conn:
            await conn.execute("DELETE FROM files WHERE org_id = %s AND id = %s", [org_id, file_id])
        await self._blobs.delete(file.object_key)
