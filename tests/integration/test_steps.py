"""Spec §12.3 to §12.6: the save, restore and watch steps, against real folders and the real store."""

import hashlib
import json
import os
import shutil
import socket
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from harnessgateway.domain.contract import SANDBOX_UID
from harnessgateway.domain.snapshot import CHUNK
from harnessgateway.ids import new_id
from harnessgateway.steps.client import InputFile, RestorePlan
from harnessgateway.steps.errors import StepError
from harnessgateway.steps.restore import restore
from harnessgateway.steps.save import save
from harnessgateway.steps.watch import watch
from harnessgateway.store.blobs import DirectoryBlobs
from harnessgateway.store.db import Database
from harnessgateway.store.snapshots import Snapshots

MB = 1024 * 1024
EMPTY = RestorePlan(manifest_key=None, warm=False, request={}, history=[], handover="", files=())


@dataclass
class LocalClient:
    """What the internal API does for a step, done in-process: the same store, no HTTP."""

    snapshots: Snapshots
    blobs: DirectoryBlobs
    org: str
    plan: RestorePlan = EMPTY
    parent: str | None = None                           # the parent snapshot's id
    limit_bytes: int = 100 * MB
    uploaded: list[str] = field(default_factory=list)
    corrupt: bool = False

    async def restore_plan(self, turn_id: str) -> RestorePlan:
        return self.plan

    async def save_plan(self, turn_id: str) -> str | None:
        return (await self.snapshots.get(self.org, self.parent)).manifest_key if self.parent else None

    async def record(self, turn_id: str, manifest: str) -> str:
        snapshot = await self.snapshots.record(
            self.org, session_id="sess_test", turn_id=turn_id, manifest=manifest, limit_bytes=self.limit_bytes)
        return snapshot.id

    def object(self, key: str) -> AsyncIterator[bytes]:
        return self.blobs.read(key)

    async def missing(self, hashes: list[str]) -> list[str]:
        return await self.snapshots.missing(self.org, hashes)

    async def chunk(self, digest: str) -> bytes:
        data = await self.snapshots.chunk(self.org, digest)
        return data[:-1] + b"!" if self.corrupt else data

    async def put_chunk(self, digest: str, data: bytes) -> None:
        self.uploaded.append(digest)
        await self.snapshots.put_chunk(self.org, digest, data)

    async def restoring(self, snapshot_id: str | None, **changes: Any) -> "LocalClient":
        key = (await self.snapshots.get(self.org, snapshot_id)).manifest_key if snapshot_id else None
        return replace(self, plan=replace(self.plan, manifest_key=key, **changes))


@pytest.fixture
def client(db: Database, blobs: DirectoryBlobs, org: str) -> LocalClient:
    return LocalClient(Snapshots(db, blobs), blobs, org)


def volumes(base: Path) -> dict[str, Path]:
    """A fresh pair of folders standing in for a session's two volumes."""
    roots = {"mind": base / "mind", "work": base / "work"}
    for root in roots.values():
        root.mkdir(parents=True)
    return roots


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    return volumes(tmp_path / "a")


def populate(roots: dict[str, Path]) -> None:
    mind, work = roots["mind"], roots["work"]
    (mind / ".claude" / "projects").mkdir(parents=True)
    (mind / ".claude" / "projects" / "session.jsonl").write_bytes(b'{"role":"user"}\n' * 50)
    (mind / ".claude" / "projects" / "session.jsonl").chmod(0o600)
    (mind / ".harnessgateway" / "claude-code").mkdir(parents=True)
    (mind / ".harnessgateway" / "claude-code" / "session").write_bytes(b"abc123")
    (work / "src").mkdir()
    (work / "src" / "main.py").write_bytes(b"print('hello')\n")
    (work / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    (work / "run.sh").chmod(0o755)
    (work / "empty.txt").write_bytes(b"")
    (work / "empty-dir").mkdir()
    (work / "private").mkdir(mode=0o700)
    (work / "private" / "note").write_bytes(b"note")
    (work / "latest").symlink_to("src/main.py")
    (work / "dangling").symlink_to("/nowhere/at/all")
    (work / "big.bin").write_bytes(os.urandom(CHUNK) + os.urandom(CHUNK) + b"tail")
    os.utime(work / "src" / "main.py", ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))


def picture(roots: dict[str, Path]) -> dict[str, tuple[Any, ...]]:
    """Everything a snapshot promises to keep, for comparing two pairs of folders."""
    seen: dict[str, tuple[Any, ...]] = {}
    for name, root in roots.items():
        for path in sorted(root.rglob("*")):
            status = path.lstat()
            key = f"{name}/{path.relative_to(root)}"
            if path.is_symlink():
                seen[key] = ("l", os.readlink(path))
            elif path.is_dir():
                seen[key] = ("d", status.st_mode & 0o7777)
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                seen[key] = ("f", status.st_mode & 0o7777, status.st_size, status.st_mtime_ns, digest)
    return seen


async def test_a_folder_is_saved_the_volumes_destroyed_and_restored_identically(
    client: LocalClient, roots: dict[str, Path], tmp_path: Path
) -> None:
    populate(roots)
    before = picture(roots)
    saved = await save(client, new_id("turn"), roots)
    assert saved.snapshot_id.startswith("snap_")
    assert saved.skipped == ()

    shutil.rmtree(tmp_path / "a")
    fresh = volumes(tmp_path / "b")
    await restore(await client.restoring(saved.snapshot_id), new_id("turn"), fresh, tmp_path / "turn")

    assert picture(fresh) == before
    assert len(before) == 16


async def test_an_unchanged_folder_uploads_nothing(client: LocalClient, roots: dict[str, Path]) -> None:
    populate(roots)
    first = await save(client, new_id("turn"), roots)
    assert first.uploaded == len(set(client.uploaded)) == 8

    client.uploaded.clear()
    client.parent = first.snapshot_id
    second = await save(client, new_id("turn"), roots)
    assert (second.uploaded, client.uploaded) == (0, [])
    assert second.snapshot_id != first.snapshot_id
    assert (await client.snapshots.manifest(client.org, second.snapshot_id)) == (
        await client.snapshots.manifest(client.org, first.snapshot_id))


async def test_an_unchanged_file_is_not_even_read(client: LocalClient, roots: dict[str, Path]) -> None:
    """Size, time and mode decide. A file rewritten in place with all three kept would keep its old hashes."""
    target = roots["work"] / "a.txt"
    target.write_bytes(b"before")
    first = await save(client, new_id("turn"), roots)
    status = target.stat()
    target.write_bytes(b"after!")
    os.utime(target, ns=(status.st_atime_ns, status.st_mtime_ns))

    client.parent = first.snapshot_id
    second = await save(client, new_id("turn"), roots)
    assert second.uploaded == 0


async def test_only_what_changed_is_uploaded(client: LocalClient, roots: dict[str, Path]) -> None:
    populate(roots)
    first = await save(client, new_id("turn"), roots)
    client.uploaded.clear()
    client.parent = first.snapshot_id

    (roots["work"] / "src" / "main.py").write_bytes(b"print('changed')\n")
    (roots["work"] / "new.txt").write_bytes(b"new")
    (roots["work"] / "copy.txt").write_bytes(b"note")                    # content the store already holds
    with (roots["work"] / "big.bin").open("r+b") as big:                 # one chunk of three changes
        big.seek(CHUNK + 10)
        big.write(b"changed")
    (roots["work"] / "run.sh").unlink()

    second = await save(client, new_id("turn"), roots)
    assert second.uploaded == 3
    manifest = await client.snapshots.manifest(client.org, second.snapshot_id)
    assert "work/run.sh" not in manifest.entries
    assert "work/copy.txt" in manifest.entries


async def test_sockets_and_pipes_are_skipped_and_named(client: LocalClient, roots: dict[str, Path]) -> None:
    os.mkfifo(roots["work"] / "pipe")
    (roots["work"] / "kept").write_bytes(b"kept")
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(roots["mind"] / "agent.sock"))
        saved = await save(client, new_id("turn"), roots)
    assert saved.skipped == ("mind/agent.sock", "work/pipe")
    manifest = await client.snapshots.manifest(client.org, saved.snapshot_id)
    assert list(manifest.entries) == ["work/kept"]


async def test_a_save_over_the_disk_limit_fails_and_records_nothing(
    client: LocalClient, roots: dict[str, Path]
) -> None:
    (roots["work"] / "big").write_bytes(b"x" * (2 * MB))
    client.limit_bytes = 1 * MB
    with pytest.raises(StepError, match="disk limit"):
        await save(client, new_id("turn"), roots)


async def test_a_restore_empties_what_the_volumes_held(client: LocalClient, roots: dict[str, Path]) -> None:
    (roots["work"] / "wanted").write_bytes(b"wanted")
    saved = await save(client, new_id("turn"), roots)
    (roots["work"] / "left-behind").mkdir()
    (roots["work"] / "left-behind" / "file").write_bytes(b"x")
    (roots["work"] / "wanted").write_bytes(b"changed since")
    (roots["mind"] / ".stale").write_bytes(b"x")
    (roots["mind"] / "link").symlink_to("/")

    await restore(await client.restoring(saved.snapshot_id), new_id("turn"), roots, roots["work"].parent / "turn")
    assert picture(roots).keys() == {"work/wanted"}
    assert (roots["work"] / "wanted").read_bytes() == b"wanted"
    assert Path("/etc").exists()


async def test_a_first_turn_starts_from_empty_folders(client: LocalClient, roots: dict[str, Path]) -> None:
    (roots["work"] / "left-behind").write_bytes(b"x")
    await restore(client, new_id("turn"), roots, roots["work"].parent / "turn")
    assert picture(roots) == {}


async def test_warm_volumes_are_left_exactly_as_they_are(client: LocalClient, roots: dict[str, Path]) -> None:
    (roots["work"] / "wanted").write_bytes(b"wanted")
    saved = await save(client, new_id("turn"), roots)
    (roots["work"] / "marker").write_bytes(b"a rebuild would remove this")
    turn = roots["work"].parent / "turn"
    await restore(await client.restoring(saved.snapshot_id, warm=True, handover="Handover from pi."),
                  new_id("turn"), roots, turn)
    assert (roots["work"] / "marker").exists()
    assert (turn / "handover.txt").read_text() == "Handover from pi."


async def test_a_chunk_that_arrives_changed_fails_the_restore(client: LocalClient, roots: dict[str, Path]) -> None:
    (roots["work"] / "a").write_bytes(b"content")
    saved = await save(client, new_id("turn"), roots)
    broken = await client.restoring(saved.snapshot_id)
    broken.corrupt = True
    with pytest.raises(StepError, match="work/a"):
        await restore(broken, new_id("turn"), roots, roots["work"].parent / "turn")


async def test_the_turn_s_inputs_are_written(client: LocalClient, roots: dict[str, Path], tmp_path: Path) -> None:
    await client.blobs.put("orgs/o/files/file_1", b"%PDF-1.7 ...")
    await client.blobs.put("orgs/o/files/file_2", b"a,b\n1,2\n")
    request = {"model": "claude-code", "input": "Summarize the report."}
    history = [{"id": "resp_1", "harness": "pi", "status": "completed", "input": "hi", "output": []}]
    plan = replace(EMPTY, request=request, history=history, handover="Handover from pi. Review /mind/.pi.", files=(
        InputFile("file_1", "report.pdf", "orgs/o/files/file_1"),
        InputFile("file_2", "data.csv", "orgs/o/files/file_2"),
    ))
    turn = tmp_path / "turn"
    await restore(replace(client, plan=plan), new_id("turn"), roots, turn)

    assert json.loads((turn / "request.json").read_text()) == request
    assert json.loads((turn / "history.json").read_text()) == history
    assert (turn / "handover.txt").read_text() == "Handover from pi. Review /mind/.pi."
    assert (turn / "files" / "file_1" / "report.pdf").read_bytes() == b"%PDF-1.7 ..."
    assert (turn / "files" / "file_2" / "data.csv").read_bytes() == b"a,b\n1,2\n"
    assert sorted(p.name for p in turn.iterdir()) == ["files", "handover.txt", "history.json", "request.json"]


async def test_with_nothing_to_hand_over_the_inputs_are_still_all_there(
    client: LocalClient, roots: dict[str, Path], tmp_path: Path
) -> None:
    turn = tmp_path / "turn"
    await restore(client, new_id("turn"), roots, turn)
    assert (turn / "handover.txt").read_text() == ""
    assert json.loads((turn / "history.json").read_text()) == []
    assert list((turn / "files").iterdir()) == []


@pytest.mark.parametrize("filename", ["../escape", "a/b", "", ".", ".."])
async def test_an_input_file_whose_name_is_not_a_plain_name_is_refused(
    client: LocalClient, roots: dict[str, Path], tmp_path: Path, filename: str
) -> None:
    await client.blobs.put("orgs/o/files/file_1", b"x")
    plan = replace(EMPTY, files=(InputFile("file_1", filename, "orgs/o/files/file_1"),))
    with pytest.raises(StepError, match="file name"):
        await restore(replace(client, plan=plan), new_id("turn"), roots, tmp_path / "turn")
    assert not (tmp_path / "escape").exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="only root can give files to another user")
async def test_everything_restored_belongs_to_the_sandbox_user(
    client: LocalClient, roots: dict[str, Path], tmp_path: Path
) -> None:
    populate(roots)
    saved = await save(client, new_id("turn"), roots)
    fresh = volumes(tmp_path / "b")
    await client.blobs.put("orgs/o/files/file_1", b"x")
    plan_client = await client.restoring(saved.snapshot_id, files=(InputFile("file_1", "f", "orgs/o/files/file_1"),))
    await restore(plan_client, new_id("turn"), fresh, tmp_path / "turn")
    for top in (*fresh.values(), tmp_path / "turn"):
        for path in (top, *top.rglob("*")):
            status = path.lstat()
            assert (status.st_uid, status.st_gid) == (SANDBOX_UID, SANDBOX_UID), path


def test_the_watcher_returns_once_the_folders_pass_the_limit(roots: dict[str, Path]) -> None:
    (roots["work"] / "a").write_bytes(b"x" * 600)

    def grow() -> None:
        time.sleep(0.15)
        (roots["mind"] / "b").write_bytes(b"x" * 600)

    writer = threading.Thread(target=grow)
    started = time.monotonic()
    writer.start()
    measured = watch(roots, limit_bytes=1000, interval_s=0.02)
    writer.join()
    assert measured == 1200
    assert 0.1 < time.monotonic() - started < 2
