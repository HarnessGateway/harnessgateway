"""Layer 0: names and identifiers. A name is lowercase letters, digits, dots, dashes and underscores,
at most 63, starting with a letter or digit; the same rule for every kind of thing a member names.
An id carries its kind as a prefix (`resp_`, `sess_`), so a wrong kind is refused by
shape before any lookup."""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from ..errors import Invalid

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
HEX12 = re.compile(r"[0-9a-f]{12}")


def check_name(name: Any, kind: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Invalid(f"a {kind} name is lowercase letters, digits, dots, dashes and underscores, at most 63, "
                      f"starting with a letter or digit: {name!r}")
    return name


def is_name(name: Any) -> bool:
    return isinstance(name, str) and bool(NAME.fullmatch(name))


def digest_of(secret: str) -> str:
    """What is kept of a secret: its SHA-256, in hex."""
    return hashlib.sha256(secret.encode()).hexdigest()


def new_id(prefix: str) -> str:
    """A fresh id of a kind: `resp_` and 32 hex digits."""
    return f"{prefix}_{uuid.uuid4().hex}"


def short_id() -> str:
    return uuid.uuid4().hex[:12]


def is_id(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(f"{prefix}_") and len(value) > len(prefix) + 1
