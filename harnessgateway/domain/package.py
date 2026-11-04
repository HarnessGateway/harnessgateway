"""A harness package on disk and in transit (SPEC §5.1): validation of its folder, its digest, and its canonical archive.

The platform reads `harness.json`, runs `install` and `run` (two executables, in any language), and copies
`seeds/home` and `seeds/workspace`. Everything else in a package is the package's own."""
from __future__ import annotations

import gzip
import io
import posixpath
import tarfile
from pathlib import Path

from ..errors import Invalid, TooLarge
from ..ids import sha256
from ..limits import PACKAGE_BYTES_MAX, PACKAGE_FILES_MAX
from .manifest import Manifest

SCRIPTS = ("install", "run")
SEEDS = ("home", "workspace")


def unpack(archive: bytes, into: Path) -> None:
    """A package archive (tar, gzipped or not) into a new folder: regular files and directories only, nothing outside
    the root, within the limits. A single top-level directory wrapping everything is stripped."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")  # noqa: SIM115 - closed below
    except tarfile.TarError:
        raise Invalid("a package is a tar archive, gzipped or not", param="package") from None
    with tar:
        members = [m for m in tar.getmembers() if posixpath.normpath(m.name) not in (".", "")]
        if len(members) > PACKAGE_FILES_MAX or sum(m.size for m in members) > PACKAGE_BYTES_MAX:
            raise TooLarge(f"a package holds at most {PACKAGE_FILES_MAX} files and {PACKAGE_BYTES_MAX >> 20} MiB")
        names = [posixpath.normpath(m.name) for m in members]
        if any(m.name.startswith("/") or "\0" in m.name for m in members) or \
                any(n == ".." or n.startswith("../") for n in names):
            raise Invalid("the package archive reaches outside itself", param="package")
        tops = {n.split("/", 1)[0] for n in names}
        strip = tops.pop() + "/" if len(tops) == 1 and "harness.json" not in names else ""
        into.mkdir(parents=True)
        for m, n in zip(members, names, strict=True):
            if not (m.isfile() or m.isdir()):
                raise Invalid(f"a package holds only files and directories, not {m.name!r}", param="package")
            rel = n[len(strip):] if strip and n.startswith(strip) else n
            if not rel or n + "/" == strip:
                continue
            dest = into / rel
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            assert src is not None
            dest.write_bytes(src.read())
            dest.chmod(0o755 if m.mode & 0o111 else 0o644)


def validate(folder: Path) -> Manifest:
    """The folder is a package on the contract, or Invalid says what is wrong: a manifest, the two executables (`install`
    and `run`: a script with a shebang line or a Linux binary), and nothing under seeds but home and workspace. The
    executables are marked so, since the contract runs them."""
    files = list(folder.rglob("*"))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in files):
        raise Invalid("a package holds only regular files and directories", param="package")
    if len(files) > PACKAGE_FILES_MAX or sum(p.stat().st_size for p in files if p.is_file()) > PACKAGE_BYTES_MAX:
        raise TooLarge("the package is too large")
    if not (folder / "harness.json").is_file():
        raise Invalid("a package needs harness.json at its root", param="package")
    manifest = Manifest.parse((folder / "harness.json").read_text(errors="replace"))
    for name in SCRIPTS:
        script = folder / name
        if not script.is_file():
            raise Invalid(f"a package needs `{name}` (an executable with a shebang line) beside harness.json",
                          param="package")
        with script.open("rb") as f:
            head = f.read(4)
        if head[:2] != b"#!" and head != b"\x7fELF":
            raise Invalid(f"{name} is an executable: a script whose first line names its interpreter (#!/usr/bin/env "
                          f"python3, #!/usr/bin/env bash, ...) or a Linux binary", param="package")
        script.chmod(script.stat().st_mode | 0o111)
    manifest.check_references()
    seeds = folder / "seeds"
    if seeds.exists():
        unknown = sorted(p.name for p in seeds.iterdir()) if seeds.is_dir() else ["seeds"]
        unknown = [n for n in unknown if n not in SEEDS]
        if unknown:
            raise Invalid(f"seeds holds only {' and '.join(SEEDS)}, not {', '.join(unknown)}", param="package")
    return manifest


def entries(folder: Path) -> list[tuple[str, Path]]:
    """Every file of a package folder as (relative posix path, path), sorted."""
    return sorted((p.relative_to(folder).as_posix(), p) for p in folder.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


_entries = entries


def clean_member(rel: object) -> str:
    """A path inside a package, or Invalid: relative, normalized, never leaving the root."""
    if not isinstance(rel, str) or not rel or len(rel) > 1024 or "\0" in rel or rel.startswith("/"):
        raise Invalid("a file path inside the package", param="path")
    norm = posixpath.normpath(rel)
    if norm in (".", "..") or norm.startswith("../"):
        raise Invalid("a file path inside the package", param="path")
    return norm


def digest(folder: Path) -> str:
    """SHA-256 over every file's path, executable bit and bytes."""
    lines = (f"{rel}\0{1 if p.stat().st_mode & 0o111 else 0}\0{sha256(p.read_bytes())}\n" for rel, p in _entries(folder))
    return sha256("".join(lines).encode())


def pack(folder: Path) -> bytes:
    """The canonical gzipped tar: sorted, owned by root, dated 0, so the same files give the same bytes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for rel, p in _entries(folder):
            info = tarfile.TarInfo(rel)
            info.size, info.mtime, info.uid, info.gid = p.stat().st_size, 0, 0, 0
            info.mode = 0o755 if p.stat().st_mode & 0o111 else 0o644
            with p.open("rb") as f:
                tar.addfile(info, f)
    return gzip.compress(raw.getvalue(), mtime=0)

