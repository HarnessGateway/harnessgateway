"""Where the platform itself may connect on a tenant's behalf (provider endpoints, git package sources). Only public
addresses, checked after resolution, so a name cannot point the platform at its own network. Self-hosted operators
whose providers live on a private network allow private addresses explicitly."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from .errors import Invalid


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except socket.gaierror:
        return []


def check_url(url: str, *, allow_private: bool, schemes: tuple[str, ...] = ("https",)) -> str:
    """The URL, normalized without a trailing slash, if its scheme is allowed and its host resolves only to addresses
    the platform may reach."""
    parts = urlsplit(url.strip())
    allowed = schemes + (("http",) if allow_private and "https" in schemes else ())
    if parts.scheme not in allowed or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise Invalid(f"a URL with scheme {' or '.join(allowed)}, a host and no credentials, query or fragment", param="url")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = resolve(parts.hostname, port)
    if not addresses:
        raise Invalid(f"{parts.hostname} does not resolve", param="url")
    if not allow_private and not all(is_public(a) for a in addresses):
        raise Invalid(f"{parts.hostname} resolves to a private address", param="url")
    return url.strip().rstrip("/")
