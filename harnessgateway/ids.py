"""Identifiers and secret tokens. An id carries its kind as a prefix, so an id of the wrong kind is refused by its
shape before any lookup. A secret is a high-entropy token kept only as a keyed digest."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from .domain.names import NAME
from .errors import Invalid, NotFound

PREFIX = {"org": "org", "key": "key", "provider": "prv", "harness": "hns", "session": "cntr", "turn": "resp",
          "file": "file", "node": "node", "op": "cop", "node_token": "njt", "usage": "use", "trace": "trc"}
SECRET = {"key": "hg_", "node": "hgn_", "node_token": "hgj_", "turn": "hgt_", "door": "hgd_", "placeholder": "hgs_"}
_SHAPE = re.compile(r"[a-z]+_[0-9a-f]{32}")
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def new(kind: str) -> str:
    return f"{PREFIX[kind]}_{secrets.token_hex(16)}"


def check(value: object, kind: str) -> str:
    """The id when it has the kind's shape; otherwise NotFound, as if it named nothing."""
    if not isinstance(value, str) or not _SHAPE.fullmatch(value) or not value.startswith(PREFIX[kind] + "_"):
        raise NotFound(f"no {kind.replace('_', ' ')} {value!r}")
    return value


def check_name(value: object, what: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise Invalid(f"a {what} name is lowercase letters, digits, dots, dashes and underscores, at most 63 characters, "
                      f"starting with a letter or digit, not {value!r}", param="name")
    return value


def new_secret(kind: str) -> str:
    if kind == "placeholder":
        return SECRET[kind] + secrets.token_hex(16)
    return SECRET[kind] + "".join(secrets.choice(_ALPHABET) for _ in range(40))


def digest(secret: str, pepper: bytes) -> str:
    """HMAC-SHA256 under the server's pepper: what is kept of a secret token."""
    return hmac.new(pepper, secret.encode(), hashlib.sha256).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
