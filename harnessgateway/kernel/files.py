"""Layer 0: reads and writes under trees a sandbox can modify, without following anything it planted.

The server runs as root and a sandbox runs as a tenant's user, so a symlink, a FIFO or a directory
the sandbox leaves in its workspace, home, run directory or tools directory must never redirect a
server read or write. Every path component is opened with O_NOFOLLOW from a directory descriptor,
and only regular files are read. Anything else is refused by name.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from ..errors import Invalid

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _parts(rel: str) -> list[str]:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or rel.startswith("/"):
        raise Invalid(f"a path inside the tree is relative and has no '..': {rel!r}")
    return parts


def _descend(root: Path, parts: list[str], create: bool) -> int:
    """A descriptor for the directory the last part lives in, opening each component without
    following links; intermediate directories are made when `create`."""
    fd = os.open(root, DIR_FLAGS)
    try:
        for name in parts[:-1]:
            try:
                nxt = os.open(name, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o755, dir_fd=fd)
                nxt = os.open(name, DIR_FLAGS, dir_fd=fd)
            except (NotADirectoryError, OSError) as e:
                raise Invalid(f"refused: {name!r} under {root} is not a plain directory ({e.strerror})") from None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def write(root: Path, rel: str, data: bytes, mode: int = 0o644) -> None:
    """Write `data` to `root/rel`, creating directories, refusing links at every step."""
    parts = _parts(rel)
    fd = _descend(Path(root), parts, create=True)
    try:
        try:
            f = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=fd)
        except OSError as e:
            raise Invalid(f"refused: {rel!r} is not a plain file ({e.strerror})") from None
        with os.fdopen(f, "wb") as out:
            st = os.fstat(out.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise Invalid(f"refused: {rel!r} is not a plain file")
            out.write(data)
    finally:
        os.close(fd)


def read(root: Path, rel: str, limit: int | None = None) -> bytes:
    """The bytes of `root/rel`, a plain file reached through plain directories; at most `limit`."""
    parts = _parts(rel)
    fd = _descend(Path(root), parts, create=False)
    try:
        try:
            f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC, dir_fd=fd)
        except OSError as e:
            raise Invalid(f"refused: {rel!r} is not a plain file ({e.strerror})") from None
        if not stat.S_ISREG(os.fstat(f).st_mode):
            os.close(f)
            raise Invalid(f"refused: {rel!r} is not a plain file")
        with os.fdopen(f, "rb") as src:
            return src.read(limit) if limit is not None else src.read()
    finally:
        os.close(fd)


def exists(root: Path, rel: str) -> bool:
    """Whether `root/rel` is a plain file reached through plain directories."""
    try:
        read(Path(root), rel, limit=0)
        return True
    except (Invalid, FileNotFoundError):
        return False


def unlink(root: Path, rel: str) -> None:
    """Remove `root/rel` itself (a link is removed, never followed); nothing if it is not there."""
    parts = _parts(rel)
    try:
        fd = _descend(Path(root), parts, create=False)
    except FileNotFoundError:
        return
    try:
        os.unlink(parts[-1], dir_fd=fd)
    except (FileNotFoundError, IsADirectoryError):
        pass
    finally:
        os.close(fd)
