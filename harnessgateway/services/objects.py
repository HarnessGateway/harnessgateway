"""Stored objects: the BlobStore port plus the `objects` ledger, which records each object's owner and size so storage
is metered without listing the store (SPEC §12)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from ..db import Database
from ..ports.blob import BlobStore

LINK_TTL_S = 3600


def org_key(org_id: str, *parts: str) -> str:
    """`orgs/<org>/<parts...>`; a trailing "" makes a prefix."""
    assert org_id.startswith("org_") and all(p and "/" not in p and p not in (".", "..") for p in parts[:-1])
    return "/".join(("orgs", org_id, *parts))


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


class Objects:
    def __init__(self, db: Database, store: BlobStore):
        self.db, self.store = db, store

    async def record(self, key: str, org_id: str | None, kind: str, size: int) -> None:
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO objects (key, org_id, kind, bytes) VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO "
                         "UPDATE SET bytes = EXCLUDED.bytes, org_id = EXCLUDED.org_id, deleted_at = NULL",
                         key, org_id, kind, size)

    async def put(self, key: str, data: bytes, *, org_id: str | None, kind: str) -> int:
        await self.store.put(key, data)
        await self.record(key, org_id, kind, len(data))
        return len(data)

    async def put_file(self, key: str, path: Path, *, org_id: str | None, kind: str) -> int:
        await self.store.put_file(key, path)
        size = path.stat().st_size
        await self.record(key, org_id, kind, size)
        return size

    async def get(self, key: str) -> bytes:
        return await self.store.get(key)

    def stream(self, key: str) -> AsyncIterator[bytes]:
        return self.store.stream(key)

    async def delete(self, key: str) -> None:
        await self.store.delete(key)
        async with self.db.platform() as tx:
            await tx.run("UPDATE objects SET deleted_at = now() WHERE key = %s AND deleted_at IS NULL", key)

    async def delete_prefix(self, prefix: str) -> None:
        assert prefix.startswith("orgs/org_") and prefix.endswith("/"), "only a subtree of one organization"
        await self.store.delete_prefix(prefix)
        async with self.db.platform() as tx:
            await tx.run("UPDATE objects SET deleted_at = now() WHERE key LIKE %s AND deleted_at IS NULL", _like(prefix))

    def link_get(self, key: str, ttl_s: int = LINK_TTL_S) -> str:
        return self.store.link_get(key, ttl_s)

    def link_put(self, key: str, ttl_s: int = LINK_TTL_S) -> str:
        return self.store.link_put(key, ttl_s)
