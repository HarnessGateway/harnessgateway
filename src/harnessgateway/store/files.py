"""Uploaded files: what a request's input may name, and a turn then finds in its sandbox."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from harnessgateway.errors import Invalid, NotFound, TooLarge
from harnessgateway.ids import new_id
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import Database

FILE_MAX = 512 * 1024 * 1024
_COLUMNS = "id, filename, purpose, bytes, created_at"


@dataclass(frozen=True)
class File:
    id: str
    filename: str
    purpose: str
    bytes: int
    created_at: datetime

    def shown(self) -> dict[str, Any]:
        """The file as the OpenAI Files API shows one."""
        return {"id": self.id, "object": "file", "bytes": self.bytes, "created_at": int(self.created_at.timestamp()),
                "filename": self.filename, "purpose": self.purpose, "status": "processed"}


def file_key(org_id: str, file_id: str) -> str:
    return f"orgs/{org_id}/files/{file_id}"


class Files:
    def __init__(self, db: Database, blobs: BlobStore) -> None:
        self._db = db
        self._blobs = blobs

    async def create(self, org_id: str, *, filename: str, purpose: str, parts: AsyncIterator[bytes]) -> File:
        """Keep an upload. Its name must be a plain file name: a turn finds it in its sandbox under that name."""
        if filename in ("", ".", "..") or "/" in filename or "\x00" in filename or len(filename) > 255:
            raise Invalid(f"{filename!r} is not a plain file name", param="file")
        file_id, key = new_id("file"), ""

        async def limited() -> AsyncIterator[bytes]:
            size = 0
            async for part in parts:
                size += len(part)
                if size > FILE_MAX:
                    raise TooLarge(f"a file is at most {FILE_MAX >> 20} MiB")
                yield part
        key = file_key(org_id, file_id)
        size = await self._blobs.write(key, limited())
        row = await self._db.one(org_id, "INSERT INTO files (id, org_id, filename, purpose, bytes) "
                                         f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                                 [file_id, org_id, filename, purpose, size])
        assert row is not None
        return File(**row)

    async def get(self, org_id: str, file_id: str) -> File:
        row = await self._db.one(org_id, f"SELECT {_COLUMNS} FROM files WHERE org_id = %s AND id = %s",
                                 [org_id, file_id])
        if row is None:
            raise NotFound(f"there is no file {file_id}")
        return File(**row)

    async def page(self, org_id: str, *, after: str | None, limit: int) -> list[File]:
        """Files, newest first; those after `after` in that order when it is given."""
        rows = await self._db.all(org_id, f"SELECT {_COLUMNS} FROM files WHERE org_id = %s AND "
                                          "(%s::text IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
                                  [org_id, after, after, limit])
        return [File(**row) for row in rows]

    def content(self, org_id: str, file_id: str) -> AsyncIterator[bytes]:
        return self._blobs.read(file_key(org_id, file_id))

    async def delete(self, org_id: str, file_id: str) -> None:
        if not await self._db.run(org_id, "DELETE FROM files WHERE org_id = %s AND id = %s", [org_id, file_id]):
            raise NotFound(f"there is no file {file_id}")
        await self._blobs.delete(file_key(org_id, file_id))
