"""Files (`/v1/files`): inputs a request references by `file_id`, delivered to the sandbox at $HG_FILES."""
from __future__ import annotations

import posixpath
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .. import ids
from ..db import Database
from ..errors import Invalid, NotFound, TooLarge
from ..limits import FILE_UPLOAD_MAX
from .objects import Objects, org_key
from .pages import check_limit, page, ts


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "file", "bytes": row["bytes"], "created_at": ts(row["created_at"]),
            "filename": row["filename"], "purpose": row["purpose"], "status": "processed"}


class Files:
    def __init__(self, db: Database, objects: Objects):
        self.db, self.objects = db, objects

    async def upload(self, org_id: str, filename: str, purpose: str, path: Path) -> dict[str, Any]:
        if path.stat().st_size > FILE_UPLOAD_MAX:
            raise TooLarge(f"a file is at most {FILE_UPLOAD_MAX >> 20} MiB")
        name = posixpath.basename((filename or "").replace("\\", "/"))[:255]
        if not name or name in (".", ".."):
            raise Invalid("the upload needs a filename", param="file")
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 64:
            raise Invalid("purpose is a short string, e.g. user_data", param="purpose")
        fid = ids.new("file")
        key = org_key(org_id, "files", fid)
        size = await self.objects.put_file(key, path, org_id=org_id, kind="file")
        async with self.db.tenant(org_id) as tx:
            await tx.run("INSERT INTO files (id, org_id, filename, purpose, bytes, object_key) VALUES (%s, %s, %s, %s, %s, %s)",
                         fid, org_id, name, purpose, size, key)
            return _view(await tx.get("SELECT * FROM files WHERE id = %s", fid))

    async def list(self, org_id: str, purpose: str | None, limit: int, after: str | None) -> dict[str, Any]:
        limit = check_limit(limit)
        async with self.db.tenant(org_id) as tx:
            rows = await tx.all("SELECT * FROM files WHERE deleted_at IS NULL AND (%s::text IS NULL OR purpose = %s) "
                                "AND (%s::text IS NULL OR created_at < (SELECT created_at FROM files WHERE id = %s)) "
                                "ORDER BY created_at DESC LIMIT %s", purpose, purpose, after, after, limit + 1)
        return page([_view(r) for r in rows], limit)

    async def _row(self, org_id: str, fid: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT * FROM files WHERE id = %s AND deleted_at IS NULL", ids.check(fid, "file"))
        if not row:
            raise NotFound(f"no file {fid}")
        return row

    async def get(self, org_id: str, fid: str) -> dict[str, Any]:
        return _view(await self._row(org_id, fid))

    async def content(self, org_id: str, fid: str) -> tuple[dict[str, Any], AsyncIterator[bytes]]:
        row = await self._row(org_id, fid)
        return _view(row), self.objects.stream(row["object_key"])

    async def delete(self, org_id: str, fid: str) -> dict[str, Any]:
        row = await self._row(org_id, fid)
        async with self.db.tenant(org_id) as tx:
            await tx.run("UPDATE files SET deleted_at = now() WHERE id = %s", fid)
        await self.objects.delete(row["object_key"])
        return {"id": fid, "object": "file", "deleted": True}
