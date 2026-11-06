"""Containers (SPEC §14.1): a session, its responses and its workspace. A container's files are what its current
snapshot's manifest lists; files written to a container are staged as operations the runner applies before the next
turn; its responses are its turns in order. Deleting a container ends the session and queues `delete_session`, which
`purge` carries out."""
from __future__ import annotations

import posixpath
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .. import ids
from ..contract import response
from ..domain.access import Principal
from ..errors import Conflict, Invalid, NotFound, TooLarge
from ..limits import FILE_UPLOAD_MAX
from ..store.db import Database, Tx
from ..store.moments import Moments
from ..store.objects import Objects, org_key
from .pages import check_limit, own, page, ts
from .snapshots import Snapshots
from .turns import request_body

WORKSPACE = "/workspace"
ROW = ("SELECT s.*, (SELECT t.id FROM turns t WHERE t.session_id = s.id ORDER BY t.seq DESC LIMIT 1) AS last_response_id, "
       "(SELECT t.status FROM turns t WHERE t.session_id = s.id AND t.status IN ('queued', 'in_progress') ORDER BY t.seq "
       "LIMIT 1) AS live_status, coalesce((SELECT n.bytes FROM snapshots n WHERE n.id = s.snapshot_id), 0) AS snapshot_bytes, "
       "(SELECT n.manifest_key FROM snapshots n WHERE n.id = s.snapshot_id) AS manifest_key FROM sessions s")


def clean_path(path: object) -> str:
    """A workspace-relative path from `src/a.py` or `/workspace/src/a.py`; never one that leaves the workspace."""
    if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path:
        raise Invalid("a file path inside the workspace", param="path")
    rel = path[len(WORKSPACE) + 1:] if path.startswith(WORKSPACE + "/") else path.lstrip("/")
    norm = posixpath.normpath(rel)
    if norm in (".", "", "..") or norm.startswith("../"):
        raise Invalid("a file path inside the workspace", param="path")
    return norm


def file_id(sid: str, rel: str) -> str:
    return "cfile_" + ids.sha256(f"{sid}\0{rel}".encode())[:32]


def _entry(sid: str, rel: str, size: int, created: int, source: str) -> dict[str, Any]:
    return {"id": file_id(sid, rel), "object": "container.file", "container_id": sid, "path": f"{WORKSPACE}/{rel}",
            "bytes": size, "created_at": created, "source": source}


def _view(row: dict[str, Any]) -> dict[str, Any]:
    state = {"in_progress": "running", "queued": "queued"}.get(row["live_status"] or "", "idle")
    return {"id": row["id"], "object": "container", "name": row["harness"], "created_at": ts(row["created_at"]),
            "status": "running", "last_active_at": ts(row["last_active_at"]),
            "metadata": {"harness": row["harness"], "model": row["model"], "turns": str(row["turns"]), "state": state,
                         "store": "true" if row["store"] else "false", "bytes": str(row["snapshot_bytes"]),
                         "last_response_id": row["last_response_id"] or "", "created_by": row["created_by"],
                         "user": row["end_user"] or "", **({"forked_from": row["forked_from"]} if row["forked_from"] else {})}}


class Containers:
    def __init__(self, db: Database, objects: Objects, snapshots: Snapshots, moments: Moments):
        self.db, self.objects, self.snapshots, self.moments = db, objects, snapshots, moments

    async def list(self, p: Principal, limit: int, after: str | None, order: str, user: str | None = None
                   ) -> dict[str, Any]:
        """The organization's sessions (a run-only key: its own), newest first unless `order=asc`; `user` keeps the
        sessions an application made for that end user."""
        p.require("responses", "read")
        limit = check_limit(limit)
        if order not in ("asc", "desc"):
            raise Invalid("order is asc or desc", param="order")
        cmp, direction = (">", "ASC") if order == "asc" else ("<", "DESC")
        clause, args = own(p)
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all(f"{ROW} WHERE s.status = 'active'{clause} AND (%s::text IS NULL OR s.end_user = %s) AND "
                                f"(%s::text IS NULL OR s.created_at {cmp} (SELECT created_at FROM sessions WHERE id = %s)) "
                                f"ORDER BY s.created_at {direction} LIMIT %s", *args, user, user, after, after, limit + 1)
        return page([_view(r) for r in rows], limit)

    async def _row(self, tx: Tx, p: Principal, sid: str, lock: bool = False) -> dict[str, Any]:
        p.require("responses", "read")
        clause, args = own(p)
        row = await tx.one(f"{ROW} WHERE s.id = %s AND s.status = 'active'{clause}{' FOR UPDATE OF s' if lock else ''}",
                           ids.check(sid, "session"), *args)
        if not row:
            raise NotFound(f"no container {sid}")
        return row

    async def row(self, p: Principal, sid: str) -> dict[str, Any]:
        async with self.db.tenant(p.org_id) as tx:
            return await self._row(tx, p, sid)

    async def get(self, p: Principal, sid: str) -> dict[str, Any]:
        return _view(await self.row(p, sid))

    async def delete(self, p: Principal, sid: str) -> dict[str, Any]:
        """The session ends: refused while a turn runs or waits; its storage goes with the `delete_session` job."""
        p.require("responses", "delete")
        async with self.db.tenant(p.org_id) as tx:
            row = await self._row(tx, p, sid, lock=True)
            if row["live_status"]:
                raise Conflict(f"{sid} has a turn {row['live_status']}; cancel it first")
            await tx.run("UPDATE sessions SET status = 'deleted' WHERE id = %s", sid)
        await self.moments.enqueue("delete_session", session_id=sid, org_id=p.org_id)
        return {"id": sid, "object": "container.deleted", "deleted": True}

    async def purge(self, org_id: str, sid: str) -> None:
        """What `delete_session` does: the session's snapshots released, its turns, traces and operations removed, its
        objects deleted. Safe to run again."""
        async with self.db.tenant(org_id) as tx:
            session = await tx.one("SELECT status FROM sessions WHERE id = %s", sid)
            if not session:
                return
            if session["status"] != "deleted":
                await tx.run("UPDATE sessions SET status = 'deleted' WHERE id = %s", sid)
            snapshot_ids = [r["id"] for r in await tx.all("SELECT id FROM snapshots WHERE session_id = %s", sid)]
            turn_ids = [r["id"] for r in await tx.all("SELECT id FROM turns WHERE session_id = %s", sid)]
            await tx.run("UPDATE sessions SET snapshot_id = NULL WHERE id = %s", sid)
            await tx.run("DELETE FROM traces WHERE response_id = ANY(%s)", turn_ids)
            await tx.run("DELETE FROM turns WHERE session_id = %s", sid)
            await tx.run("DELETE FROM container_ops WHERE session_id = %s", sid)
        await self.snapshots.release(org_id, snapshot_ids)
        for rid in turn_ids:
            await self.objects.delete_prefix(org_key(org_id, "turns", rid, ""))
        await self.objects.delete_prefix(org_key(org_id, "sessions", sid, ""))

    # ── responses ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def list_responses(self, p: Principal, sid: str, limit: int, after: str | None) -> dict[str, Any]:
        """The session's turns in order, each as the caller reads it at GET /v1/responses/{id}: the stored Response for
        an ended turn, the partial one for a turn still queued or running."""
        limit = check_limit(limit)
        async with self.db.tenant(p.org_id) as tx:
            await self._row(tx, p, sid)
            rows = await tx.all("SELECT * FROM turns WHERE session_id = %s AND (%s::text IS NULL OR seq > "
                                "(SELECT seq FROM turns WHERE id = %s)) ORDER BY seq LIMIT %s", sid, after, after, limit + 1)
        data = []
        for turn in rows:
            if turn["response"] is not None:
                data.append(turn["response"])
            else:
                data.append(response(rid=turn["id"], body=await request_body(self.objects, turn),
                                     created=turn["created_at"].timestamp(), status=turn["status"],
                                     metadata={"session_id": sid}))
        return page(data, limit)

    # ── files ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def _files(self, p: Principal, sid: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """The session row and its workspace files by relative path: the snapshot's, then the staged operations."""
        async with self.db.tenant(p.org_id) as tx:
            row = await self._row(tx, p, sid)
            ops = await tx.all("SELECT op, path, bytes, created_at FROM container_ops WHERE session_id = %s "
                               "AND applied_at IS NULL ORDER BY created_at", sid)
        files: dict[str, dict[str, Any]] = {}
        if row["manifest_key"]:
            for f in (await self.snapshots.read(p.org_id, row["manifest_key"])).workspace_files():
                path = str(f["path"])
                files[path] = _entry(sid, path, int(str(f["bytes"])), int(str(f["mtime"])), "assistant")
        for op in ops:
            if op["op"] == "delete":
                files.pop(op["path"], None)
            else:
                files[op["path"]] = _entry(sid, op["path"], op["bytes"], ts(op["created_at"]) or 0, "user")
        return row, files

    async def list_files(self, p: Principal, sid: str, limit: int, after: str | None) -> dict[str, Any]:
        limit = check_limit(limit)
        _, files = await self._files(p, sid)
        items = sorted(files.values(), key=lambda f: f["path"])
        if after:
            idx = next((i for i, f in enumerate(items) if f["id"] == after), -1)
            items = items[idx + 1:]
        return page(items, limit)

    async def get_file(self, p: Principal, sid: str, fid: str) -> dict[str, Any]:
        _, files = await self._files(p, sid)
        for f in files.values():
            if f["id"] == fid:
                return f
        raise NotFound(f"no file {fid} in {sid}")

    async def file_content(self, p: Principal, sid: str, fid: str) -> tuple[dict[str, Any], AsyncIterator[bytes]]:
        """The file's bytes: a staged upload's, else the snapshot's."""
        row, files = await self._files(p, sid)
        f = next((f for f in files.values() if f["id"] == fid), None)
        if f is None:
            raise NotFound(f"no file {fid} in {sid}")
        rel = f["path"][len(WORKSPACE) + 1:]
        async with self.db.tenant(p.org_id) as tx:
            op = await tx.one("SELECT object_key FROM container_ops WHERE session_id = %s AND path = %s AND op = 'put' "
                              "AND applied_at IS NULL ORDER BY created_at DESC LIMIT 1", sid, rel)
        if op:
            return f, self.objects.stream(op["object_key"])
        if not row["manifest_key"]:
            raise NotFound(f"no file {rel} in the workspace")
        return f, self.snapshots.file(p.org_id, row["manifest_key"], rel)

    async def put_file(self, p: Principal, sid: str, path: object, src: Path) -> dict[str, Any]:
        p.require("files", "create")
        rel = clean_path(path)
        if src.stat().st_size > FILE_UPLOAD_MAX:
            raise TooLarge(f"a file is at most {FILE_UPLOAD_MAX >> 20} MiB")
        await self.row(p, sid)
        op_id = ids.new("op")
        key = org_key(p.org_id, "sessions", sid, "ops", op_id)
        size = await self.objects.put_file(key, src, org_id=p.org_id, kind="container_file")
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("INSERT INTO container_ops (id, org_id, session_id, op, path, object_key, bytes) "
                         "VALUES (%s, %s, %s, 'put', %s, %s, %s)", op_id, p.org_id, sid, rel, key, size)
        return _entry(sid, rel, size, int(time.time()), "user")

    async def delete_file(self, p: Principal, sid: str, fid: str) -> dict[str, Any]:
        p.require("files", "delete")
        f = await self.get_file(p, sid, fid)
        async with self.db.tenant(p.org_id) as tx:
            await tx.run("INSERT INTO container_ops (id, org_id, session_id, op, path) VALUES (%s, %s, %s, 'delete', %s)",
                         ids.new("op"), p.org_id, sid, f["path"][len(WORKSPACE) + 1:])
        return {"id": fid, "object": "container.file.deleted", "deleted": True}
