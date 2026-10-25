"""The harness package, contract 1: the only module that knows what is in one.

A package is a directory:

    harness.json       the manifest (`Manifest`): how to launch it, never what the CLI does
    install.sh         installs the CLI into $HG_TOOLS, once per package digest, with egress to `install.egress`
    request.sh         one turn: reads the Responses request at $HG_REQUEST, prints Responses stream events on stdout
    seeds/workspace/   copied into a new session's workspace
    seeds/home/        copied into a new session's $HOME

Anything else (a mapper, a README, tests) is the package's own; the platform copies seeds, runs two scripts and reads
nothing else. A package is identified by its digest, a SHA-256 over its files' paths, modes and bytes, and moved as a
canonical (deterministic) gzipped tar.
"""
from __future__ import annotations

import gzip
import io
import json
import posixpath
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..errors import Invalid, TooLarge
from ..ids import NAME, sha256
from ..limits import PACKAGE_BYTES_MAX, PACKAGE_FILES_MAX

CONTRACT = 1
REQUIRED = ("harness.json", "install.sh", "request.sh")
SCRIPTS = ("install.sh", "request.sh")
SEEDS = ("workspace", "home")
PROVIDER_APIS = ("anthropic", "openai")
_HOST = re.compile(r"(?=.{1,253}(:|$))([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+(:[0-9]{1,5})?")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hosts(values: list[str]) -> list[str]:
    out = []
    for v in values:
        host = v.strip().lower()
        if not _HOST.fullmatch(host):
            raise ValueError(f"an egress entry is a hostname with an optional :port, not {v!r}")
        out.append(host)
    return sorted(set(out))


Hosts = Annotated[list[str], AfterValidator(_hosts)]


class ProviderSpec(_Strict):
    apis: Annotated[list[Literal["anthropic", "openai"]], Field(min_length=1)]


class InstallSpec(_Strict):
    egress: Hosts = []
    timeout_s: Annotated[int, Field(ge=1, le=3600)] = 900


class RunSpec(_Strict):
    egress: Hosts = []
    timeout_s: Annotated[int, Field(ge=1, le=86_400)] = 3600
    stop_grace_s: Annotated[int, Field(ge=1, le=300)] = 20


class SettingSpec(_Strict):
    secret: bool = False
    required: bool = False
    description: Annotated[str, Field(max_length=500)] = ""


class Defaults(_Strict):
    model: Annotated[str, Field(max_length=200)] = ""


class Manifest(_Strict):
    contract: Literal[1]
    name: Annotated[str, Field(pattern=f"^{NAME.pattern}$")]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    description: Annotated[str, Field(max_length=1000)] = ""
    provider: ProviderSpec | None = None
    defaults: Defaults = Defaults()
    install: InstallSpec = InstallSpec()
    run: RunSpec = RunSpec()
    settings: dict[str, SettingSpec] = {}

    @field_validator("settings")
    @classmethod
    def _setting_names(cls, v: dict[str, SettingSpec]) -> dict[str, SettingSpec]:
        if len(v) > 64 or not all(_ENV.fullmatch(k) for k in v):
            raise ValueError("settings are at most 64, named like environment variables (A-Z, 0-9, _)")
        return v

    @classmethod
    def parse(cls, text: str) -> Manifest:
        try:
            return cls.model_validate(json.loads(text))
        except ValueError as e:
            raise Invalid(f"harness.json: {_first_error(e)}", param="harness.json") from None

    def check_settings(self, values: dict[str, str]) -> None:
        """Values an organization gives for this package's settings: only declared names, every required one."""
        unknown = sorted(set(values) - set(self.settings))
        if unknown:
            raise Invalid(f"settings not declared by {self.name}: {', '.join(unknown)}", param="settings")
        if any(not isinstance(v, str) or len(v) > 16_384 for v in values.values()):
            raise Invalid("a setting is a string of at most 16 KiB", param="settings")

    def missing_settings(self, values: dict[str, str]) -> list[str]:
        return sorted(k for k, spec in self.settings.items() if spec.required and not values.get(k))


def _first_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        err = e.errors()[0]
        where = ".".join(str(p) for p in err.get("loc", ()))
        return f"{where or 'body'}: {err.get('msg', 'invalid')}"
    return str(e)


def unpack(archive: bytes, into: Path) -> None:
    """Extract a package archive (tar, gzipped or not): regular files and directories only, nothing outside the root,
    within the file and size limits. A single top-level directory wrapping everything is stripped."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")  # noqa: SIM115 - closed by the with below
    except tarfile.TarError:
        raise Invalid("a package is uploaded as a tar archive, gzipped or not") from None
    with tar:
        members = [m for m in tar.getmembers() if posixpath.normpath(m.name) not in (".", "")]
        if len(members) > PACKAGE_FILES_MAX or sum(m.size for m in members) > PACKAGE_BYTES_MAX:
            raise TooLarge(f"a package holds at most {PACKAGE_FILES_MAX} files and {PACKAGE_BYTES_MAX // 2**20} MiB")
        if any(m.name.startswith("/") or "\0" in m.name for m in members):
            raise Invalid("the package archive reaches outside itself")
        names = [posixpath.normpath(m.name) for m in members]
        if any(n == ".." or n.startswith("../") for n in names):
            raise Invalid("the package archive reaches outside itself")
        tops = {n.split("/", 1)[0] for n in names}
        strip = (tops.pop() + "/") if len(tops) == 1 and "harness.json" not in names else ""
        for m, n in zip(members, names, strict=True):
            if not (m.isfile() or m.isdir()):
                raise Invalid(f"the package archive holds something that is not a file or directory: {m.name}")
            rel = n[len(strip):] if strip and n.startswith(strip) else n
            if not rel or rel == strip.rstrip("/"):
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
    """The folder is a package on the contract, or Invalid says what is wrong. Scripts must parse as bash."""
    for name in REQUIRED:
        if not (folder / name).is_file():
            raise Invalid(f"a harness package needs {', '.join(REQUIRED)}: {name} is missing")
    files = [p for p in folder.rglob("*")]
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in files):
        raise Invalid("a package holds only regular files and directories")
    if len(files) > PACKAGE_FILES_MAX or sum(p.stat().st_size for p in files if p.is_file()) > PACKAGE_BYTES_MAX:
        raise TooLarge("the package is too large")
    manifest = Manifest.parse((folder / "harness.json").read_text(errors="replace"))
    for name in SCRIPTS:
        check = subprocess.run(["bash", "-n", str(folder / name)], capture_output=True, text=True, timeout=30)
        if check.returncode != 0:
            raise Invalid(f"{name} is not valid bash: {check.stderr.strip()[-500:]}")
    seeds = folder / "seeds"
    if seeds.exists():
        if not seeds.is_dir():
            raise Invalid("seeds is a directory")
        unknown = sorted(p.name for p in seeds.iterdir() if p.name not in SEEDS)
        if unknown:
            raise Invalid(f"seeds holds only {', '.join(SEEDS)}, not {', '.join(unknown)}")
    return manifest


def _entries(folder: Path) -> list[tuple[str, Path]]:
    return sorted((p.relative_to(folder).as_posix(), p) for p in folder.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


def digest(folder: Path) -> str:
    """The package's content identity: SHA-256 over every file's path, executable bit and bytes."""
    h = []
    for rel, p in _entries(folder):
        h.append(f"{rel}\0{1 if p.stat().st_mode & 0o111 else 0}\0{sha256(p.read_bytes())}\n")
    return sha256("".join(h).encode())


def pack(folder: Path) -> bytes:
    """The folder as a canonical gzipped tar: sorted, owned by root, dated 0, so the same files give the same bytes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for rel, p in _entries(folder):
            info = tarfile.TarInfo(rel)
            info.size, info.mtime, info.uid, info.gid = p.stat().st_size, 0, 0, 0
            info.mode = 0o755 if p.stat().st_mode & 0o111 else 0o644
            with p.open("rb") as f:
                tar.addfile(info, f)
    return gzip.compress(raw.getvalue(), mtime=0)
