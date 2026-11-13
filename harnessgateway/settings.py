"""Settings: read once from the environment, checked together, and named when wrong (spec §18.4)."""

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import harnessgateway
from harnessgateway.domain.hosts import HostEntry
from harnessgateway.errors import Invalid

_RUNTIMES = ("auto", "runsc", "runc")
_LOG_LEVELS = ("debug", "info", "warning", "error")


class SettingsError(Exception):
    """One or more settings are missing or invalid. The message names each, never a secret's value."""


def _secret_key(text: str) -> bytes:
    try:
        key = base64.b64decode(text, validate=True)
    except binascii.Error:
        raise ValueError("is 32 bytes, base64-encoded") from None
    if len(key) != 32:
        raise ValueError(f"is 32 bytes, base64-encoded; it decodes to {len(key)}")
    return key


def one_of(choices: tuple[str, ...]) -> Callable[[str], str]:
    def check(text: str) -> str:
        if text not in choices:
            raise ValueError(f"is one of {', '.join(choices)}")
        return text
    return check


def number(minimum: int) -> Callable[[str], int]:
    def check(text: str) -> int:
        if not text.isdigit() or int(text) < minimum:
            raise ValueError(f"is a whole number, at least {minimum}")
        return int(text)
    return check


def _port(text: str) -> int:
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise ValueError("is a port, 1 to 65535")
    return int(text)


def listed(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _hosts(text: str) -> tuple[HostEntry, ...]:
    try:
        return tuple(HostEntry.parse(item) for item in listed(text))
    except Invalid as e:
        raise ValueError(e.message) from None


def _extensions(text: str) -> tuple[str, ...]:
    items = listed(text)
    if any(item.count(":") != 1 or not all(item.split(":")) for item in items):
        raise ValueError("is a comma-separated list of module:Class")
    return tuple(items)


def _certificates(path: str) -> str:
    """The PEM certificates in a file; "" when no file is named."""
    if not path:
        return ""
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise ValueError(f"names a file of PEM certificates that cannot be read: {e}") from None
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise ValueError("names a file that holds no PEM certificate")
    return text


def url(text: str) -> str:
    if not text.startswith(("http://", "https://")):
        raise ValueError("is an http:// or https:// URL")
    return text.rstrip("/")


# How a group of settings is read: field -> (variable, default or None when it is required, reader). `read` and
# the readers above it are also what another edition reads its own settings with (spec §19).
Spec = dict[str, tuple[str, str | None, Callable[[str], Any]]]
_INTERNAL_URL = ("HG_INTERNAL_URL", "http://serve:8001", url)
_PRIVATE_HOSTS = ("HG_PRIVATE_HOSTS", "", _hosts)
_LOG_LEVEL = ("HG_LOG_LEVEL", "info", one_of(_LOG_LEVELS))
_EXTRA_CA = ("HG_EXTRA_CA", "", _certificates)


def read(env: Mapping[str, str], spec: Spec) -> dict[str, Any]:
    """Every setting of a spec, read from the environment; or one error that names everything wrong."""
    values: dict[str, Any] = {}
    problems = []
    for field, (variable, default, reader) in spec.items():
        text = env.get(variable, default)
        if text is None or (default is None and not text):
            problems.append(f"{variable} is required")
            continue
        try:
            values[field] = reader(text)
        except ValueError as e:
            problems.append(f"{variable} {e}")
    if problems:
        raise SettingsError("; ".join(problems))
    return values


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: bytes
    proxy_key: str
    data: str
    catalog: str
    driver: str
    runtime: str
    platform_image: str
    sandbox_image: str
    internal_url: str
    port: int
    internal_port: int
    max_turns: int
    volume_ttl_s: int
    private_hosts: tuple[HostEntry, ...]
    extra_ca: str                              # PEM text: authorities the operator adds to what is trusted
    extensions: tuple[str, ...]
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        version = harnessgateway.__version__
        return cls(**read(env, {
            "database_url": ("HG_DATABASE_URL", None, str),
            "secret_key": ("HG_SECRET_KEY", None, _secret_key),
            "proxy_key": ("HG_PROXY_KEY", None, str),
            "data": ("HG_DATA", "/data", str),
            "catalog": ("HG_CATALOG", "/packages", str),
            "driver": ("HG_DRIVER", "docker", str),
            "runtime": ("HG_RUNTIME", "auto", one_of(_RUNTIMES)),
            "platform_image": ("HG_PLATFORM_IMAGE", f"ghcr.io/harnessgateway/harnessgateway:{version}", str),
            "sandbox_image": ("HG_SANDBOX_IMAGE", f"ghcr.io/harnessgateway/sandbox:{version}", str),
            "internal_url": _INTERNAL_URL,
            "port": ("HG_PORT", "8000", _port),
            "internal_port": ("HG_INTERNAL_PORT", "8001", _port),
            "max_turns": ("HG_MAX_TURNS", "8", number(1)),
            "volume_ttl_s": ("HG_VOLUME_TTL_S", "3600", number(0)),
            "private_hosts": _PRIVATE_HOSTS,
            "extra_ca": _EXTRA_CA,
            "extensions": ("HG_EXTENSIONS", "", _extensions),
            "log_level": _LOG_LEVEL,
        }))


@dataclass(frozen=True)
class ProxySettings:
    """What the egress proxy needs: it holds no database address and no sealing key."""

    proxy_key: str
    internal_url: str
    port: int
    private_hosts: tuple[HostEntry, ...]
    extra_ca: str
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ProxySettings":
        return cls(**read(env, {
            "proxy_key": ("HG_PROXY_KEY", None, str),
            "internal_url": _INTERNAL_URL,
            "port": ("HG_PROXY_PORT", "8080", _port),
            "private_hosts": _PRIVATE_HOSTS,
            "extra_ca": _EXTRA_CA,
            "log_level": _LOG_LEVEL,
        }))
