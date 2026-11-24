"""Secrets at rest: sealed under the installation's key, each bound to the place it belongs."""

import base64
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12


class Sealer(Protocol):
    """What seals a secret. `where` names the place it belongs (organization, kind, name, …): a sealed value
    moved anywhere else must not open. An edition may supply one that keeps its key in a key service."""

    def seal(self, value: str, *where: str) -> str: ...

    def open(self, sealed: str, *where: str) -> str: ...


class KeySealer:
    """Seals with AES-256-GCM under a key the process is given."""

    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def seal(self, value: str, *where: str) -> str:
        nonce = os.urandom(_NONCE)
        return base64.b64encode(nonce + self._aead.encrypt(nonce, value.encode(), "\x00".join(where).encode())).decode()

    def open(self, sealed: str, *where: str) -> str:
        try:
            raw = base64.b64decode(sealed, validate=True)
            return self._aead.decrypt(raw[:_NONCE], raw[_NONCE:], "\x00".join(where).encode()).decode()
        except (InvalidTag, ValueError):
            raise ValueError(f"the sealed value at {'/'.join(where)} does not open with this key") from None
