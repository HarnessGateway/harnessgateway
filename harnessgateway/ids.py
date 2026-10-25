"""Identifiers and secrets. Every id carries its kind as a prefix, so an id of the wrong kind is refused by its shape
before any lookup. Secrets are high-entropy tokens kept only as a keyed digest."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from .errors import Invalid, NotFound

KINDS = {
    "org": "org", "member": "mem", "key": "key", "provider": "prv", "install": "hns", "session": "cntr",
    "turn": "resp", "file": "file", "node": "node", "invite": "inv", "op": "cop", "jointoken": "njt",
}
_SHAPE = re.compile(r"[a-z]+_[A-Za-z0-9]{8,64}")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
SECRET_PREFIXES = {"key": "sk-hg-", "node": "hgn_", "turn": "hgt_", "invite": "hgi_", "jointoken": "hgj_"}


def new(kind: str) -> str:
    """A fresh id of a kind, e.g. `resp_` and 32 hex digits."""
    return f"{KINDS[kind]}_{secrets.token_hex(16)}"


def check(value: object, kind: str) -> str:
    """The id if it has the kind's shape; otherwise NotFound, as if it named nothing (no shape oracle)."""
    if not isinstance(value, str) or not _SHAPE.fullmatch(value) or not value.startswith(KINDS[kind] + "_"):
        raise NotFound(f"no {kind} {value!r}")
    return value


def check_name(value: object, what: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise Invalid(f"a {what} name is lowercase letters, digits, dots, dashes and underscores, at most 63 characters, "
                      f"starting with a letter or digit: {value!r}", param="name")
    return value


def new_secret(kind: str) -> str:
    return SECRET_PREFIXES[kind] + secrets.token_urlsafe(32)


def digest(secret: str, pepper: bytes) -> str:
    """What is kept of a secret: HMAC-SHA256 under the server pepper, so a stolen table cannot be brute-forced offline."""
    return hmac.new(pepper, secret.encode(), hashlib.sha256).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
