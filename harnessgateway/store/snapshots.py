"""Snapshots and the chunks they are made of (spec §12).

A chunk is stored once per organization, under its hash, and counts the snapshots that name it. A snapshot is
a manifest, recorded only when every chunk it names is stored.
"""

import hashlib
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from itertools import batched

from harnessgateway.domain.snapshot import CHUNK, Entry, ManifestError, SnapshotManifest, is_hash
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.blobs import BlobMissing, BlobStore
from harnessgateway.store.db import Connection, Database

_BATCH = 10_000
_MIB = 1024 * 1024
# Held shared while a chunk is stored and exclusively while removed chunks' contents are deleted, so that a
# chunk being stored again is never deleted under its new row.
_CHUNK_LOCK = 0x6867_6368_756E


@dataclass(frozen=True)
class Snapshot:
    id: str
    session_id: str
    turn_id: str
    manifest_key: str
    bytes: int
    files: int


def chunk_key(org_id: str, digest: str) -> str:
    return f"orgs/{org_id}/chunks/{digest[:2]}/{digest}"


def manifest_key(org_id: str, snapshot_id: str) -> str:
    return f"orgs/{org_id}/snapshots/{snapshot_id}"


def _check_hashes(hashes: Iterable[str]) -> list[str]:
    distinct = list(dict.fromkeys(hashes))
    if not all(is_hash(h) for h in distinct):
        raise Invalid("a hash is 64 hexadecimal characters")
    return distinct


class Snapshots:
    def __init__(self, db: Database, blobs: BlobStore) -> None:
        self._db = db
        self._blobs = blobs

    async def missing(self, org_id: str, hashes: Sequence[str]) -> list[str]:
        """Which of these chunks are not stored. One that is stored but unnamed gets a fresh grace period."""
        wanted = _check_hashes(hashes)
        stored: set[str] = set()
        async with self._db.org(org_id) as conn:
            for batch in batched(wanted, _BATCH):
                rows = await (await conn.execute(
                    "UPDATE blobs SET unreferenced_at = CASE WHEN refs = 0 THEN now() END "
                    "WHERE org_id = %s AND hash = ANY(%s) RETURNING hash", [org_id, list(batch)])).fetchall()
                stored.update(row["hash"] for row in rows)
        return [h for h in wanted if h not in stored]

    async def put_chunk(self, org_id: str, digest: str, data: bytes) -> None:
        if not 0 < len(data) <= CHUNK:
            raise Invalid(f"a chunk is 1 to {CHUNK} bytes, not {len(data)}")
        if hashlib.sha256(data).hexdigest() != digest:
            raise Invalid("the chunk does not match its hash")
        async with self._db.org(org_id) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", [_CHUNK_LOCK])
            await self._blobs.put(chunk_key(org_id, digest), data)
            await conn.execute(
                "INSERT INTO blobs (org_id, hash, bytes) VALUES (%s, %s, %s) ON CONFLICT (org_id, hash) DO UPDATE "
                "SET unreferenced_at = CASE WHEN blobs.refs = 0 THEN now() END", [org_id, digest, len(data)])

    async def chunk(self, org_id: str, digest: str) -> bytes:
        if not is_hash(digest):
            raise NotFound("no such chunk")
        try:
            return await self._blobs.get(chunk_key(org_id, digest))
        except BlobMissing:
            raise NotFound(f"no such chunk: {digest}") from None

    async def record(
        self, org_id: str, *, session_id: str, turn_id: str, manifest: str, limit_bytes: int
    ) -> Snapshot:
        """Record a snapshot. Refused when the manifest is invalid, too large, or names a chunk not stored."""
        try:
            parsed = SnapshotManifest.loads(manifest)
        except ManifestError as e:
            raise Invalid(f"the manifest is not valid: {e}") from None
        if parsed.size() > limit_bytes:
            raise Invalid(f"the snapshot is {parsed.size() // _MIB} MiB; "
                          f"the environment's disk limit is {limit_bytes // _MIB} MiB")
        snapshot_id = new_id("snapshot")
        snapshot = Snapshot(id=snapshot_id, session_id=session_id, turn_id=turn_id,
                            manifest_key=manifest_key(org_id, snapshot_id), bytes=parsed.size(), files=parsed.files())
        await self._blobs.put(snapshot.manifest_key, manifest.encode())
        try:
            async with self._db.org(org_id) as conn:
                if await self._count(conn, org_id, parsed.hashes(), +1) != len(parsed.hashes()):
                    raise Invalid("the manifest names a chunk that is not stored")
                await conn.execute(
                    "INSERT INTO snapshots (id, org_id, session_id, turn_id, manifest_key, bytes, files) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [snapshot.id, org_id, session_id, turn_id, snapshot.manifest_key, snapshot.bytes, snapshot.files])
        except BaseException:
            await self._blobs.delete(snapshot.manifest_key)
            raise
        return snapshot

    async def get(self, org_id: str, snapshot_id: str) -> Snapshot:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT id, session_id, turn_id, manifest_key, bytes, files FROM snapshots "
                "WHERE org_id = %s AND id = %s", [org_id, snapshot_id])).fetchone()
        if row is None:
            raise NotFound(f"no such snapshot: {snapshot_id}")
        return Snapshot(**row)

    async def of_turn(self, org_id: str, turn_id: str) -> Snapshot | None:
        """The snapshot a turn's save recorded, if it recorded one."""
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT id, session_id, turn_id, manifest_key, bytes, files FROM snapshots "
                "WHERE org_id = %s AND turn_id = %s ORDER BY created_at DESC LIMIT 1", [org_id, turn_id])).fetchone()
        return Snapshot(**row) if row else None

    async def manifest(self, org_id: str, snapshot_id: str) -> SnapshotManifest:
        snapshot = await self.get(org_id, snapshot_id)
        return SnapshotManifest.loads((await self._blobs.get(snapshot.manifest_key)).decode())

    async def content(self, org_id: str, entry: Entry) -> AsyncIterator[bytes]:
        """A file of a snapshot, chunk by chunk."""
        for digest in entry.hashes:
            yield await self.chunk(org_id, digest)

    async def release(self, org_id: str, snapshot_ids: Sequence[str]) -> None:
        """Forget snapshots. Their chunks lose one reference each; one nobody names starts its grace period."""
        for snapshot_id in snapshot_ids:
            async with self._db.org(org_id) as conn:
                row = await (await conn.execute(
                    "DELETE FROM snapshots WHERE org_id = %s AND id = %s RETURNING manifest_key",
                    [org_id, snapshot_id])).fetchone()
                if row is None:
                    continue
                parsed = SnapshotManifest.loads((await self._blobs.get(row["manifest_key"])).decode())
                await self._count(conn, org_id, parsed.hashes(), -1)
            await self._blobs.delete(row["manifest_key"])

    async def collect(self, org_id: str, *, grace_s: int = 3600) -> int:
        """Remove the chunks nobody has named for longer than the grace period. Returns how many."""
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                "DELETE FROM blobs WHERE org_id = %s AND refs = 0 "
                "AND unreferenced_at < now() - make_interval(secs => %s) RETURNING hash", [org_id, grace_s])).fetchall()
        removed = [row["hash"] for row in rows]
        async with self._db.org(org_id) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", [_CHUNK_LOCK])
            for batch in batched(removed, _BATCH):
                back = await (await conn.execute(
                    "SELECT hash FROM blobs WHERE org_id = %s AND hash = ANY(%s)", [org_id, list(batch)])).fetchall()
                for digest in set(batch) - {row["hash"] for row in back}:
                    await self._blobs.delete(chunk_key(org_id, digest))
        return len(removed)

    @staticmethod
    async def _count(conn: Connection, org_id: str, hashes: Iterable[str], by: int) -> int:
        """Add to the reference count of each chunk, in a fixed order so two writers cannot deadlock."""
        changed = 0
        for batch in batched(sorted(hashes), _BATCH):
            await conn.execute(
                "SELECT 1 FROM blobs WHERE org_id = %s AND hash = ANY(%s) ORDER BY hash FOR UPDATE",
                [org_id, list(batch)])
            cursor = await conn.execute(
                "UPDATE blobs SET refs = refs + %s, unreferenced_at = CASE WHEN refs + %s = 0 THEN now() END "
                "WHERE org_id = %s AND hash = ANY(%s)", [by, by, org_id, list(batch)])
            changed += cursor.rowcount
        return changed
