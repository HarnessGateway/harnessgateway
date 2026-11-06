"""Egress entries and policies (SPEC §8.2): the one rule of what a box may reach, used by the api (to check a request's
MCP servers and to compose a turn's policy) and the door (to decide each connection).

An entry is `host`, `*.domain` or `*`, with an optional `:port` (443 when absent); `!` in front makes it a deny."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"(\*|(\*\.)?{_LABEL}(\.{_LABEL})+)")
DEFAULT_PORT = 443


@dataclass(frozen=True)
class Entry:
    pattern: str
    port: int
    deny: bool = False

    @classmethod
    def parse(cls, text: object) -> Entry:
        if not isinstance(text, str):
            raise ValueError(f"an egress entry is a string, not {text!r}")
        raw = text.strip().lower()
        deny = raw.startswith("!")
        body = raw[1:] if deny else raw
        host, sep, port_s = body.rpartition(":") if ":" in body else (body, "", "")
        port = DEFAULT_PORT
        if sep:
            if not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
                raise ValueError(f"an egress entry's port is 1 to 65535: {text!r}")
            port = int(port_s)
        if len(host) > 253 or not _HOST.fullmatch(host):
            raise ValueError(f"an egress entry is a hostname, *.domain or *, with an optional :port and an optional "
                             f"leading ! to deny, not {text!r}")
        return cls(host, port, deny)

    def matches(self, host: str, port: int) -> bool:
        if port != self.port:
            return False
        if self.pattern == "*":
            return True
        if self.pattern.startswith("*."):
            return host.endswith(self.pattern[1:])      # a subdomain at any depth, never the domain or a look-alike
        return host == self.pattern

    def __str__(self) -> str:
        return ("!" if self.deny else "") + self.pattern + ("" if self.port == DEFAULT_PORT else f":{self.port}")


def entries(values: Iterable[object]) -> list[str]:
    """Validated, normalized, de-duplicated entry strings (ValueError names the first bad one)."""
    return sorted({str(Entry.parse(v)) for v in values})


def host_entry(url_host: str, port: int | None) -> str:
    """The allow entry for one URL's host and port."""
    return url_host.lower() + ("" if port in (None, DEFAULT_PORT) else f":{port}")


@dataclass(frozen=True)
class Policy:
    """Allowed when some allow entry matches and no deny entry does, or the host is a platform host: the api as boxes
    reach it (`host:port`, exact, possibly a single label on a private network), which no deny entry can remove."""
    allow: tuple[Entry, ...] = ()
    deny: tuple[Entry, ...] = ()
    platform: tuple[tuple[str, int], ...] = ()

    @classmethod
    def of(cls, *levels: Iterable[str], platform: Iterable[str] = ()) -> Policy:
        parsed = [Entry.parse(v) for level in levels for v in level]
        hosts = tuple(_platform_host(v) for v in platform)
        return cls(tuple(e for e in parsed if not e.deny), tuple(e for e in parsed if e.deny), hosts)

    def is_platform(self, host: str, port: int) -> bool:
        return (host.lower().rstrip("."), port) in self.platform

    def permits(self, host: str, port: int) -> bool:
        host = host.lower().rstrip(".")
        if (host, port) in self.platform:
            return True
        return any(e.matches(host, port) for e in self.allow) and not any(e.matches(host, port) for e in self.deny)

    def entries(self) -> list[str]:
        return sorted({str(e) for e in (*self.allow, *self.deny)})

    def platform_entries(self) -> list[str]:
        return sorted(f"{h}:{p}" for h, p in self.platform)


def _platform_host(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit() or not host or len(host) > 253:
        raise ValueError(f"a platform host is host:port, not {value!r}")
    return host.lower(), int(port)
