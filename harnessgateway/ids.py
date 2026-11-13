"""Making identifiers: a type prefix and a ULID (spec §4). They sort by when they were made."""

import secrets
import time

from harnessgateway.domain.names import PREFIXES

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _base32(number: int, length: int) -> str:
    return "".join(_ALPHABET[(number >> (5 * i)) & 31] for i in reversed(range(length)))


def ulid() -> str:
    """26 characters that sort by when they were made: 48 bits of milliseconds, then 80 random bits."""
    return f"{_base32(time.time_ns() // 1_000_000, 10)}{_base32(secrets.randbits(80), 16)}"


def new_id(kind: str) -> str:
    """A new identifier of `kind`: its prefix and a ULID."""
    return f"{PREFIXES[kind]}_{ulid()}"
