"""Where the platform itself connects on an organization's behalf (provider endpoints, git package sources): public
addresses only, checked after resolution, unless the operator allows private ones (HG_ALLOW_PRIVATE_EGRESS)."""
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
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except socket.gaierror:
        return []


def check_url(url: str, *, allow_private: bool, param: str = "url") -> str:
    """The URL without a trailing slash, if it is https (http too with private addresses allowed), names a host, carries
    no credentials, query or fragment, and resolves only to addresses the platform may reach."""
    parts = urlsplit(url.strip() if isinstance(url, str) else "")
    schemes = ("https", "http") if allow_private else ("https",)
    if parts.scheme not in schemes or not parts.hostname or parts.username or parts.password or parts.query or \
            parts.fragment:
        raise Invalid(f"a URL with scheme {' or '.join(schemes)}, a host and no credentials, query or fragment",
                      param=param)
    addresses = resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    if not addresses:
        raise Invalid(f"{parts.hostname} does not resolve", param=param)
    if not allow_private and not all(is_public(a) for a in addresses):
        raise Invalid(f"{parts.hostname} resolves to a private address", param=param)
    return url.strip().rstrip("/")
