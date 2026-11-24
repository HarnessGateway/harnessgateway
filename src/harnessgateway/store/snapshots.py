"""Snapshots and the chunks they are made of.

A chunk is kept once for an organization, under its hash, and counts the snapshots that name it. A snapshot is
recorded only when every chunk it names is kept. A chunk nothing names is removed after a grace period, which
asking about the chunk renews: a save that found it already kept can rely on it until its snapshot is recorded,
whichever process is collecting.
"""

import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from harnessgateway.domain.snapshot import CHUNK, Entry, Manifest, is_hash
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import Database

GRACE_S = 3600
_UNNAMED_SINCE = "unreferenced_at = CASE WHEN {refs} = 0 THEN now() END"


@dataclass(frozen=True)
class Snapshot:
    id: str
    session_id: str
    turn_id: str
    bytes: int
    files: int


def _chunk_key(org_id: str, digest: str) -> str:
    return f"orgs/{org_id}/chunks/{digest[:2]}/{digest}"


def _manifest_key(org_id: str, snapshot_id: str) -> str:
    return f"orgs/{org_id}/snapshots/{snapshot_id}"


class Chunks:
    """One organization's chunks: what a driver saving or restoring a session's folders reads and writes."""

    def __init__(self, db: Database, blobs: BlobStore, org_id: str) -> None:
        self._db = db
        self._blobs = blobs
        self._org_id = org_id

    async def missing(self, hashes: Sequence[str]) -> set[str]:
        """Which of these chunks are not kept. One that is kept but unnamed is given a fresh grace period."""
        wanted = sorted(set(hashes))
        rows = await self._db.all(self._org_id, (
            f"UPDATE chunks SET {_UNNAMED_SINCE.format(refs='refs')} WHERE org_id = %s AND hash = ANY(%s) "
            "RETURNING hash"), [self._org_id, wanted])
        return set(wanted) - {row["hash"] for row in rows}

    async def put(self, digest: str, data: bytes) -> None:
        if not 0 < len(data) <= CHUNK or hashlib.sha256(data).hexdigest() != digest:
            raise Invalid(f"a chunk is 1 to {CHUNK} bytes and matches its hash")
        async with self._db.tx(self._org_id) as conn:       # the row first: it is held while the bytes are written,
            await conn.execute(                             # so a collection removing this chunk ends before, not after
                "INSERT INTO chunks (org_id, hash, bytes) VALUES (%s, %s, %s) ON CONFLICT (org_id, hash) DO UPDATE "
                f"SET {_UNNAMED_SINCE.format(refs='chunks.refs')}", [self._org_id, digest, len(data)])
            await self._blobs.put(_chunk_key(self._org_id, digest), data)

    async def get(self, digest: str) -> bytes:
        if not is_hash(digest):
            raise NotFound(f"no such chunk: {digest[:80]}")
        return await self._blobs.get(_chunk_key(self._org_id, digest))


class Snapshots:
    def __init__(self, db: Database, blobs: BlobStore) -> None:
        self._db = db
        self._blobs = blobs

    def chunks(self, org_id: str) -> Chunks:
        return Chunks(self._db, self._blobs, org_id)

    async def record(self, org_id: str, *, session_id: str, turn_id: str, manifest: Manifest) -> Snapshot:
        """Record a snapshot. Refused when it names a chunk that is not kept."""
        snapshot = Snapshot(new_id("snapshot"), session_id, turn_id, manifest.size(), len(manifest.files()))
        key, named = _manifest_key(org_id, snapshot.id), sorted(manifest.hashes())
        await self._blobs.put(key, manifest.dumps().encode())
        try:
            async with self._db.tx(org_id) as conn:
                counted = await conn.execute(
                    "UPDATE chunks SET refs = refs + 1, unreferenced_at = NULL WHERE org_id = %s AND hash = ANY(%s)",
                    [org_id, named])
                if counted.rowcount != len(named):
                    raise Invalid("the snapshot names a chunk that is not kept")
                await conn.execute(
                    "INSERT INTO snapshots (id, org_id, session_id, turn_id, bytes, files) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [snapshot.id, org_id, session_id, turn_id, snapshot.bytes, snapshot.files])
        except BaseException:
            await self._blobs.delete(key)
            raise
        return snapshot

    async def get(self, org_id: str, snapshot_id: str) -> Snapshot:
        row = await self._db.one(org_id, "SELECT id, session_id, turn_id, bytes, files FROM snapshots "
                                         "WHERE org_id = %s AND id = %s", [org_id, snapshot_id])
        if row is None:
            raise NotFound(f"no such snapshot: {snapshot_id}")
        return Snapshot(**row)

    async def manifest(self, org_id: str, snapshot_id: str | None) -> Manifest:
        """A snapshot's manifest; an empty one for no snapshot, which is how a new session begins."""
        if snapshot_id is None:
            return Manifest()
        return Manifest.loads((await self._blobs.get(_manifest_key(org_id, snapshot_id))).decode())

    async def content(self, org_id: str, entry: Entry) -> AsyncIterator[bytes]:
        """A file of a snapshot, chunk by chunk."""
        for digest in entry.hashes:
            yield await self._blobs.get(_chunk_key(org_id, digest))

    async def text(self, org_id: str, entry: Entry) -> str:
        """A small file of a snapshot, as text."""
        return b"".join([piece async for piece in self.content(org_id, entry)]).decode(errors="replace")

    async def release(self, org_id: str, snapshot_ids: Sequence[str]) -> None:
        """Forget snapshots. Each chunk they named is named once less; one nothing names starts its grace period."""
        for snapshot_id in snapshot_ids:
            key = _manifest_key(org_id, snapshot_id)
            named = sorted((await self.manifest(org_id, snapshot_id)).hashes())
            async with self._db.tx(org_id) as conn:
                gone = await conn.execute("DELETE FROM snapshots WHERE org_id = %s AND id = %s", [org_id, snapshot_id])
                if gone.rowcount:
                    await conn.execute(
                        f"UPDATE chunks SET refs = refs - 1, {_UNNAMED_SINCE.format(refs='refs - 1')} "
                        "WHERE org_id = %s AND hash = ANY(%s)", [org_id, named])
            await self._blobs.delete(key)

    async def collect(self, *, grace_s: float = GRACE_S) -> int:
        """Remove the chunks nothing has named for longer than the grace period. How many. Each chunk's row is
        held until its bytes are gone, so one being kept again at that moment is kept after, and stays."""
        removed = 0
        while True:
            async with self._db.tx() as conn:
                rows = await (await conn.execute(
                    "SELECT org_id, hash FROM chunks WHERE refs = 0 AND unreferenced_at < now() - "
                    "make_interval(secs => %s) LIMIT 1000 FOR UPDATE SKIP LOCKED", [grace_s])).fetchall()
                for row in rows:
                    await self._blobs.delete(_chunk_key(row["org_id"], row["hash"]))
                    await conn.execute("DELETE FROM chunks WHERE org_id = %s AND hash = %s",
                                       [row["org_id"], row["hash"]])
            removed += len(rows)
            if not rows:
                return removed
