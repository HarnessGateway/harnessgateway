"""Folders on a local disk: how the Docker driver keeps what its sandboxes are given, and moves a session's files
in and out of them.

The platform does this work itself, as root on its data volume. What an agent wrote is never trusted: links are
recorded and never followed, and a file is read only once its sandbox has ended.
"""

import asyncio
import hashlib
import os
import shutil
import stat
from collections.abc import AsyncIterator, Collection, Iterator, Mapping
from pathlib import Path

from harnessgateway.domain.contract import SANDBOX_UID
from harnessgateway.domain.snapshot import CHUNK, Entry, Manifest
from harnessgateway.driver.base import ChunkStore

Found = tuple[str, Path, os.stat_result]       # a path as a manifest names it, where it is, and its status


def _give(path: Path) -> None:
    """Hand a path to the sandbox's user. Only root can, which the platform is on its own volume."""
    if os.geteuid() == 0:
        os.lchown(path, SANDBOX_UID, SANDBOX_UID)


def _fresh(folder: Path) -> Path:
    """The folder, empty and the sandbox user's."""
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    _give(folder)
    return folder


def _walk(root: str, folder: Path, leave_out: frozenset[str] = frozenset()) -> Iterator[Found]:
    """Everything under a folder. A link is reported and never followed. A path in `leave_out` is passed over
    with all it holds. The tree may be changing while it is walked: what vanishes is left out."""
    pending = [(root, folder)]                     # a stack, not recursion: a tree may be deeper than Python allows
    while pending:
        prefix, inside = pending.pop()
        try:
            entries = list(os.scandir(inside))
        except FileNotFoundError:
            continue
        for entry in entries:
            path = f"{prefix}/{entry.name}"
            if path in leave_out:
                continue
            try:
                status = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            yield path, Path(entry.path), status
            if stat.S_ISDIR(status.st_mode):
                pending.append((path, Path(entry.path)))


def _hashes(location: Path) -> list[str]:
    """The hash of each chunk of a file, in order."""
    with open(os.open(location, os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
        return [hashlib.sha256(data).hexdigest() for data in iter(lambda: file.read(CHUNK), b"")]


def _chunk(location: Path, index: int) -> bytes:
    with open(os.open(location, os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
        file.seek(index * CHUNK)
        return file.read(CHUNK)


class LocalFolders:
    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, folder: str) -> Path:
        return self._root / folder

    async def fill(self, folder: str, files: Mapping[str, bytes], *, executable: Collection[str] = (),
                   streams: Mapping[str, AsyncIterator[bytes]] | None = None) -> None:
        target = _fresh(self.path(folder))
        for name in (*files, *(streams or {})):
            location = target / name
            for parent in reversed(location.relative_to(target).parents[:-1]):
                (target / parent).mkdir(exist_ok=True)
                _give(target / parent)
            if name in files:
                location.write_bytes(files[name])
            else:
                with location.open("wb") as out:
                    async for piece in (streams or {})[name]:
                        await asyncio.to_thread(out.write, piece)
            location.chmod(0o755 if name in executable else 0o644)
            _give(location)

    async def restore(self, folders: Mapping[str, str], manifest: Manifest, chunks: ChunkStore) -> None:
        roots = {root: _fresh(self.path(folder)) for root, folder in folders.items()}
        directories = []
        for path in sorted(manifest.entries):                    # a directory sorts before what it holds
            entry = manifest.entries[path]
            root, _, rest = path.partition("/")
            location = roots[root] / rest
            if entry.kind == "d":
                location.mkdir(mode=0o700)
                directories.append((location, entry.mode))
            elif entry.kind == "l":
                os.symlink(entry.target, location)
            else:
                with location.open("xb") as out:
                    for digest in entry.hashes:
                        await asyncio.to_thread(out.write, await chunks.get(digest))
                os.chmod(location, entry.mode)
                os.utime(location, ns=(entry.mtime, entry.mtime))
            _give(location)
        for location, mode in reversed(directories):             # last, so a read-only directory can be filled first
            os.chmod(location, mode)

    def _found(self, folders: Mapping[str, str], leave_out: frozenset[str] = frozenset()) -> list[Found]:
        return [found for root, folder in folders.items() for found in _walk(root, self.path(folder), leave_out)]

    async def save(self, folders: Mapping[str, str], parent: Manifest, leave_out: frozenset[str],
                   chunks: ChunkStore) -> Manifest:
        entries: list[Entry] = []
        read_from: dict[str, tuple[Path, int]] = {}    # where one copy of each chunk a changed file holds can be read
        for path, location, status in await asyncio.to_thread(self._found, folders, leave_out):
            mode = stat.S_IMODE(status.st_mode)
            if stat.S_ISDIR(status.st_mode):
                entries.append(Entry(path, "d", mode=mode))
            elif stat.S_ISLNK(status.st_mode):
                entries.append(Entry(path, "l", target=os.readlink(location)))
            elif stat.S_ISREG(status.st_mode):     # anything else (a socket, a pipe, a device) is not a file to keep
                before = parent.entries.get(path)
                if before and before.unchanged(size=status.st_size, mtime=status.st_mtime_ns, mode=mode):
                    entries.append(before)
                    continue
                hashes = await asyncio.to_thread(_hashes, location)
                for index, digest in enumerate(hashes):
                    read_from.setdefault(digest, (location, index))
                entries.append(Entry(path, "f", mode=mode, size=status.st_size, mtime=status.st_mtime_ns,
                                     hashes=tuple(hashes)))
        for digest in await chunks.missing(list(read_from)):
            await chunks.put(digest, await asyncio.to_thread(_chunk, *read_from[digest]))
        return Manifest(entries)

    async def measure(self, folders: Mapping[str, str]) -> int:
        found = await asyncio.to_thread(self._found, folders)
        return sum(status.st_size for _, _, status in found if stat.S_ISREG(status.st_mode))

    async def remove(self, folder: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self.path(folder), True)
