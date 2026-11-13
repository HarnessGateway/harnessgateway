"""Spec §12.1: the core's blob store is a directory."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from harnessgateway.store.blobs import BlobMissing, DirectoryBlobs


async def parts(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_what_is_put_is_got_back(tmp_path: Path) -> None:
    blobs = DirectoryBlobs(tmp_path)
    await blobs.put("orgs/org_1/files/file_1", b"hello")
    assert await blobs.get("orgs/org_1/files/file_1") == b"hello"


async def test_a_stream_is_written_and_read_in_pieces(tmp_path: Path) -> None:
    blobs = DirectoryBlobs(tmp_path)
    big = bytes(range(256)) * 20_000                                     # about 5 MB
    assert await blobs.write("a/big", parts(big[:1000], big[1000:])) == len(big)
    pieces = [piece async for piece in blobs.read("a/big")]
    assert len(pieces) > 1
    assert b"".join(pieces) == big


async def test_putting_again_replaces(tmp_path: Path) -> None:
    blobs = DirectoryBlobs(tmp_path)
    await blobs.put("a/b", b"one")
    await blobs.put("a/b", b"two")
    assert await blobs.get("a/b") == b"two"


async def test_a_missing_key_is_reported_as_missing(tmp_path: Path) -> None:
    blobs = DirectoryBlobs(tmp_path)
    with pytest.raises(BlobMissing, match="a/none"):
        await blobs.get("a/none")


async def test_deleting_is_idempotent(tmp_path: Path) -> None:
    blobs = DirectoryBlobs(tmp_path)
    await blobs.put("a/b", b"one")
    await blobs.delete("a/b")
    await blobs.delete("a/b")
    with pytest.raises(BlobMissing):
        await blobs.get("a/b")


async def test_a_failed_write_leaves_nothing_behind(tmp_path: Path) -> None:
    blobs = DirectoryBlobs(tmp_path)

    async def broken() -> AsyncIterator[bytes]:
        yield b"half"
        raise RuntimeError("the connection dropped")

    with pytest.raises(RuntimeError):
        await blobs.write("a/b", broken())
    with pytest.raises(BlobMissing):
        await blobs.get("a/b")
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.parametrize("key", ["", "/etc/passwd", "a/../b", "a//b", "a/", "a b", "a/~b", "a\\b", "a/.", "../x"])
async def test_a_key_that_could_leave_the_directory_is_refused(tmp_path: Path, key: str) -> None:
    blobs = DirectoryBlobs(tmp_path / "store")
    with pytest.raises(ValueError, match="key"):
        await blobs.put(key, b"x")
    assert not (tmp_path / "x").exists()
