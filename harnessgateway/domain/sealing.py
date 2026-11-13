"""Keeping secrets: sealing at rest, placeholders in a sandbox, and hashing keys (spec §10.4, §10.5, §16)."""

import hashlib
import hmac
import os
import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12
_TAG = 16


class SealError(Exception):
    """A sealed value could not be opened: wrong key, wrong place, or damaged."""


class Sealer:
    """Encrypts secret values under the platform's key, bound to the place they belong."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"the secret key is exactly 32 bytes, not {len(key)}")
        self._aead = AESGCM(key)

    @staticmethod
    def _binding(org: str, kind: str, name: str) -> bytes:
        return "\x00".join((org, kind, name)).encode()

    def seal(self, value: str, *, org: str, kind: str, name: str) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + self._aead.encrypt(nonce, value.encode(), self._binding(org, kind, name))

    def open(self, sealed: bytes, *, org: str, kind: str, name: str) -> str:
        if len(sealed) < _NONCE + _TAG:
            raise SealError("the sealed value is too short to be one")
        try:
            return self._aead.decrypt(sealed[:_NONCE], sealed[_NONCE:], self._binding(org, kind, name)).decode()
        except InvalidTag:
            raise SealError(f"the sealed {kind} value {name!r} does not open here") from None


def new_placeholder() -> str:
    """What a sandbox holds in place of a secret: a string that is worth nothing outside the egress proxy."""
    return f"hgs_{secrets.token_hex(16)}"


def substitute(text: str, values: Mapping[str, str]) -> str:
    """`text` with each placeholder in `values` replaced by its secret."""
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def keyed_hash(key: bytes, value: str) -> str:
    """What is stored in place of an API key or a token: it can be checked, never read back."""
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
