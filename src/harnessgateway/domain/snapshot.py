"""A snapshot: what a session's two folders held when a turn ended, as a manifest of entries. A file's content is
a list of chunk hashes, so files that did not change between turns cost nothing more to keep."""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

CHUNK = 4 * 1024 * 1024
ENTRIES_MAX = 1_000_000
ROOTS = ("mind", "work")                       # the two folders, as a manifest's paths begin
_HASH = re.compile(r"[0-9a-f]{64}")
_FIELDS = {"f": ("path", "kind", "mode", "size", "mtime", "hashes"), "d": ("path", "kind", "mode"),
           "l": ("path", "kind", "target")}


class ManifestError(ValueError):
    """A manifest does not describe a tree that can be written out."""


def is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


@dataclass(frozen=True)
class Entry:
    path: str                                  # "work/src/main.py"
    kind: Literal["f", "d", "l"]               # a file, a directory, or a link
    mode: int = 0
    size: int = 0
    mtime: int = 0                             # nanoseconds
    hashes: tuple[str, ...] = ()               # a file's chunks, in order
    target: str = ""                           # where a link points

    def __post_init__(self) -> None:
        root, _, rest = self.path.partition("/")
        inside = rest and not any(part in ("", ".", "..") for part in rest.split("/")) and "\x00" not in rest
        if root not in ROOTS or not inside:
            raise ManifestError(f"{self.path!r} is not a path under {' or '.join(ROOTS)}")
        if self.kind not in _FIELDS or not all(is_hash(digest) for digest in self.hashes):
            raise ManifestError(f"{self.path}: not an entry")
        if self.kind == "f" and len(self.hashes) != -(-self.size // CHUNK):
            raise ManifestError(f"{self.path}: {len(self.hashes)} chunks do not hold {self.size} bytes")

    def unchanged(self, *, size: int, mtime: int, mode: int) -> bool:
        """Whether a file on disk with these facts is this entry still, so that it need not be read again."""
        return self.kind == "f" and (self.size, self.mtime, self.mode) == (size, mtime, mode)

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _FIELDS[self.kind]}

    @classmethod
    def from_json(cls, data: object) -> "Entry":
        try:
            assert isinstance(data, dict)
            return cls(**{**data, "hashes": tuple(data.get("hashes", ()))})
        except (AssertionError, TypeError):
            raise ManifestError(f"not an entry: {str(data)[:80]}") from None


class Manifest:
    """Every path of one snapshot. Each entry sits in a directory that is itself listed, so writing the entries
    out in path order never passes through a link or a file."""

    def __init__(self, entries: Iterable[Entry] = ()) -> None:
        self.entries: dict[str, Entry] = {}
        for entry in entries:
            if entry.path in self.entries or len(self.entries) >= ENTRIES_MAX:
                raise ManifestError(f"{entry.path} is listed twice, or the snapshot passes {ENTRIES_MAX} entries")
            self.entries[entry.path] = entry
        for path in self.entries:
            parent = path.rpartition("/")[0]
            if parent not in ROOTS and (parent not in self.entries or self.entries[parent].kind != "d"):
                raise ManifestError(f"{path}: {parent} is not a listed directory")

    def dumps(self) -> str:
        return "\n".join(json.dumps(self.entries[path].to_json(), separators=(",", ":"))
                         for path in sorted(self.entries))

    @classmethod
    def loads(cls, text: str) -> "Manifest":
        try:
            return cls(Entry.from_json(json.loads(line)) for line in text.splitlines() if line)
        except json.JSONDecodeError:
            raise ManifestError("a line of the manifest is not JSON") from None

    def hashes(self) -> set[str]:
        return {digest for entry in self.entries.values() for digest in entry.hashes}

    def size(self) -> int:
        return sum(entry.size for entry in self.entries.values())

    def files(self, under: str = "") -> dict[str, Entry]:
        """The files whose path begins `under`, by the rest of their path, in order."""
        return {path[len(under):]: entry for path, entry in sorted(self.entries.items())
                if entry.kind == "f" and path.startswith(under)}
