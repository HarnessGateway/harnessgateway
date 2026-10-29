"""KeyWrapper with the master key derived from HG_SECRET_KEY: AES-256-GCM, the context as associated data."""
from __future__ import annotations

import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class LocalKeys:
    def __init__(self, master: bytes):
        assert len(master) == 32
        self._aead = AESGCM(master)

    def wrap(self, key: bytes, context: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aead.encrypt(nonce, key, context.encode())

    def unwrap(self, wrapped: bytes, context: str) -> bytes:
        return self._aead.decrypt(wrapped[:12], wrapped[12:], context.encode())
