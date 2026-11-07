"""Content-addressed, incremental snapshots of a session (SPEC §9.5). Standard library only: the runner in the sandbox
and the api share this one implementation.

A snapshot is a *manifest*: one line per path under the included roots, with the entry's type, mode, mtime, size and
the SHA-256 of its content (files over CHUNK bytes are split into CHUNK-sized pieces, each hashed). Content lives once
per organization as *blobs* keyed by hash. Walking a tree against the previous manifest hashes only what changed by
type, size or mtime, so a turn that touched three files hashes and uploads three files.

    manifest = walk(root, included_roots(persist), previous)   # the tree as it is now
    manifest.hashes() - previous.hashes()                        # what may be new to the store
    manifest.dumps()                                             # the lines to upload
    restore(root, Manifest.loads(text), fetch)                   # the tree from a manifest and a blob reader
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

CHUNK = 4 * 1024 * 1024
WORKSPACE, STATE, HOME = "workspace", "state", "home/hg"
ROOTS = (WORKSPACE, STATE, HOME)
WHOLE_HOME = "."
KINDS = ("f", "d", "l")
ENTRY_MAX = 1_000_000          # entries in one manifest
PATH_MAX = 4096


class ManifestError(ValueError):
    """A manifest line the format refuses."""


@dataclass(frozen=True)
class Entry:
    path: str                  # relative to the box root, posix, normalized: workspace/..., state/..., home/hg/...
    kind: str                  # f | d | l
    mode: int = 0o644          # permission bits
    mtime: int = 0             # nanoseconds since the epoch (files only)
    size: int = 0              # bytes (files only)
    hashes: tuple[str, ...] = ()   # the content, in CHUNK pieces (files only)
    target: str = ""           # a symlink's target (links only)

    def to_line(self) -> str:
        doc: dict[str, object] = {"p": self.path, "t": self.kind, "m": self.mode}
        if self.kind == "f":
            doc.update(mt=self.mtime, s=self.size, h=list(self.hashes))
        elif self.kind == "l":
            doc["target"] = self.target
        return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_line(cls, line: str) -> Entry:
        try:
            doc = json.loads(line)
        except ValueError:
            raise ManifestError(f"not JSON: {line[:80]!r}") from None
        if not isinstance(doc, dict):
            raise ManifestError("an entry is an object")
        path, kind = doc.get("p"), doc.get("t")
        if kind not in KINDS:
            raise ManifestError(f"entry type is one of {', '.join(KINDS)}: {kind!r}")
        path = check_path(path)
        mode = doc.get("m", 0o644)
        if not isinstance(mode, int) or not 0 <= mode <= 0o7777:
            raise ManifestError(f"{path}: mode is permission bits")
        if kind == "f":
            mtime, size, hashes = doc.get("mt", 0), doc.get("s", 0), doc.get("h", [])
            if not isinstance(mtime, int) or not isinstance(size, int) or size < 0 or not isinstance(hashes, list):
                raise ManifestError(f"{path}: a file has mt, s and h")
            if any(not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h) for h in hashes):
                raise ManifestError(f"{path}: a hash is 64 hex characters")
            expected = (size + CHUNK - 1) // CHUNK if size else 0
            if len(hashes) != expected:
                raise ManifestError(f"{path}: {size} bytes is {expected} chunks, not {len(hashes)}")
            return cls(path, kind, mode, mtime, size, tuple(hashes))
        if kind == "l":
            target = doc.get("target")
            if not isinstance(target, str) or not target or len(target) > PATH_MAX:
                raise ManifestError(f"{path}: a link has a target")
            return cls(path, kind, mode, target=target)
        return cls(path, kind, mode)


def check_path(path: object) -> str:
    """A manifest path: relative, normalized, under one of the roots, no NUL."""
    if not isinstance(path, str) or not path or len(path) > PATH_MAX or "\0" in path or path.startswith("/"):
        raise ManifestError(f"a manifest path is relative: {path!r}")
    if posixpath.normpath(path) != path or path.split("/")[0] not in ("workspace", "state", "home"):
        raise ManifestError(f"a manifest path is normalized and under workspace/, state/ or home/: {path!r}")
    if path.startswith("home") and path != "home" and not path.startswith(HOME):
        raise ManifestError(f"home paths are under home/hg: {path!r}")
    return path


class Manifest:
    def __init__(self, entries: Iterable[Entry] = ()):
        self.entries: dict[str, Entry] = {}
        for e in entries:
            self.entries[e.path] = e

    def __len__(self) -> int:
        return len(self.entries)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Manifest) and self.entries == other.entries

    def dumps(self) -> str:
        return "".join(self.entries[p].to_line() + "\n" for p in sorted(self.entries))

    @classmethod
    def loads(cls, text: str) -> Manifest:
        m = cls()
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            if n > ENTRY_MAX:
                raise ManifestError(f"a manifest holds at most {ENTRY_MAX} entries")
            e = Entry.from_line(line)
            if e.path in m.entries:
                raise ManifestError(f"{e.path}: listed twice")
            m.entries[e.path] = e
        return m

    def hashes(self) -> set[str]:
        return {h for e in self.entries.values() for h in e.hashes}

    def size(self) -> int:
        """Logical bytes: every file's size, once per path."""
        return sum(e.size for e in self.entries.values())

    def files(self) -> int:
        return sum(1 for e in self.entries.values() if e.kind == "f")

    def under(self, root: str) -> Manifest:
        """The entries under one root (`workspace`, `state`, `home/hg`): what a harness switch carries."""
        prefix = root.rstrip("/") + "/"
        return Manifest(e for p, e in self.entries.items() if p == root or p.startswith(prefix))

    def workspace_files(self) -> list[dict[str, object]]:
        """The workspace's regular files as the Containers API lists them: {path (workspace-relative), bytes, mtime}."""
        prefix = WORKSPACE + "/"
        return [{"path": p[len(prefix):], "bytes": e.size, "mtime": e.mtime // 1_000_000_000}
                for p, e in sorted(self.entries.items()) if e.kind == "f" and p.startswith(prefix)]


def included_roots(persist: Iterable[str]) -> list[str]:
    """The roots a session keeps, relative to the box root, from a manifest's persist list."""
    roots = [WORKSPACE, STATE]
    for p in persist:
        roots.append(HOME if p == WHOLE_HOME else f"{HOME}/{p}")
    return roots


def hash_file(path: Path) -> tuple[int, tuple[str, ...]]:
    """A file's size and the hashes of its CHUNK-sized pieces."""
    hashes = []
    size = 0
    with path.open("rb") as f:
        while piece := f.read(CHUNK):
            size += len(piece)
            hashes.append(hashlib.sha256(piece).hexdigest())
    return size, tuple(hashes)


def walk(root: Path, roots: Iterable[str], previous: Manifest | None = None) -> Manifest:
    """The tree under `root` at the given relative roots, as a manifest. An entry of the previous manifest with the
    same path, type, mode, size and mtime keeps its hashes unhashed."""
    prev = previous.entries if previous else {}
    out = Manifest()
    for rel in roots:
        top = root / rel
        if not top.exists() and not top.is_symlink():
            continue
        for path in sorted(_tree(top)):
            relp = path.relative_to(root).as_posix()
            st = path.lstat()
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                out.entries[relp] = Entry(relp, "l", mode, target=os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                out.entries[relp] = Entry(relp, "d", mode)
            elif stat.S_ISREG(st.st_mode):
                was = prev.get(relp)
                if was and was.kind == "f" and was.size == st.st_size and was.mtime == st.st_mtime_ns and was.mode == mode:
                    out.entries[relp] = was
                else:
                    size, hashes = hash_file(path)
                    out.entries[relp] = Entry(relp, "f", mode, st.st_mtime_ns, size, hashes)
            # sockets, fifos, devices: not persisted
    return out


def _tree(top: Path) -> Iterable[Path]:
    yield top
    if top.is_dir() and not top.is_symlink():
        for child in top.iterdir():
            yield from _tree(child)


def restore(root: Path, manifest: Manifest, fetch: Callable[[str], bytes]) -> int:
    """Lay the manifest down under `root`: directories first, then files from their blobs, then links; modes and
    mtimes set. Returns the bytes written. Nothing outside `root` is ever touched (the paths were checked on load)."""
    written = 0
    entries = [manifest.entries[p] for p in sorted(manifest.entries)]
    for e in entries:
        if e.kind == "d":
            (root / e.path).mkdir(parents=True, exist_ok=True)
    for e in entries:
        dest = root / e.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if e.kind == "f":
            if dest.is_symlink() or dest.is_dir():
                raise ManifestError(f"{e.path}: a file where the tree has something else")
            with dest.open("wb") as f:
                for h in e.hashes:
                    piece = fetch(h)
                    if hashlib.sha256(piece).hexdigest() != h:
                        raise ManifestError(f"{e.path}: blob {h[:12]} does not match its hash")
                    f.write(piece)
                    written += len(piece)
            os.chmod(dest, e.mode)
            os.utime(dest, ns=(e.mtime, e.mtime))
        elif e.kind == "l":
            if dest.is_symlink() or dest.exists():
                dest.unlink() if not dest.is_dir() or dest.is_symlink() else None
            os.symlink(e.target, dest)
    for e in entries:
        if e.kind == "d":
            os.chmod(root / e.path, e.mode)
    return written
