"""The snapshot manifest: one line per path of a session's mind and work (spec §12.2)."""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

CHUNK = 4 * 1024 * 1024
ENTRIES_MAX = 1_000_000
PATH_MAX = 4096
ROOTS = ("mind", "work")

_HASH = re.compile(r"[0-9a-f]{64}")
_KEYS = {"f": {"p", "t", "m", "s", "mt", "h"}, "d": {"p", "t", "m"}, "l": {"p", "t", "to"}}


class ManifestError(ValueError):
    """A manifest, or one of its entries, is not what the specification allows."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def check_path(path: object) -> str:
    if not isinstance(path, str) or not 0 < len(path) <= PATH_MAX or "\x00" in path:
        raise ManifestError(f"a path is 1 to {PATH_MAX} characters: {str(path)[:80]!r}")
    root, *rest = path.split("/")
    if root not in ROOTS or not rest or any(part in ("", ".", "..") for part in rest):
        raise ManifestError(f"a path is under mind/ or work/, with no empty part and no '..': {path[:80]!r}")
    return path


@dataclass(frozen=True)
class Entry:
    path: str
    kind: Literal["f", "d", "l"]
    mode: int = 0                              # permission bits (files and directories)
    size: int = 0                              # bytes (files)
    mtime: int = 0                             # nanoseconds since the epoch (files)
    hashes: tuple[str, ...] = ()               # the SHA-256 of each chunk, in order (files)
    target: str = ""                           # as written (links)

    def __post_init__(self) -> None:
        check_path(self.path)
        if self.kind not in _KEYS:
            raise ManifestError(f"{self.path}: an entry is a file (f), a directory (d) or a link (l)")
        if not _is_int(self.mode) or not 0 <= self.mode <= 0o7777:
            raise ManifestError(f"{self.path}: the mode is permission bits")
        if self.kind == "f":
            if not _is_int(self.size) or self.size < 0 or not _is_int(self.mtime):
                raise ManifestError(f"{self.path}: a file has a size and a modification time")
            if len(self.hashes) != -(-self.size // CHUNK):
                raise ManifestError(f"{self.path}: {self.size} bytes is {-(-self.size // CHUNK)} chunks, "
                                    f"not {len(self.hashes)}")
            if not all(is_hash(h) for h in self.hashes):
                raise ManifestError(f"{self.path}: a hash is 64 hexadecimal characters")
        if self.kind == "l" and (not isinstance(self.target, str) or not self.target):
            raise ManifestError(f"{self.path}: a link has a target")

    def unchanged(self, *, size: int, mtime: int, mode: int) -> bool:
        """Whether a file found on disk with these facts is this file as it was: it keeps these hashes unread."""
        return self.kind == "f" and (self.size, self.mtime, self.mode) == (size, mtime, mode)

    def to_json(self) -> dict[str, Any]:
        if self.kind == "f":
            return {"p": self.path, "t": "f", "m": self.mode, "s": self.size, "mt": self.mtime, "h": list(self.hashes)}
        if self.kind == "d":
            return {"p": self.path, "t": "d", "m": self.mode}
        return {"p": self.path, "t": "l", "to": self.target}

    @classmethod
    def from_json(cls, obj: object) -> "Entry":
        if not isinstance(obj, dict) or obj.get("t") not in _KEYS or set(obj) != _KEYS[obj["t"]]:
            raise ManifestError(f"not a manifest entry: {str(obj)[:120]}")
        hashes = obj.get("h", [])
        if not isinstance(hashes, list):
            raise ManifestError(f"{obj['p']}: h is a list of hashes")
        return cls(path=obj["p"], kind=obj["t"], mode=obj.get("m", 0), size=obj.get("s", 0), mtime=obj.get("mt", 0),
                   hashes=tuple(hashes), target=obj.get("to", ""))


class SnapshotManifest:
    """Every path of one snapshot. Immutable once made.

    Every entry below a root sits in a listed directory, so writing the entries out never passes through a
    link or a file.
    """

    def __init__(self, entries: Iterable[Entry]) -> None:
        self.entries: dict[str, Entry] = {}
        for entry in entries:
            if entry.path in self.entries:
                raise ManifestError(f"{entry.path} is listed twice")
            self.entries[entry.path] = entry
            if len(self.entries) > ENTRIES_MAX:
                raise ManifestError(f"a snapshot holds at most {ENTRIES_MAX} entries")
        for path in self.entries:
            parent = path.rpartition("/")[0]
            if parent not in ROOTS and (parent not in self.entries or self.entries[parent].kind != "d"):
                raise ManifestError(f"{path}: {parent} is not a listed directory")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SnapshotManifest) and self.entries == other.entries

    __hash__ = None  # type: ignore[assignment]

    def dumps(self) -> str:
        return "\n".join(json.dumps(self.entries[p].to_json(), separators=(",", ":")) for p in sorted(self.entries))

    @classmethod
    def loads(cls, text: str) -> "SnapshotManifest":
        def entries() -> Iterable[Entry]:
            for number, line in enumerate(text.splitlines(), 1):
                if not line:
                    continue
                try:
                    yield Entry.from_json(json.loads(line))
                except json.JSONDecodeError:
                    raise ManifestError(f"line {number} is not JSON") from None
        return cls(entries())

    def hashes(self) -> set[str]:
        return {h for entry in self.entries.values() for h in entry.hashes}

    def size(self) -> int:
        return sum(entry.size for entry in self.entries.values() if entry.kind == "f")

    def files(self) -> int:
        return sum(1 for entry in self.entries.values() if entry.kind == "f")

    def listing(self, root: str) -> list[dict[str, Any]]:
        """The files under one root, as the APIs list them (spec §12.7)."""
        prefix = f"{root}/"
        return [
            {"path": path[len(prefix):], "bytes": entry.size, "modified_at": entry.mtime // 1_000_000_000}
            for path, entry in sorted(self.entries.items())
            if entry.kind == "f" and path.startswith(prefix)
        ]
