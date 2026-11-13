"""Host entries, the egress decision, and the addresses that are never reached (spec §9.3, §11.3, §11.4)."""

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from harnessgateway.errors import Invalid

DEFAULT_PORTS = (80, 443)
ENTRIES_MAX = 256

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(\*\.)?{_LABEL}(\.{_LABEL})*")
_METADATA = ipaddress.ip_address("169.254.169.254")


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class HostEntry:
    """One line of an allow or deny list: `host`, `*.suffix`, or either with `:port`."""

    host: str
    port: int | None = None

    @classmethod
    def parse(cls, value: object) -> "HostEntry":
        if not isinstance(value, str) or not value or value != value.strip():
            raise Invalid(f"a host entry is host, *.suffix or host:port: {value!r}")
        text = value.lower()
        host, colon, port_text = text.rpartition(":")
        if not colon or _address(text) is not None:          # no port, or a bare IPv6 address
            host, port_text = text, ""
        port = None
        if port_text:
            if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
                raise Invalid(f"a port is 1 to 65535: {value!r}")
            port = int(port_text)
        if _address(host) is None and not _HOSTNAME.fullmatch(host):
            raise Invalid(f"a host entry is host, *.suffix or host:port, with no scheme or path: {value!r}")
        return cls(host, port)

    def __str__(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"

    def matches(self, host: str, port: int) -> bool:
        if port not in ((self.port,) if self.port is not None else DEFAULT_PORTS):
            return False
        host = host.lower()
        if self.host.startswith("*."):
            return _address(host) is None and host.endswith(self.host[1:])
        return host == self.host


def parse_entries(values: object, what: str, limit: int = ENTRIES_MAX) -> tuple[HostEntry, ...]:
    if not isinstance(values, list | tuple):
        raise Invalid(f"{what} is a list of host entries", param=what)
    if len(values) > limit:
        raise Invalid(f"{what} holds at most {limit} entries, not {len(values)}", param=what)
    try:
        return tuple(HostEntry.parse(v) for v in values)
    except Invalid as e:
        raise Invalid(f"{what}: {e.message}", param=what) from None


def _hosts(names: Iterable[str]) -> tuple[HostEntry, ...]:
    return tuple(HostEntry(name) for name in names)


PACKAGE_MANAGER_HOSTS = _hosts((
    "registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org", "github.com",
    "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com",
    "release-assets.githubusercontent.com", "crates.io", "static.crates.io", "index.crates.io",
    "proxy.golang.org", "sum.golang.org", "rubygems.org", "repo.maven.apache.org", "repo1.maven.org",
    "deb.debian.org", "security.debian.org", "archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com",
    "dl-cdn.alpinelinux.org", "astral.sh", "nodejs.org",
))


def is_forbidden_address(text: str) -> bool:
    """Whether a resolved address must never be connected to, whatever a policy allows."""
    address = ipaddress.ip_address(text)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address == _METADATA
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class EgressPolicy:
    """What one turn's sandbox may connect to."""

    mode: Literal["none", "limited", "open"]
    allow: tuple[HostEntry, ...]
    deny: tuple[HostEntry, ...]

    def decide(self, host: str, port: int) -> Decision:
        where = f"{host}:{port}"
        if self.mode == "none":
            return Decision(False, f"{where}: this environment has no network")
        if any(entry.matches(host, port) for entry in self.deny):
            return Decision(False, f"{where} is on the environment's deny list")
        if self.mode == "open" or any(entry.matches(host, port) for entry in self.allow):
            return Decision(True)
        return Decision(False, f"{where} is not allowed: add {host} to the environment's network.allow")
