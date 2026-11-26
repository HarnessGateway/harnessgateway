"""What a process is told through its environment. Nothing is read from anywhere else, and everything is checked
when the process starts, so a wrong setting stops it then and not in the middle of a turn."""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harnessgateway.domain.egress import entries


class SettingsError(Exception):
    """A setting is missing or wrong. The message names it and says what is allowed."""


NO_SEALING_KEY = "HG_SECRET_KEY is 32 random bytes in base64: openssl rand -base64 32"


def _text(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = env.get(name) or default
    if value is None:
        raise SettingsError(f"{name} is required")
    return value


def _number(env: Mapping[str, str], name: str, default: int) -> int:
    text = _text(env, name, str(default))
    if not text.isdigit() or int(text) < 1:
        raise SettingsError(f"{name} is a whole number, 1 or more: {text!r}")
    return int(text)


def _listed(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(_text(env, name, "").replace(",", " ").split())


@dataclass(frozen=True)
class Settings:
    """What `serve` runs with."""

    database_url: str
    secret_key: bytes | None                   # seals secrets at rest; None: an extension's sealer does
    proxy_key: str                             # what the egress proxy proves itself with
    data: Path                                 # where the data volume is mounted: sandboxes' folders live on it
    blobs: str                                 # where bytes are kept: a path, or a URL an extension's store answers
    namespace: str                             # names everything this installation makes on its host
    driver: str                                # which driver runs sandboxes
    runtime: str                               # the Docker driver's: auto | runsc | runc
    port: int
    internal_port: int
    max_turns: int                             # how many turns one process runs at once
    idle_s: int                                # how long a session's folders are kept warm after its last turn
    extensions: tuple[str, ...]                # `module:Class`, in the order they are asked
    console: bool                              # whether the console's pages are served beside the API

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        key = None
        if env.get("HG_SECRET_KEY"):
            try:
                key = base64.b64decode(env["HG_SECRET_KEY"], validate=True)
            except binascii.Error:
                key = b""
            if len(key) != 32:
                raise SettingsError(NO_SEALING_KEY)
        runtime = _text(env, "HG_RUNTIME", "auto")
        if runtime not in ("auto", "runsc", "runc"):
            raise SettingsError(f"HG_RUNTIME is auto, runsc or runc: {runtime!r}")
        console = _text(env, "HG_CONSOLE", "on")
        if console not in ("on", "off"):
            raise SettingsError(f"HG_CONSOLE is on or off: {console!r}")
        data = Path(_text(env, "HG_DATA", "/data"))
        return cls(
            database_url=_text(env, "HG_DATABASE_URL"), secret_key=key, proxy_key=_text(env, "HG_PROXY_KEY"),
            data=data, blobs=_text(env, "HG_BLOBS", str(data / "blobs")), namespace=_text(env, "HG_NAMESPACE", "hg"),
            driver=_text(env, "HG_DRIVER", "docker"), runtime=runtime, port=_number(env, "HG_PORT", 8000),
            internal_port=_number(env, "HG_INTERNAL_PORT", 8001), max_turns=_number(env, "HG_MAX_TURNS", 8),
            idle_s=_number(env, "HG_IDLE_S", 3600), extensions=_listed(env, "HG_EXTENSIONS"),
            console=console == "on")


@dataclass(frozen=True)
class ProxySettings:
    """What `proxy` runs with: no database address and no sealing key."""

    proxy_key: str
    internal_url: str                          # where it asks the platform about a running sandbox
    port: int
    private_hosts: tuple[str, ...]             # hosts on private addresses a sandbox may reach: a model server, say

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ProxySettings":
        try:
            private_hosts = entries(_listed(env, "HG_PRIVATE_HOSTS"))
        except ValueError as error:
            raise SettingsError(f"HG_PRIVATE_HOSTS: {error}") from None
        return cls(proxy_key=_text(env, "HG_PROXY_KEY"),
                   internal_url=_text(env, "HG_INTERNAL_URL", "http://serve:8001").rstrip("/"),
                   port=_number(env, "HG_PROXY_PORT", 8080), private_hosts=private_hosts)
