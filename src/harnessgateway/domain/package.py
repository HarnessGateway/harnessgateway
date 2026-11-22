"""A harness package as it travels: a gzip-compressed tar archive of a folder that holds `install` and `run`.
Reading one is where everything about it is checked, so nothing unchecked is ever installed."""

import gzip
import hashlib
import io
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass

from harnessgateway.errors import Invalid, TooLarge

PACKED_MAX = 64 * 1024 * 1024
UNPACKED_MAX = 256 * 1024 * 1024
FILES_MAX = 5000
PROGRAMS = ("install", "run")                  # what the platform runs: once for an image, and once a turn
_STRAY = frozenset({".git", "__pycache__", ".DS_Store"})


def is_stray(path: str) -> bool:
    """Whether a path is something a working folder holds, or a Mac's tar adds beside each file (`._name`), that
    is no part of a package: it is left out wherever a package is read."""
    parts = path.split("/")
    return bool(_STRAY & set(parts)) or parts[-1].startswith("._")


@dataclass(frozen=True)
class PackageFile:
    path: str
    content: bytes
    executable: bool = False


@dataclass(frozen=True)
class Package:
    files: tuple[PackageFile, ...]
    digest: str                                # of its files alone: the same files are the same package


def _path(name: str) -> str:
    """A member's name as a path inside the package; or a refusal if it could land anywhere else."""
    path = name.removeprefix("./")
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts) or "\x00" in path:
        raise Invalid(f"the package holds a path that is not inside it: {name!r}")
    return path


def _digest(files: Iterable[PackageFile]) -> str:
    seen = hashlib.sha256()
    for file in files:
        seen.update(f"{file.path}\x00{int(file.executable)}\x00{len(file.content)}\x00".encode())
        seen.update(file.content)
    return seen.hexdigest()


def read_archive(data: bytes) -> Package:
    """The package an archive holds, checked in full; or a refusal that says what is wrong with it."""
    if len(data) > PACKED_MAX:
        raise TooLarge(f"a package archive is at most {PACKED_MAX >> 20} MiB")
    files: dict[str, PackageFile] = {}
    size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if member.isdir() or is_stray(member.name.removeprefix("./")):
                    continue
                if not member.isfile():
                    raise Invalid(f"{member.name} is a link or a device: a package holds plain files and folders only")
                size += member.size
                if len(files) >= FILES_MAX or size > UNPACKED_MAX:
                    raise TooLarge(f"a package holds at most {FILES_MAX} files and {UNPACKED_MAX >> 20} MiB")
                content = archive.extractfile(member)
                assert content is not None                     # it is a plain file
                files[_path(member.name)] = PackageFile(_path(member.name), content.read(), bool(member.mode & 0o100))
    except (tarfile.TarError, EOFError, OSError):
        raise Invalid("a package is a gzip-compressed tar archive of its folder: "
                      "tar -czf package.tar.gz -C folder .") from None
    for program in PROGRAMS:
        if program not in files or not files[program].executable:
            raise Invalid(f"the package has no executable {program}")
    ordered = tuple(files[path] for path in sorted(files))
    return Package(ordered, _digest(ordered))


def write_archive(files: Iterable[PackageFile]) -> bytes:
    """Files as a package archive. The same files give the same bytes, whenever it is written."""
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as packed, tarfile.open(fileobj=packed, mode="w") as archive:
        for file in sorted(files, key=lambda file: file.path):
            member = tarfile.TarInfo(file.path)
            member.size, member.mode = len(file.content), 0o755 if file.executable else 0o644
            archive.addfile(member, io.BytesIO(file.content))
    return out.getvalue()
