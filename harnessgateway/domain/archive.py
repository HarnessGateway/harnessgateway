"""A harness package as it is uploaded: a gzip-compressed tar archive of a folder (spec §8.1).

Reading one is a calculation on bytes: nothing is written anywhere. The digest depends only on the files'
paths, whether each is executable, and their contents, so the same folder gives the same digest whatever
tool packed it.
"""

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass

from harnessgateway.domain.package import Manifest
from harnessgateway.errors import Invalid, TooLarge

PACKED_MAX = 64 * 1024 * 1024
UNPACKED_MAX = 256 * 1024 * 1024
FILES_MAX = 5000
MANIFEST_NAME = "harness.json"
EXECUTABLES = ("install", "run")
KIT_DIR = "kit"                                # in an install, where the platform puts the kit (spec §8.5)


@dataclass(frozen=True)
class PackageFile:
    path: str
    executable: bool
    content: bytes


@dataclass(frozen=True)
class Package:
    manifest: Manifest
    files: tuple[PackageFile, ...]             # in path order
    digest: str

    @property
    def size(self) -> int:
        return sum(len(file.content) for file in self.files)


def _path(name: str) -> str:
    path = name.removeprefix("./").rstrip("/")
    if name.startswith("/") or not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise Invalid(f"a path in a package stays inside it, with no '..' and no empty part: {name[:120]!r}")
    return path


def _digest(files: tuple[PackageFile, ...]) -> str:
    """Each file as its path, its executable bit and its content, every part with its length in front."""
    total = hashlib.sha256()
    for file in files:
        for part in (file.path.encode(), b"x" if file.executable else b"-", file.content):
            total.update(len(part).to_bytes(8, "big"))
            total.update(part)
    return total.hexdigest()


def _files(data: bytes) -> tuple[PackageFile, ...]:
    files: dict[str, PackageFile] = {}
    unpacked = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isreg():
                    raise Invalid(f"a package holds plain files and folders only, no links or devices: {member.name}")
                path = _path(member.name)
                if path in files:
                    raise Invalid(f"{path} is in the package twice")
                if path.split("/")[0] == KIT_DIR:
                    raise Invalid(f"{path}: {KIT_DIR}/ is where the platform puts the kit; a package cannot hold it")
                unpacked += member.size
                if len(files) >= FILES_MAX:
                    raise TooLarge(f"a package holds at most {FILES_MAX} files")
                if unpacked > UNPACKED_MAX:
                    raise TooLarge(f"a package is at most {UNPACKED_MAX >> 20} MiB unpacked")
                source = tar.extractfile(member)
                assert source is not None
                files[path] = PackageFile(path, bool(member.mode & 0o100), source.read())
    except (tarfile.TarError, OSError, EOFError) as e:
        raise Invalid(f"a package is a gzip-compressed tar archive: {e}") from None
    return tuple(files[path] for path in sorted(files))


def read_archive(data: bytes) -> Package:
    """The package in an uploaded archive; or a refusal that says what is wrong with it."""
    if len(data) > PACKED_MAX:
        raise TooLarge(f"a package archive is at most {PACKED_MAX >> 20} MiB")
    files = _files(data)
    by_path = {file.path: file for file in files}
    if MANIFEST_NAME not in by_path:
        raise Invalid(f"the package has no {MANIFEST_NAME}")
    try:
        manifest = Manifest.load(json.loads(by_path[MANIFEST_NAME].content))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Invalid(f"{MANIFEST_NAME} is not JSON") from None
    for name in EXECUTABLES:
        if name not in by_path:
            raise Invalid(f"the package has no {name}")
        if not by_path[name].executable:
            raise Invalid(f"{name} is not executable")
    return Package(manifest=manifest, files=files, digest=_digest(files))


def write_archive(files: Iterable[PackageFile]) -> bytes:
    """Files as a package archive. The same files give the same bytes, whoever packs them and whenever."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for file in sorted(files, key=lambda file: file.path):
            info = tarfile.TarInfo(file.path)
            info.size = len(file.content)
            info.mode = 0o755 if file.executable else 0o644
            tar.addfile(info, io.BytesIO(file.content))
    return gzip.compress(raw.getvalue(), mtime=0)
