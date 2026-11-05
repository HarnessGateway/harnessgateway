"""Containers (`/v1/containers`): a session, its responses and its workspace. A container's files are what its last
snapshot listed; files written to a container are staged and land in the workspace before its next turn; its responses
are its turns in order, each as `GET /v1/responses/{id}` returns it (SPEC §14.1)."""
from __future__ import annotations

import asyncio
import json
import posixpath
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import ids
from ..db import Database
from ..domain import contract
from ..domain.access import Principal
from ..errors import Conflict, Invalid, NotFound, TooLarge
from ..limits import FILE_UPLOAD_MAX
from .objects import Objects, org_key
from .pages import check_limit, page, ts
from .turns import own, request_body

WORKSPACE = "/workspace"
LAST = "(SELECT t.id FROM turns t WHERE t.session_id = s.id ORDER BY t.seq DESC LIMIT 1) AS last_response_id"


def clean_path(path: object) -> str:
    """A workspace-relative path from `src/a.py` or `/workspace/src/a.py`; never one that leaves the workspace."""
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        raise Invalid("a file path inside the workspace", param="path")
    rel = path[len(WORKSPACE) + 1:] if path.startswith(WORKSPACE + "/") else path.lstrip("/")
    norm = posixpath.normpath(rel)
    if norm in (".", "", "..") or norm.startswith("../"):
        raise Invalid("a file path inside the workspace", param="path")
    return norm


def _state(row: dict[str, Any]) -> str:
    return "running" if row["running_turn_id"] else "saving" if row["saving_turn_id"] else "idle"


class Containers:
    def __init__(self, db: Database, objects: Objects):
        self.db, self.objects = db, objects

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "object": "container", "name": row["harness"], "created_at": ts(row["created_at"]),
                "status": "running", "last_active_at": ts(row["last_active_at"]),
                "metadata": {"harness": row["harness"], "project": row["project_id"], "model": row["model"],
                             "turns": str(row["turns"]), "state": _state(row), "store": "true" if row["store"] else "false",
                             "bytes": str(row["snapshot_bytes"]), "last_response_id": row.get("last_response_id") or "",
                             "created_by": row["created_by"], "user": row["end_user"] or "",
                             **({"forked_from": row["forked_from"]} if row.get("forked_from") else {})}}

    async def list(self, p: Principal, limit: int, after: str | None, order: str, user: str | None = None
                   ) -> dict[str, Any]:
        """The principal's project's sessions (an admin without a project: the organization's; a run-only key: its
        own), newest first unless `order=asc`; `user` keeps the sessions an application made for that end user."""
        p.require("responses", "read")
        limit = check_limit(limit)
        if order not in ("asc", "desc"):
            raise Invalid("order is asc or desc", param="order")
        cmp, direction = (">", "ASC") if order == "asc" else ("<", "DESC")
        clause, args = own(p)
        async with self.db.tenant(p.org_id, p.project) as tx:
            rows = await tx.all(f"SELECT s.*, {LAST} FROM sessions s WHERE s.status = 'active'{clause} AND (%s::text IS NULL "
                                f"OR s.end_user = %s) AND (%s::text IS NULL OR s.created_at {cmp} (SELECT created_at FROM "
                                f"sessions WHERE id = %s)) ORDER BY s.created_at {direction} LIMIT %s",
                                *args, user, user, after, after, limit + 1)
        return page([self._view(r) for r in rows], limit)

    async def row(self, p: Principal, sid: str, lock: bool = False) -> dict[str, Any]:
        p.require("responses", "read")
        clause, args = own(p)
        async with self.db.tenant(p.org_id, p.project) as tx:
            row = await tx.one(f"SELECT s.*, {LAST} FROM sessions s WHERE s.id = %s AND s.status = 'active'{clause}"
                               f"{' FOR UPDATE OF s' if lock else ''}", ids.check(sid, "session"), *args)
        if not row:
            raise NotFound(f"no container {sid}")
        return row

    async def get(self, p: Principal, sid: str) -> dict[str, Any]:
        return self._view(await self.row(p, sid))

    async def delete(self, p: Principal, sid: str) -> dict[str, Any]:
        p.require("responses", "delete")
        clause, args = own(p)
        async with self.db.tenant(p.org_id, p.project) as tx:
            row = await tx.one(f"SELECT running_turn_id FROM sessions s WHERE s.id = %s AND s.status = 'active'{clause} "
                               "FOR UPDATE", ids.check(sid, "session"), *args)
            if not row:
                raise NotFound(f"no container {sid}")
            if row["running_turn_id"]:
                raise Conflict(f"{sid} is running {row['running_turn_id']}; cancel it first")
            await tx.run("UPDATE sessions SET status = 'deleted', snapshot_key = NULL, files_key = NULL WHERE id = %s", sid)
        await self.objects.delete_prefix(org_key(p.org_id, "sessions", sid, ""))
        return {"id": sid, "object": "container.deleted", "deleted": True}

    # ── responses ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def list_responses(self, p: Principal, sid: str, limit: int, after: str | None) -> dict[str, Any]:
        """The session's turns in order, each as the caller reads it at GET /v1/responses/{id}: the stored Response for
        an ended turn, the partial one for a turn still queued or running."""
        limit = check_limit(limit)
        await self.row(p, sid)
        async with self.db.tenant(p.org_id, p.project) as tx:
            rows = await tx.all("SELECT * FROM turns WHERE session_id = %s AND (%s::text IS NULL OR seq > "
                                "(SELECT seq FROM turns WHERE id = %s)) ORDER BY seq LIMIT %s", sid, after, after, limit + 1)
        data = []
        for turn in rows:
            if turn["response"] is not None:
                data.append(turn["response"])
            else:
                data.append(contract.response(rid=turn["id"], body=await request_body(self.objects, turn),
                                              created=turn["created_at"].timestamp(), status=turn["status"],
                                              metadata={"session_id": sid}))
        return page(data, limit)

    # ── files ──────────────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def file_id(sid: str, rel: str) -> str:
        return "cfile_" + ids.sha256(f"{sid}\0{rel}".encode())[:32]

    def _entry(self, sid: str, rel: str, size: int, created: int, source: str) -> dict[str, Any]:
        return {"id": self.file_id(sid, rel), "object": "container.file", "container_id": sid,
                "path": f"{WORKSPACE}/{rel}", "bytes": size, "created_at": created, "source": source}

    async def _files(self, p: Principal, sid: str) -> dict[str, dict[str, Any]]:
        row = await self.row(p, sid)
        files: dict[str, dict[str, Any]] = {}
        if row["files_key"]:
            for f in json.loads(await self.objects.get(row["files_key"])):
                files[f["path"]] = self._entry(sid, f["path"], int(f["bytes"]), int(f.get("mtime", 0)), "assistant")
        async with self.db.tenant(p.org_id, p.project) as tx:
            ops = await tx.all("SELECT op, path, bytes, created_at FROM container_ops WHERE session_id = %s "
                               "AND applied_at IS NULL ORDER BY created_at", sid)
        for op in ops:
            if op["op"] == "delete":
                files.pop(op["path"], None)
            else:
                files[op["path"]] = self._entry(sid, op["path"], op["bytes"], ts(op["created_at"]) or 0, "user")
        return files

    async def list_files(self, p: Principal, sid: str, limit: int, after: str | None) -> dict[str, Any]:
        limit = check_limit(limit)
        items = sorted((await self._files(p, sid)).values(), key=lambda f: f["path"])
        if after:
            idx = next((i for i, f in enumerate(items) if f["id"] == after), -1)
            items = items[idx + 1:]
        return page(items, limit)

    async def get_file(self, p: Principal, sid: str, fid: str) -> dict[str, Any]:
        for f in (await self._files(p, sid)).values():
            if f["id"] == fid:
                return f
        raise NotFound(f"no file {fid} in {sid}")

    async def file_content(self, p: Principal, sid: str, fid: str) -> tuple[dict[str, Any], bytes]:
        f = await self.get_file(p, sid, fid)
        rel = f["path"][len(WORKSPACE) + 1:]
        async with self.db.tenant(p.org_id, p.project) as tx:
            op = await tx.one("SELECT object_key FROM container_ops WHERE session_id = %s AND path = %s AND op = 'put' "
                              "AND applied_at IS NULL ORDER BY created_at DESC LIMIT 1", sid, rel)
        if op:
            return f, await self.objects.get(op["object_key"])
        row = await self.row(p, sid)
        if not row["snapshot_key"]:
            raise NotFound(f"no file {rel} in the workspace")
        with tempfile.TemporaryFile() as spool:
            async for chunk in self.objects.stream(row["snapshot_key"]):
                spool.write(chunk)
            spool.seek(0)
            return f, await asyncio.to_thread(_member, spool, "workspace/" + rel)

    async def put_file(self, p: Principal, sid: str, path: object, src: Path) -> dict[str, Any]:
        p.require("files", "create")
        rel = clean_path(path)
        if src.stat().st_size > FILE_UPLOAD_MAX:
            raise TooLarge(f"a file is at most {FILE_UPLOAD_MAX >> 20} MiB")
        await self.row(p, sid)
        op_id = ids.new("op")
        key = org_key(p.org_id, "sessions", sid, "ops", op_id)
        size = await self.objects.put_file(key, src, org_id=p.org_id, kind="container_file")
        async with self.db.tenant(p.org_id, p.project) as tx:
            await tx.run("INSERT INTO container_ops (id, org_id, session_id, op, path, object_key, bytes) "
                         "VALUES (%s, %s, %s, 'put', %s, %s, %s)", op_id, p.org_id, sid, rel, key, size)
        return self._entry(sid, rel, size, int(time.time()), "user")

    async def delete_file(self, p: Principal, sid: str, fid: str) -> dict[str, Any]:
        p.require("files", "delete")
        f = await self.get_file(p, sid, fid)
        async with self.db.tenant(p.org_id, p.project) as tx:
            await tx.run("INSERT INTO container_ops (id, org_id, session_id, op, path) VALUES (%s, %s, %s, 'delete', %s)",
                         ids.new("op"), p.org_id, sid, f["path"][len(WORKSPACE) + 1:])
        return {"id": fid, "object": "container.file.deleted", "deleted": True}


def _member(spool: Any, name: str) -> bytes:
    with tarfile.open(fileobj=spool, mode="r:gz") as tar:
        for m in tar:
            if m.isfile() and posixpath.normpath(m.name.removeprefix("./")) == name:
                src = tar.extractfile(m)
                assert src is not None
                return src.read()
    raise NotFound(f"no file {name.removeprefix('workspace/')} in the workspace")
