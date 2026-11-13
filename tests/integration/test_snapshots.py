"""Spec §12: chunks are stored once by hash, a snapshot is a recorded manifest, and unused chunks are removed."""

import hashlib

import pytest

from harnessgateway.domain.snapshot import CHUNK, Entry, SnapshotManifest
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.blobs import DirectoryBlobs
from harnessgateway.store.db import Database
from harnessgateway.store.snapshots import Snapshots
from tests.conftest import make_org

MB = 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_of(files: dict[str, bytes]) -> str:
    """A manifest of small files directly under work/."""
    return SnapshotManifest(
        Entry(path=f"work/{name}", kind="f", mode=0o644, size=len(data), mtime=1, hashes=(sha(data),) if data else ())
        for name, data in files.items()
    ).dumps()


@pytest.fixture
def snapshots(db: Database, blobs: DirectoryBlobs) -> Snapshots:
    return Snapshots(db, blobs)


async def record(snapshots: Snapshots, org: str, files: dict[str, bytes], limit: int = 100 * MB) -> str:
    for data in files.values():
        if data:
            await snapshots.put_chunk(org, sha(data), data)
    snapshot = await snapshots.record(
        org, session_id=new_id("session"), turn_id=new_id("turn"), manifest=manifest_of(files), limit_bytes=limit)
    return snapshot.id


async def refs(db: Database, org: str, data: bytes) -> int | None:
    async with db.org(org) as conn:
        row = await (await conn.execute(
            "SELECT refs FROM blobs WHERE org_id = %s AND hash = %s", [org, sha(data)])).fetchone()
    return row["refs"] if row else None


async def age(db: Database, org: str, data: bytes, seconds: int) -> None:
    async with db.org(org) as conn:
        await conn.execute(
            "UPDATE blobs SET unreferenced_at = now() - make_interval(secs => %s) WHERE org_id = %s AND hash = %s",
            [seconds, org, sha(data)])


async def test_a_chunk_is_stored_under_its_hash_and_read_back(snapshots: Snapshots, org: str) -> None:
    await snapshots.put_chunk(org, sha(b"hello"), b"hello")
    assert await snapshots.chunk(org, sha(b"hello")) == b"hello"
    assert await snapshots.missing(org, [sha(b"hello"), sha(b"other"), sha(b"hello")]) == [sha(b"other")]


async def test_storing_the_same_chunk_twice_is_fine(snapshots: Snapshots, org: str) -> None:
    await snapshots.put_chunk(org, sha(b"hello"), b"hello")
    await snapshots.put_chunk(org, sha(b"hello"), b"hello")
    assert await snapshots.chunk(org, sha(b"hello")) == b"hello"


async def test_a_chunk_that_does_not_match_its_hash_is_refused(snapshots: Snapshots, org: str) -> None:
    with pytest.raises(Invalid, match="hash"):
        await snapshots.put_chunk(org, sha(b"hello"), b"goodbye")
    assert await snapshots.missing(org, [sha(b"hello")]) == [sha(b"hello")]


@pytest.mark.parametrize("data", [b"", b"x" * (CHUNK + 1)])
async def test_an_empty_or_oversized_chunk_is_refused(snapshots: Snapshots, org: str, data: bytes) -> None:
    with pytest.raises(Invalid, match="chunk"):
        await snapshots.put_chunk(org, sha(data), data)


async def test_something_that_is_not_a_hash_is_refused(snapshots: Snapshots, org: str) -> None:
    with pytest.raises(Invalid, match="hash"):
        await snapshots.missing(org, ["../../etc/passwd"])
    with pytest.raises(NotFound):
        await snapshots.chunk(org, "../../etc/passwd")
    with pytest.raises(NotFound):
        await snapshots.chunk(org, sha(b"never stored"))


async def test_one_organization_s_chunks_are_not_another_s(snapshots: Snapshots, db: Database, org: str) -> None:
    other = await make_org(db)
    await snapshots.put_chunk(org, sha(b"hello"), b"hello")
    assert await snapshots.missing(other, [sha(b"hello")]) == [sha(b"hello")]
    with pytest.raises(NotFound):
        await snapshots.chunk(other, sha(b"hello"))


async def test_a_recorded_snapshot_has_an_id_a_size_and_a_file_count(snapshots: Snapshots, org: str) -> None:
    files = {"a.txt": b"hello", "b.txt": b"goodbye!", "empty": b""}
    for data in files.values():
        if data:
            await snapshots.put_chunk(org, sha(data), data)
    session, turn = new_id("session"), new_id("turn")
    snapshot = await snapshots.record(
        org, session_id=session, turn_id=turn, manifest=manifest_of(files), limit_bytes=100 * MB)
    assert snapshot.id.startswith("snap_")
    assert (snapshot.session_id, snapshot.turn_id, snapshot.bytes, snapshot.files) == (session, turn, 13, 3)
    assert await snapshots.get(org, snapshot.id) == snapshot
    assert (await snapshots.manifest(org, snapshot.id)).dumps() == manifest_of(files)


async def test_a_manifest_naming_a_chunk_that_is_not_stored_is_not_recorded(
    snapshots: Snapshots, db: Database, org: str
) -> None:
    await snapshots.put_chunk(org, sha(b"here"), b"here")
    with pytest.raises(Invalid, match="not stored"):
        await snapshots.record(org, session_id=new_id("session"), turn_id=new_id("turn"),
                               manifest=manifest_of({"a": b"here", "b": b"absent"}), limit_bytes=100 * MB)
    assert await refs(db, org, b"here") == 0
    async with db.org(org) as conn:
        assert await (await conn.execute("SELECT 1 FROM snapshots WHERE org_id = %s", [org])).fetchone() is None


async def test_a_manifest_that_is_not_valid_is_not_recorded(snapshots: Snapshots, org: str) -> None:
    with pytest.raises(Invalid, match="line 1"):
        await snapshots.record(org, session_id=new_id("session"), turn_id=new_id("turn"),
                               manifest="not a manifest", limit_bytes=100 * MB)


async def test_a_snapshot_over_the_disk_limit_is_never_recorded(snapshots: Snapshots, org: str) -> None:
    with pytest.raises(Invalid, match=r"2 MiB.*1 MiB"):
        await record(snapshots, org, {"big": b"x" * (2 * MB)}, limit=1 * MB)


async def test_an_unknown_snapshot_is_not_found(snapshots: Snapshots, db: Database, org: str) -> None:
    with pytest.raises(NotFound):
        await snapshots.get(org, new_id("snapshot"))
    snapshot_id = await record(snapshots, org, {"a": b"hello"})
    with pytest.raises(NotFound):
        await snapshots.manifest(await make_org(db), snapshot_id)


async def test_each_chunk_counts_the_snapshots_that_name_it(snapshots: Snapshots, db: Database, org: str) -> None:
    first = await record(snapshots, org, {"a": b"shared", "b": b"only first", "copy-of-a": b"shared"})
    second = await record(snapshots, org, {"a": b"shared"})
    assert (await refs(db, org, b"shared"), await refs(db, org, b"only first")) == (2, 1)

    await snapshots.release(org, [first])
    assert (await refs(db, org, b"shared"), await refs(db, org, b"only first")) == (1, 0)
    with pytest.raises(NotFound):
        await snapshots.get(org, first)
    assert (await snapshots.get(org, second)).id == second

    await snapshots.release(org, [first])                    # releasing what is gone is not an error
    assert await refs(db, org, b"shared") == 1


async def test_chunks_nobody_names_are_removed_after_the_grace(snapshots: Snapshots, db: Database, org: str) -> None:
    kept = await record(snapshots, org, {"a": b"kept"})
    gone = await record(snapshots, org, {"a": b"released long ago", "b": b"released just now"})
    await snapshots.put_chunk(org, sha(b"never named"), b"never named")
    await snapshots.release(org, [gone])
    await age(db, org, b"released long ago", 7200)
    await age(db, org, b"never named", 7200)

    assert await snapshots.collect(org, grace_s=3600) == 2

    assert await snapshots.missing(org, [sha(b"released long ago"), sha(b"never named")]) == [
        sha(b"released long ago"), sha(b"never named")]
    with pytest.raises(NotFound):
        await snapshots.chunk(org, sha(b"released long ago"))
    assert await snapshots.chunk(org, sha(b"released just now")) == b"released just now"
    assert await snapshots.chunk(org, sha(b"kept")) == b"kept"
    assert (await snapshots.get(org, kept)).id == kept


async def test_a_chunk_a_save_was_told_is_stored_is_not_removed_under_it(
    snapshots: Snapshots, db: Database, org: str
) -> None:
    """A save that hears "stored" does not upload; the chunk must still be there when its manifest arrives."""
    await snapshots.put_chunk(org, sha(b"old"), b"old")
    await age(db, org, b"old", 7200)
    assert await snapshots.missing(org, [sha(b"old")]) == []
    assert await snapshots.collect(org, grace_s=3600) == 0
    assert await record(snapshots, org, {"a": b"old"})
