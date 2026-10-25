"""Layer 1: the harness package, contract 8. The only module that knows what is in one.

A package is a directory (kept as a git repository, moved as a tar) and holds:
- `harness.json`: what the platform needs to launch it, and nothing about the CLI's behaviour;
- `install.sh`: puts the CLI into `$HG_TOOLS`, once per package version, with egress to `install.egress`;
- `request.sh`: runs one turn. The client's Responses request is at `$HG_REQUEST` (and on stdin),
  verbatim; the script reads what its CLI can use and runs it. Its stdout is relayed live and kept:
  JSON lines whose `type` starts with `response.` are Responses stream events the client receives;
- `response.sh` (optional): runs after request.sh with `$HG_STDOUT`, `$HG_STDERR`, `$HG_EXIT` and
  `$HG_OUTCOME`, and prints one JSON object shaping the response (`output`, `usage`, `status`,
  `error`, `incomplete_details`, `metadata`); its exit code is the status. Without it the response
  is assembled from the `response.output_item.done` events request.sh printed;
- `seeds/workspace/` (optional): files copied into the workspace when a session starts;
- `seeds/home/` (optional): files copied into `$HOME` every turn, replacing what is there.

Anything else in the directory is the package's own (a mapper in Python, a README, tests); the
platform copies seeds and runs the three scripts, and reads nothing else.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import Invalid
from ..kernel import files

CONTRACT = 8
ACCEPTED = (7, CONTRACT)                             # a contract-7 package still runs; its `interrupt` (the steer signal 8 dropped) is ignored
REQUIRED = ("harness.json", "install.sh", "request.sh")
SCRIPTS = ("install.sh", "request.sh", "response.sh")
SEEDS = ("workspace", "home")
MAX_FILES, MAX_BYTES = 10_000, 256 * 1024 * 1024
KEYS = {"contract", "description", "cli", "defaults", "provider_env", "install", "run", "accepts"}
FIELDS = ("input", "instructions", "previous_response_id", "reasoning.effort", "reasoning.summary", "text.format",
          "text.verbosity", "tools", "max_tool_calls", "max_output_tokens", "store", "truncation", "service_tier",
          "metadata", "temperature", "top_p")


@dataclass(frozen=True)
class Config:
    """harness.json, loaded. Every field has a default; a request's `model` may override the two defaults."""
    description: str = ""
    cli: str = ""
    version: str = ""
    provider: str = ""
    model: str = ""
    key_env: str = ""
    url_env: str = ""
    install_egress: tuple[str, ...] = ()
    install_timeout_s: float = 900
    run_egress: tuple[str, ...] = ()
    timeout_s: float = 3600
    stop_timeout_s: float = 20
    accepts: tuple[str, ...] = field(default_factory=lambda: ("input", "previous_response_id"))

    def as_json(self) -> dict[str, Any]:
        return {"contract": CONTRACT, "description": self.description, "cli": {"name": self.cli, "version": self.version},
                "defaults": {"provider": self.provider, "model": self.model},
                "provider_env": {"key": self.key_env, "url": self.url_env},
                "install": {"egress": list(self.install_egress), "timeout_s": self.install_timeout_s},
                "run": {"egress": list(self.run_egress), "timeout_s": self.timeout_s, "stop_timeout_s": self.stop_timeout_s},
                "accepts": list(self.accepts)}

    @property
    def egress(self) -> tuple[str, ...]:
        return tuple(sorted({*self.install_egress, *self.run_egress}))

    @property
    def needs_provider(self) -> bool:
        """A package that names no provider_env runs without a provider: no token, no gateway, any CLI."""
        return bool(self.key_env or self.url_env)


def _text(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default)
    if not isinstance(v, str):
        raise Invalid(f"harness.json: {key} is text")
    return v


def _seconds(d: dict, key: str, default: float) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise Invalid(f"harness.json: {key} is a positive number of seconds")
    return float(v)


def _section(d: dict, key: str, allowed: tuple[str, ...]) -> dict:
    s = d.get(key, {})
    if not isinstance(s, dict) or any(k not in allowed for k in s):
        raise Invalid(f"harness.json: {key} is an object with only {', '.join(allowed)}")
    return s


def _hosts(section: dict, where: str) -> tuple[str, ...]:
    egress = section.get("egress", [])
    if not isinstance(egress, list) or not all(isinstance(h, str) and h.strip() for h in egress):
        raise Invalid(f"harness.json: {where}.egress is a list of hosts")
    return tuple(h.strip() for h in egress)


def load_config(text: str) -> Config:
    try:
        d = json.loads(text)
    except ValueError as e:
        raise Invalid(f"harness.json is not JSON: {e}") from None
    return load_config_dict(d)


def load_config_dict(d: Any) -> Config:
    if not isinstance(d, dict) or d.get("contract") not in ACCEPTED:
        raise Invalid(f'harness.json must be an object with "contract": {CONTRACT}')
    if unknown := sorted(set(d) - KEYS - ({"interrupt"} if d.get("contract") == 7 else set())):
        raise Invalid(f"harness.json: unknown keys {', '.join(unknown)}")
    cli = _section(d, "cli", ("name", "version"))
    defaults = _section(d, "defaults", ("provider", "model"))
    env = _section(d, "provider_env", ("key", "url"))
    install = _section(d, "install", ("egress", "timeout_s"))
    run = _section(d, "run", ("egress", "timeout_s", "stop_timeout_s"))
    for name in (env.get("key", ""), env.get("url", "")):
        if not isinstance(name, str) or (name and not name.replace("_", "").isalnum()):
            raise Invalid("harness.json: provider_env names are environment variable names")
    accepts = d.get("accepts", ["input", "previous_response_id"])
    if not isinstance(accepts, list) or not all(isinstance(a, str) and a in FIELDS for a in accepts):
        raise Invalid(f"harness.json: accepts is a list of request fields from {', '.join(FIELDS)}")
    return Config(description=_text(d, "description"), cli=_text(cli, "name"), version=_text(cli, "version"),
                  provider=_text(defaults, "provider"), model=_text(defaults, "model"),
                  key_env=env.get("key", ""), url_env=env.get("url", ""),
                  install_egress=_hosts(install, "install"), install_timeout_s=_seconds(install, "timeout_s", 900),
                  run_egress=_hosts(run, "run"), timeout_s=_seconds(run, "timeout_s", 3600),
                  stop_timeout_s=_seconds(run, "stop_timeout_s", 20), accepts=tuple(accepts))


def unpack(archive: bytes, into: Path) -> None:
    """Extract an uploaded tar into `into`: regular files and directories only, under one root. A
    single top-level directory wrapping everything (`tar czf x.tgz my-harness/`) is stripped."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")
    except tarfile.TarError:
        raise Invalid("a harness is uploaded as a tar archive, gzipped or not") from None
    with tar:
        members = [m for m in tar.getmembers() if m.name not in (".", "./")]
        if len(members) > MAX_FILES or sum(m.size for m in members) > MAX_BYTES:
            raise Invalid("the harness archive is too large")
        names = [posixpath.normpath(m.name) for m in members]
        if any(n.startswith(("/", "../")) or n == ".." for n in names):
            raise Invalid("the harness archive reaches outside itself")
        tops = {n.split("/", 1)[0] for n in names}
        strip = tops.pop() + "/" if len(tops) == 1 and "harness.json" not in names else ""
        for m, n in zip(members, names):
            if strip and n == strip.rstrip("/") and m.isdir():
                continue
            rel = n[len(strip):] if n.startswith(strip) else n
            dest = into / rel
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as f, open(dest, "wb") as out:
                    out.write(f.read())
                os.chmod(dest, (m.mode & 0o755) | 0o644)
            else:
                raise Invalid(f"the harness archive holds something that is not a file or directory: {m.name}")


def validate(folder: Path) -> Config:
    """The folder is a package on the contract, or Invalid says what is missing. Its scripts are made
    executable: an upload may have lost the bit. Bash and Python files must at least parse."""
    for name in REQUIRED:
        if not (folder / name).is_file():
            raise Invalid(f"a harness package needs {', '.join(REQUIRED)}: {name} is missing")
    config = load_config((folder / "harness.json").read_text(errors="replace"))
    for name in SCRIPTS:
        script = folder / name
        if not script.is_file():
            continue
        os.chmod(script, script.stat().st_mode | 0o755)
        check = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        if check.returncode != 0:
            raise Invalid(f"{name} is not valid bash: {check.stderr.strip()[-500:]}")
    for py in sorted(folder.rglob("*.py")):
        check = subprocess.run(["python3", "-I", "-m", "py_compile", str(py)], capture_output=True, text=True)
        if check.returncode != 0:
            raise Invalid(f"{py.relative_to(folder).as_posix()} is not valid Python: {check.stderr.strip()[-500:]}")
    seeds = folder / "seeds"
    if seeds.exists():
        if not seeds.is_dir():
            raise Invalid("seeds is a directory")
        if unknown := sorted(p.name for p in seeds.iterdir() if p.name not in SEEDS):
            raise Invalid(f"seeds holds only {', '.join(SEEDS)}, not {', '.join(unknown)}")
    return config


def digest(folder: Path) -> str:
    """The package's content identity: its files' paths and bytes."""
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(f"{p.relative_to(folder).as_posix()}\0".encode())
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:16]


def pack(folder: Path) -> bytes:
    """The validated folder as a gzipped tar, the canonical form handed to workers."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


def seed(folder: Path, kind: str) -> Path | None:
    p = folder / "seeds" / kind
    return p if p.is_dir() else None


def copy_seed(source: Path, into: Path, overwrite: bool) -> None:
    """Every file under `source` into `into`, through the safe writer, so nothing the sandbox left in
    `into` can redirect a copy; an existing file is replaced only when `overwrite`."""
    for p in sorted(source.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(source).as_posix()
        if not overwrite and files.exists(into, rel):
            continue
        files.write(into, rel, p.read_bytes(), (p.stat().st_mode & 0o755) | 0o644)
