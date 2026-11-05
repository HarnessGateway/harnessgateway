"""Files (`/v1/files`): inputs a request references by `file_id`, delivered to the sandbox at $HG_FILES. Files are the
organization's; a run-only key sees the ones it uploaded."""
from __future__ import annotations

import posixpath
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .. import ids
from ..db import Database
from ..domain.access import Principal
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

    @staticmethod
    def _own(p: Principal) -> tuple[str, list[str]]:
        return (" AND created_by = %s", [p.actor]) if p.owns_only else ("", [])

    async def upload(self, p: Principal, filename: str, purpose: str, path: Path) -> dict[str, Any]:
        p.require("files", "create")
        if path.stat().st_size > FILE_UPLOAD_MAX:
            raise TooLarge(f"a file is at most {FILE_UPLOAD_MAX >> 20} MiB")
        name = posixpath.basename((filename or "").replace("\\", "/"))[:255]
        if not name or name in (".", ".."):
            raise Invalid("the upload needs a filename", param="file")
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 64:
            raise Invalid("purpose is a short string, e.g. user_data", param="purpose")
        fid = ids.new("file")
        key = org_key(p.org_id, "files", fid)
        size = await self.objects.put_file(key, path, org_id=p.org_id, kind="file")
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("INSERT INTO files (id, org_id, created_by, filename, purpose, bytes, object_key) VALUES "
                         "(%s, %s, %s, %s, %s, %s, %s)", fid, p.org_id, p.actor, name, purpose, size, key)
            return _view(await tx.get("SELECT * FROM files WHERE id = %s", fid))

    async def list(self, p: Principal, purpose: str | None, limit: int, after: str | None) -> dict[str, Any]:
        p.require("files", "read")
        limit = check_limit(limit)
        clause, args = self._own(p)
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all(f"SELECT * FROM files WHERE deleted_at IS NULL{clause} AND (%s::text IS NULL OR purpose = %s) "
                                "AND (%s::text IS NULL OR created_at < (SELECT created_at FROM files WHERE id = %s)) "
                                "ORDER BY created_at DESC LIMIT %s", *args, purpose, purpose, after, after, limit + 1)
        return page([_view(r) for r in rows], limit)

    async def _row(self, p: Principal, fid: str) -> dict[str, Any]:
        p.require("files", "read")
        clause, args = self._own(p)
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one(f"SELECT * FROM files WHERE id = %s AND deleted_at IS NULL{clause}", ids.check(fid, "file"),
                               *args)
        if not row:
            raise NotFound(f"no file {fid}")
        return row

    async def get(self, p: Principal, fid: str) -> dict[str, Any]:
        return _view(await self._row(p, fid))

    async def content(self, p: Principal, fid: str) -> tuple[dict[str, Any], AsyncIterator[bytes]]:
        row = await self._row(p, fid)
        return _view(row), self.objects.stream(row["object_key"])

    async def delete(self, p: Principal, fid: str) -> dict[str, Any]:
        p.require("files", "delete")
        row = await self._row(p, fid)
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("UPDATE files SET deleted_at = now() WHERE id = %s", fid)
        await self.objects.delete(row["object_key"])
        return {"id": fid, "object": "file", "deleted": True}
