"""Snapshots (SPEC §9.5): content-addressed manifests and the blobs they name, reference-counted per organization.

The runner builds manifests and uploads blobs; this service records them, answers which blobs the store lacks, reads
files out of a manifest for the Containers API, makes a fork's or a switch's first snapshot without copying bytes,
and releases what a deleted session referenced."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from hgsnapshot import Manifest, ManifestError

from .. import ids
from ..errors import Invalid, NotFound
from ..store.db import Database, Tx
from ..store.objects import Objects, blob_key, org_key

log = logging.getLogger("harnessgateway.snapshots")


def manifest_key(org_id: str, sid: str, snapshot_id: str) -> str:
    return org_key(org_id, "sessions", sid, "snapshots", f"{snapshot_id}.jsonl")


class Snapshots:
    def __init__(self, db: Database, objects: Objects):
        self.db, self.objects = db, objects

    # ── blobs ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def missing(self, org_id: str, hashes: list[str]) -> list[str]:
        """Of the hashes given, those the organization's store does not hold."""
        async with self.db.tenant(org_id) as tx:
            have = {r["hash"] for r in await tx.all("SELECT hash FROM blobs WHERE org_id = %s AND hash = ANY(%s)",
                                                    org_id, hashes)}
        return [h for h in hashes if h not in have]

    async def record_blob(self, org_id: str, digest: str, size: int) -> None:
        """A blob the store now holds, with no references yet (its manifest follows)."""
        async with self.db.tenant(org_id) as tx:
            await tx.run("INSERT INTO blobs (org_id, hash, bytes) VALUES (%s, %s, %s) ON CONFLICT (org_id, hash) DO UPDATE "
                         "SET bytes = EXCLUDED.bytes, updated_at = now()", org_id, digest, size)

    def blob_stream(self, org_id: str, digest: str) -> AsyncIterator[bytes]:
        return self.objects.stream(blob_key(org_id, digest))

    # ── manifests ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def read(self, org_id: str, key: str) -> Manifest:
        try:
            return Manifest.loads((await self.objects.get(key)).decode("utf-8"))
        except KeyError:
            raise NotFound("the snapshot is gone") from None

    async def create(self, tx: Tx, org_id: str, sid: str, turn_id: str | None, manifest: Manifest) -> dict[str, Any]:
        """A snapshot row for a manifest whose blobs the organization holds; its hashes referenced. The manifest object
        must already be stored under the key this returns, or be stored right after (the caller's transaction)."""
        hashes = sorted(manifest.hashes())
        if hashes:
            held = await tx.value("SELECT count(*) FROM blobs WHERE org_id = %s AND hash = ANY(%s)", org_id, hashes)
            if held != len(hashes):
                raise Invalid(f"the manifest names {len(hashes) - held} blobs the store does not hold", param="manifest")
            await tx.run("UPDATE blobs SET refs = refs + 1, updated_at = now() WHERE org_id = %s AND hash = ANY(%s)",
                         org_id, hashes)
        snapshot_id = ids.new("snapshot")
        key = manifest_key(org_id, sid, snapshot_id)
        await tx.run("INSERT INTO snapshots (id, org_id, session_id, turn_id, manifest_key, bytes, files) VALUES "
                     "(%s, %s, %s, %s, %s, %s, %s)", snapshot_id, org_id, sid, turn_id, key, manifest.size(), manifest.files())
        return {"id": snapshot_id, "key": key, "bytes": manifest.size(), "files": manifest.files()}

    async def store_manifest(self, org_id: str, key: str, manifest: Manifest) -> None:
        await self.objects.put(key, manifest.dumps().encode(), org_id=org_id, kind="manifest")

    async def derive(self, tx: Tx, org_id: str, source_id: str, new_sid: str, root: str | None) -> dict[str, Any]:
        """A snapshot for a new session from an existing one, whole (a fork) or one root of it (a switch carries the
        workspace). No bytes copy: the blobs are shared and their references go up."""
        source = await tx.get("SELECT manifest_key FROM snapshots WHERE id = %s", source_id)
        manifest = await self.read(org_id, source["manifest_key"])
        if root is not None:
            manifest = manifest.under(root)
        made = await self.create(tx, org_id, new_sid, None, manifest)
        await self.store_manifest(org_id, made["key"], manifest)
        return made

    async def release(self, org_id: str, snapshot_ids: list[str]) -> None:
        """The snapshots' references let go, rows removed, manifests deleted; blobs at zero wait for `gc`."""
        for snapshot_id in snapshot_ids:
            async with self.db.tenant(org_id) as tx:
                row = await tx.one("SELECT manifest_key FROM snapshots WHERE id = %s", snapshot_id)
                if not row:
                    continue
                try:
                    hashes = sorted((await self.read(org_id, row["manifest_key"])).hashes())
                except (NotFound, ManifestError):
                    hashes = []
                    log.warning("snapshot %s of %s: manifest unreadable; its blobs stay referenced", snapshot_id, org_id)
                if hashes:
                    await tx.run("UPDATE blobs SET refs = greatest(refs - 1, 0), updated_at = now() WHERE org_id = %s AND "
                                 "hash = ANY(%s)", org_id, hashes)
                await tx.run("DELETE FROM snapshots WHERE id = %s", snapshot_id)
            await self.objects.delete(row["manifest_key"])

    async def collect(self, older_than_s: int = 3600, limit: int = 1000) -> int:
        """Blobs with no references for a while: deleted from the store and the table. How many."""
        async with self.db.platform() as tx:
            rows = await tx.all("SELECT org_id, hash FROM blobs WHERE refs = 0 AND updated_at < now() - %s * interval "
                                "'1 second' LIMIT %s", older_than_s, limit)
        for r in rows:
            await self.objects.store.delete(blob_key(r["org_id"], r["hash"]))
            async with self.db.platform() as tx:
                await tx.run("DELETE FROM blobs WHERE org_id = %s AND hash = %s AND refs = 0", r["org_id"], r["hash"])
        return len(rows)

    # ── reading files out of a snapshot ───────────────────────────────────────────────────────────────────────────
    async def file(self, org_id: str, key: str, path: str) -> AsyncIterator[bytes]:
        """A workspace file's bytes from the manifest at `key`, chunk by chunk."""
        manifest = await self.read(org_id, key)
        entry = manifest.entries.get(f"workspace/{path}")
        if entry is None or entry.kind != "f":
            raise NotFound(f"no file {path} in the workspace")
        for h in entry.hashes:
            async for chunk in self.blob_stream(org_id, h):
                yield chunk
