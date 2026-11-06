"""Installs (SPEC §9.6): an organization's own run of a package's `install`, once per package digest and sandbox image,
archived in the organization's storage and never placed in an image. A turn waits while its install is pending and
fails when it failed."""
from __future__ import annotations

import logging
from typing import Any

from .. import ids
from ..store.db import Database, Tx
from ..store.moments import Moments
from ..store.objects import Objects, org_key
from .pages import ts

log = logging.getLogger("harnessgateway.installs")


def archive_key(org_id: str, digest: str, image: str) -> str:
    return org_key(org_id, "installs", f"{digest}-{ids.sha256(image.encode())[:16]}.tar.zst")


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "install", "digest": row["digest"], "status": row["status"], "bytes": row["bytes"],
            "log_tail": row["log_tail"], "created_at": ts(row["created_at"]), "updated_at": ts(row["updated_at"])}


class Installs:
    def __init__(self, db: Database, objects: Objects, moments: Moments, image: str):
        self.db, self.objects, self.moments, self.image = db, objects, moments, image

    async def ensure(self, org_id: str, digest: str) -> dict[str, Any]:
        """The install row for this package on the current sandbox image, created pending and queued when absent."""
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT * FROM installs WHERE org_id = %s AND digest = %s AND image = %s",
                               org_id, digest, self.image)
            if row is None:
                await tx.run("INSERT INTO installs (id, org_id, digest, image) VALUES (%s, %s, %s, %s)",
                             ids.new("install"), org_id, digest, self.image)
                row = await tx.get("SELECT * FROM installs WHERE org_id = %s AND digest = %s AND image = %s",
                                   org_id, digest, self.image)
                queued = True
            else:
                queued = False
        if queued:
            await self.moments.enqueue("install", install_id=row["id"], org_id=org_id)
        return _view(row)

    async def current(self, tx: Tx, org_id: str, digest: str) -> dict[str, Any] | None:
        return await tx.one("SELECT * FROM installs WHERE org_id = %s AND digest = %s AND image = %s",
                            org_id, digest, self.image)

    async def get(self, org_id: str, install_id: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            row = await tx.get("SELECT * FROM installs WHERE id = %s", ids.check(install_id, "install"))
        return _view(row)

    async def retry(self, org_id: str, digest: str) -> dict[str, Any]:
        """A failed install run again (after a fix on the registry's side, say)."""
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("UPDATE installs SET status = 'pending', log_tail = '', updated_at = now() WHERE org_id = %s "
                               "AND digest = %s AND image = %s AND status = 'failed' RETURNING *", org_id, digest, self.image)
        if row is None:
            return await self.ensure(org_id, digest)
        await self.moments.enqueue("install", install_id=row["id"], org_id=org_id)
        return _view(row)

    async def pending_without_box(self) -> list[dict[str, Any]]:
        """Installs still pending with no live box: what the reconcile duty re-queues."""
        async with self.db.platform() as tx:
            return await tx.all("SELECT i.id, i.org_id FROM installs i WHERE i.status = 'pending' AND i.created_at < now() - "
                                "interval '1 minute' AND NOT EXISTS (SELECT 1 FROM boxes b WHERE b.install_id = i.id AND "
                                "b.status <> 'gone')")

    async def finish(self, org_id: str, install_id: str, *, ok: bool, size: int, log_tail: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            row = await tx.get("SELECT * FROM installs WHERE id = %s", install_id)
            key = archive_key(org_id, row["digest"], self.image) if ok else None
            await tx.run("UPDATE installs SET status = %s, object_key = %s, bytes = %s, log_tail = %s, updated_at = now() "
                         "WHERE id = %s", "ready" if ok else "failed", key, size if ok else 0, log_tail[-16384:], install_id)
            row = await tx.get("SELECT * FROM installs WHERE id = %s", install_id)
        if ok and key:
            await self.objects.record(key, org_id, "install", size)
        log.info("install %s for %s: %s", row["digest"][:12], org_id, row["status"])
        return _view(row)
