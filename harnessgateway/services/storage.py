"""Files and containers, the OpenAI way. Files (`/v1/files`) are inputs a request references by `file_id`; the platform
puts them into the sandbox at $HG_FILES. A container (`/v1/containers`) is a session: its sandbox and workspace, which
the platform owns. A container's files are read from the workspace's last snapshot; files written to a container are
staged and land in the workspace when its next turn starts."""
from __future__ import annotations

import asyncio
import io
import json
import posixpath
import tarfile
import tempfile
import time
from collections.abc import Iterator
from typing import IO, Any

from .. import ids
from ..blobs import Blobs, org_key
from ..db import Database
from ..domain.orglimits import OrgLimits
from ..errors import Conflict, Invalid, NotFound, TooLarge
from ..limits import FILE_UPLOAD_MAX, PAGE_MAX
from .principal import Principal
from .tenancy import Tenancy

WORKSPACE = "/workspace"


def _page(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    data = rows[:limit]
    return {"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None, "has_more": len(rows) > limit}


def _limit(limit: int) -> int:
    if not 1 <= limit <= PAGE_MAX:
        raise Invalid(f"limit is 1 to {PAGE_MAX}", param="limit")
    return limit


def clean_path(path: str) -> str:
    """A workspace-relative path: `src/a.py` or `/workspace/src/a.py`; never escaping the workspace."""
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        raise Invalid("a file path inside the workspace", param="path")
    rel = path[len(WORKSPACE) + 1:] if path.startswith(WORKSPACE + "/") else path.lstrip("/")
    norm = posixpath.normpath(rel)
    if norm in (".", "") or norm.startswith("../") or norm == "..":
        raise Invalid("a file path inside the workspace", param="path")
    return norm


def _file_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "file", "bytes": row["bytes"], "created_at": int(row["created_at"].timestamp()),
            "filename": row["filename"], "purpose": row["purpose"], "status": "processed"}


class Files:
    def __init__(self, db: Database, blobs: Blobs):
        self.db, self.blobs = db, blobs

    async def upload(self, p: Principal, filename: str, purpose: str, data: IO[bytes], size: int) -> dict[str, Any]:
        if size > FILE_UPLOAD_MAX:
            raise TooLarge(f"a file is at most {FILE_UPLOAD_MAX // 2**20} MiB")
        name = posixpath.basename((filename or "").replace("\\", "/"))[:255]
        if not name or name in (".", ".."):
            raise Invalid("the upload needs a filename", param="file")
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 64:
            raise Invalid("purpose is a short string, e.g. user_data", param="purpose")
        fid = ids.new("file")
        key = org_key(p.org_id, "files", fid)
        stored = await self.blobs.put(key, data, org_id=p.org_id, kind="file")
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("INSERT INTO files (id, org_id, filename, purpose, bytes, object_key) VALUES (%s, %s, %s, %s, %s, %s)",
                         fid, p.org_id, name, purpose, stored, key)
            row = await tx.one("SELECT * FROM files WHERE id = %s", fid)
        return _file_view(row)

    async def list(self, p: Principal, purpose: str | None, limit: int, after: str | None) -> dict[str, Any]:
        limit = _limit(limit)
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all("SELECT * FROM files WHERE deleted_at IS NULL AND (%s::text IS NULL OR purpose = %s) "
                                "AND (%s::text IS NULL OR created_at < (SELECT created_at FROM files WHERE id = %s)) "
                                "ORDER BY created_at DESC LIMIT %s", purpose, purpose, after, after, limit + 1)
        return _page([_file_view(r) for r in rows], limit)

    async def _row(self, p: Principal, fid: str) -> dict[str, Any]:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT * FROM files WHERE id = %s AND deleted_at IS NULL", ids.check(fid, "file"))
        if not row:
            raise NotFound(f"no file {fid}")
        return row

    async def get(self, p: Principal, fid: str) -> dict[str, Any]:
        return _file_view(await self._row(p, fid))

    async def content(self, p: Principal, fid: str) -> tuple[dict[str, Any], Iterator[bytes]]:
        row = await self._row(p, fid)
        return _file_view(row), self.blobs.stream(row["object_key"])

    async def delete(self, p: Principal, fid: str) -> dict[str, Any]:
        row = await self._row(p, fid)
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("UPDATE files SET deleted_at = now() WHERE id = %s", fid)
        await self.blobs.delete(row["object_key"])
        return {"id": fid, "object": "file", "deleted": True}


class Containers:
    def __init__(self, db: Database, blobs: Blobs, tenancy: Tenancy):
        self.db, self.blobs, self.tenancy = db, blobs, tenancy

    def _view(self, row: dict[str, Any], limits: OrgLimits) -> dict[str, Any]:
        return {"id": row["id"], "object": "container", "name": row["install_name"],
                "created_at": int(row["created_at"].timestamp()),
                "status": "running" if row["status"] == "active" else "expired",
                "last_active_at": int(row["last_active_at"].timestamp()),
                "expires_after": {"anchor": "last_active_at", "minutes": limits.session_retention_days * 1440},
                "metadata": {"harness": row["install_name"], "turns": str(row["turns"]),
                             "store": "true" if row["store"] else "false"}}

    async def list(self, p: Principal, limit: int, after: str | None, order: str) -> dict[str, Any]:
        limit = _limit(limit)
        if order not in ("asc", "desc"):
            raise Invalid("order is asc or desc", param="order")
        limits = await self.tenancy.limits(p.org_id)
        cmp, direction = (">", "ASC") if order == "asc" else ("<", "DESC")
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all(f"SELECT * FROM sessions WHERE status <> 'deleted' AND (%s::text IS NULL OR created_at {cmp} "
                                f"(SELECT created_at FROM sessions WHERE id = %s)) ORDER BY created_at {direction} LIMIT %s",
                                after, after, limit + 1)
        return _page([self._view(r, limits) for r in rows], limit)

    async def _row(self, p: Principal, sid: str) -> dict[str, Any]:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT * FROM sessions WHERE id = %s AND status <> 'deleted'", ids.check(sid, "session"))
        if not row:
            raise NotFound(f"no container {sid}")
        return row

    async def get(self, p: Principal, sid: str) -> dict[str, Any]:
        return self._view(await self._row(p, sid), await self.tenancy.limits(p.org_id))

    async def delete(self, p: Principal, sid: str) -> dict[str, Any]:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT running_turn_id FROM sessions WHERE id = %s AND status <> 'deleted' FOR UPDATE",
                               ids.check(sid, "session"))
            if not row:
                raise NotFound(f"no container {sid}")
            if row["running_turn_id"]:
                raise Conflict(f"{sid} is running {row['running_turn_id']}; cancel it first")
            await tx.run("UPDATE sessions SET status = 'deleted', snapshot_key = NULL, manifest_key = NULL WHERE id = %s", sid)
        await self.blobs.delete_prefix(org_key(p.org_id, "sessions", sid, ""))
        return {"id": sid, "object": "container.deleted", "deleted": True}

    # files in a container ─────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def file_id(sid: str, rel: str) -> str:
        return "cfile_" + ids.sha256(f"{sid}\0{rel}".encode())[:32]

    def _file_view(self, sid: str, rel: str, size: int, created: int, source: str) -> dict[str, Any]:
        return {"id": self.file_id(sid, rel), "object": "container.file", "container_id": sid, "path": f"{WORKSPACE}/{rel}",
                "bytes": size, "created_at": created, "source": source}

    async def _files(self, p: Principal, sid: str) -> dict[str, dict[str, Any]]:
        row = await self._row(p, sid)
        files: dict[str, dict[str, Any]] = {}
        if row["manifest_key"]:
            for f in json.loads(await self.blobs.get(row["manifest_key"])):
                files[f["path"]] = self._file_view(sid, f["path"], int(f["bytes"]), int(f.get("mtime", 0)), "assistant")
        async with self.db.tenant(p.org_id) as tx:
            ops = await tx.all("SELECT op, path, bytes, created_at FROM container_ops WHERE session_id = %s "
                               "AND applied_at IS NULL ORDER BY created_at", sid)
        for op in ops:
            if op["op"] == "delete":
                files.pop(op["path"], None)
            else:
                files[op["path"]] = self._file_view(sid, op["path"], op["bytes"], int(op["created_at"].timestamp()), "user")
        return files

    async def list_files(self, p: Principal, sid: str, limit: int, after: str | None) -> dict[str, Any]:
        limit = _limit(limit)
        items = sorted((await self._files(p, sid)).values(), key=lambda f: f["path"])
        if after:
            idx = next((i for i, f in enumerate(items) if f["id"] == after), -1)
            items = items[idx + 1:]
        return _page(items, limit)

    async def _find(self, p: Principal, sid: str, fid: str) -> dict[str, Any]:
        for f in (await self._files(p, sid)).values():
            if f["id"] == fid:
                return f
        raise NotFound(f"no file {fid} in {sid}")

    async def get_file(self, p: Principal, sid: str, fid: str) -> dict[str, Any]:
        return await self._find(p, sid, fid)

    async def file_content(self, p: Principal, sid: str, fid: str) -> tuple[dict[str, Any], bytes]:
        f = await self._find(p, sid, fid)
        rel = f["path"][len(WORKSPACE) + 1:]
        async with self.db.tenant(p.org_id) as tx:
            op = await tx.one("SELECT object_key FROM container_ops WHERE session_id = %s AND path = %s AND op = 'put' "
                              "AND applied_at IS NULL ORDER BY created_at DESC LIMIT 1", sid, rel)
        if op:
            return f, await self.blobs.get(op["object_key"])
        row = await self._row(p, sid)
        return f, await self._from_snapshot(row["snapshot_key"], rel)

    async def _from_snapshot(self, key: str, rel: str) -> bytes:
        def run() -> bytes:
            with tempfile.TemporaryFile() as spool:
                for chunk in self.blobs.stream(key):
                    spool.write(chunk)
                spool.seek(0)
                with tarfile.open(fileobj=spool, mode="r:gz") as tar:
                    for m in tar:
                        if m.isfile() and posixpath.normpath(m.name.lstrip("./")) == f"workspace/{rel}":
                            src = tar.extractfile(m)
                            assert src is not None
                            return src.read()
            raise NotFound(f"no file {rel} in the workspace")
        return await asyncio.to_thread(run)

    async def put_file(self, p: Principal, sid: str, path: str, data: IO[bytes], size: int) -> dict[str, Any]:
        rel = clean_path(path)
        if size > FILE_UPLOAD_MAX:
            raise TooLarge(f"a file is at most {FILE_UPLOAD_MAX // 2**20} MiB")
        await self._row(p, sid)
        op_id = ids.new("op")
        key = org_key(p.org_id, "sessions", sid, "ops", op_id)
        stored = await self.blobs.put(key, data, org_id=p.org_id, kind="container_file")
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("INSERT INTO container_ops (id, org_id, session_id, op, path, object_key, bytes) "
                         "VALUES (%s, %s, %s, 'put', %s, %s, %s)", op_id, p.org_id, sid, rel, key, stored)
        return self._file_view(sid, rel, stored, int(time.time()), "user")

    async def delete_file(self, p: Principal, sid: str, fid: str) -> dict[str, Any]:
        f = await self._find(p, sid, fid)
        rel = f["path"][len(WORKSPACE) + 1:]
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("INSERT INTO container_ops (id, org_id, session_id, op, path) VALUES (%s, %s, %s, 'delete', %s)",
                         ids.new("op"), p.org_id, sid, rel)
        return {"id": fid, "object": "container.file.deleted", "deleted": True}


def bytes_io(data: bytes) -> IO[bytes]:
    return io.BytesIO(data)
