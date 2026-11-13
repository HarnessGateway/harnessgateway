"""Walking a session's two folders (spec §12.2). Links are reported, never followed."""

import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Found:
    path: str                                  # as a manifest names it: "work/src/main.py"
    location: Path                             # where it is on this machine
    kind: Literal["f", "d", "l", "other"]      # other: a socket, a pipe or a device
    mode: int
    size: int                                  # bytes, for a file; 0 for anything else
    mtime: int                                 # nanoseconds


def _kind(mode: int) -> Literal["f", "d", "l", "other"]:
    if stat.S_ISLNK(mode):
        return "l"
    if stat.S_ISDIR(mode):
        return "d"
    return "f" if stat.S_ISREG(mode) else "other"


def _listing(prefix: str, folder: Path) -> Iterator[tuple[str, os.DirEntry[str]]]:
    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except FileNotFoundError:                  # removed while being walked
        return
    for entry in entries:
        yield f"{prefix}/{entry.name}", entry


def walk(roots: Mapping[str, Path]) -> Iterator[Found]:
    """Everything under each root: a directory before what it holds, names in order.

    A tree may be changing while it is walked (the watcher's case); what vanishes is left out.
    """
    for name, root in roots.items():
        pending = [_listing(name, root)]       # a stack, not recursion: a tree may be deeper than Python allows
        while pending:
            for path, entry in pending[-1]:
                try:
                    status = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = _kind(status.st_mode)
                yield Found(path, Path(entry.path), kind, stat.S_IMODE(status.st_mode),
                            status.st_size if kind == "f" else 0, status.st_mtime_ns)
                if kind == "d":
                    pending.append(_listing(path, Path(entry.path)))
                    break
            else:
                pending.pop()


def measure(roots: Mapping[str, Path]) -> int:
    """The bytes a snapshot of these folders would hold: the sum of their files' sizes."""
    return sum(found.size for found in walk(roots) if found.kind == "f")
