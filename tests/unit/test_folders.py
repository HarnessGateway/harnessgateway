"""A snapshot's manifest, and a session's folders saved to chunks and brought back."""

import hashlib
import os
from pathlib import Path

import pytest

from harnessgateway.domain.snapshot import CHUNK, Entry, Manifest, ManifestError
from harnessgateway.driver.folders import LocalFolders

FOLDERS = {"mind": "sessions/s/mind", "work": "sessions/s/work"}


class Chunks:
    """Chunks in memory, counting what is asked of them."""

    def __init__(self) -> None:
        self.kept: dict[str, bytes] = {}
        self.put_count = 0

    async def missing(self, hashes):
        return set(hashes) - set(self.kept)

    async def put(self, digest, data):
        assert hashlib.sha256(data).hexdigest() == digest
        self.kept[digest] = data
        self.put_count += 1

    async def get(self, digest):
        return self.kept[digest]


async def test_folders_are_saved_once_restored_whole_and_never_followed_out_of(tmp_path: Path):
    local, chunks = LocalFolders(tmp_path / "folders"), Chunks()
    await local.restore(FOLDERS, Manifest(), chunks)                    # a new session: two empty folders
    work, mind = local.path(FOLDERS["work"]), local.path(FOLDERS["mind"])
    (work / "src").mkdir()
    (work / "src" / "main.py").write_text("print(1)\n")
    (work / "run.sh").write_text("#!/bin/sh\n")
    (work / "run.sh").chmod(0o755)
    (work / "big.bin").write_bytes(b"a" * CHUNK + b"b" * 10)
    (work / "empty").touch()
    (tmp_path / "outside.txt").write_text("not the agent's")
    os.symlink(tmp_path / "outside.txt", work / "link")                 # a link out of the sandbox's folders
    os.mkfifo(work / "pipe")
    (mind / ".cache").mkdir()
    (mind / ".cache" / "junk").write_text("x" * 100)
    (mind / "notes.md").write_text("remember")
    saved = await local.save(FOLDERS, Manifest(), frozenset({"mind/.cache"}), chunks)
    assert sorted(saved.entries) == ["mind/notes.md", "work/big.bin", "work/empty", "work/link", "work/run.sh", "work/src", "work/src/main.py"]
    assert saved.entries["work/link"].kind == "l" and b"not the agent's" not in b"".join(chunks.kept.values())
    assert len(saved.entries["work/big.bin"].hashes) == 2 and saved.entries["work/empty"].hashes == ()
    assert await local.measure(FOLDERS) == saved.size() + 100           # the cache counts against the disk, saved or not
    before = chunks.put_count
    again = await local.save(FOLDERS, saved, frozenset({"mind/.cache"}), chunks)
    assert again.dumps() == saved.dumps() and chunks.put_count == before        # nothing changed: nothing is read or sent
    (work / "src" / "main.py").write_text("print(2)\n")
    changed = await local.save(FOLDERS, saved, frozenset({"mind/.cache"}), chunks)
    assert chunks.put_count == before + 1 and changed.entries["work/big.bin"] == saved.entries["work/big.bin"]
    await local.restore(FOLDERS, saved, chunks)
    assert (work / "src" / "main.py").read_text() == "print(1)\n" and (work / "run.sh").stat().st_mode & 0o111
    assert (work / "big.bin").stat().st_size == CHUNK + 10 and os.readlink(work / "link") == str(tmp_path / "outside.txt")
    assert not (mind / ".cache").exists() and not (work / "pipe").exists() and (mind / "notes.md").read_text() == "remember"
    assert (work / "src" / "main.py").stat().st_mtime_ns == saved.entries["work/src/main.py"].mtime
    await local.fill("turns/t", {"request.json": b"{}", "startup.sh": b"echo\n"}, executable=["startup.sh"],
                     streams={"files/file_1/a.txt": _parts(b"up", b"loaded")})
    turn = local.path("turns/t")
    assert (turn / "files" / "file_1" / "a.txt").read_bytes() == b"uploaded" and (turn / "startup.sh").stat().st_mode & 0o100
    await local.remove("turns/t")
    assert not turn.exists()


async def _parts(*pieces: bytes):
    for piece in pieces:
        yield piece


def test_a_manifest_describes_a_tree_that_can_be_written_out():
    file, folder = Entry("work/a/b.txt", "f", mode=0o644, size=3, mtime=5, hashes=("a" * 64,)), Entry("work/a", "d", mode=0o755)
    manifest = Manifest([folder, file, Entry("mind/l", "l", target="/etc/passwd")])
    assert Manifest.loads(manifest.dumps()).dumps() == manifest.dumps() and manifest.size() == 3 and manifest.hashes() == {"a" * 64}
    assert list(manifest.files("work/")) == ["a/b.txt"] and file.unchanged(size=3, mtime=5, mode=0o644) and not file.unchanged(size=3, mtime=6, mode=0o644)
    for wrong in (lambda: Entry("etc/passwd", "f"), lambda: Entry("work/../x", "f"), lambda: Entry("work", "d"), lambda: Entry("work/a", "x"),
                  lambda: Entry("work/a", "f", size=1), lambda: Entry("work/a", "f", size=1, hashes=("zz",)), lambda: Manifest([file]),
                  lambda: Manifest([folder, folder]), lambda: Manifest.loads("not json"), lambda: Manifest.loads('{"path": "work/a"}')):
        with pytest.raises(ManifestError):
            wrong()
