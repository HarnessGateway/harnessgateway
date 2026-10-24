"""Layer 0: the vault. A provider's key is sealed the moment it arrives and opened only at the moment
a run's egress door is started, in memory, for that one run. At rest the server holds ciphertext.

AES-256-GCM under one master key, with the record's path as associated data, so a sealed value
cannot be moved to another record. The master key comes from the environment (HG_MASTER_KEY, 64 hex
digits) or a file (HG_MASTER_KEY_FILE); in a hosted deployment that is a secret a key service
provides, on a self-hosted box a file `init` wrote with mode 600.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Mapping, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "v1:"
NONCE = 12


class Sealer(Protocol):
    """What seals a secret for one record and opens it again; what derives a secondary secret for a
    purpose. The local vault holds the master key itself; a KMS backend holds it in the service."""

    def seal(self, value: str, aad: str) -> str: ...
    def open(self, sealed: str, aad: str) -> str: ...
    def derive(self, label: str) -> bytes: ...


class Vault:
    def __init__(self, master: bytes):
        if len(master) != 32:
            raise ValueError("a master key is 32 bytes")
        self._master = master
        self._aead = AESGCM(master)

    def derive(self, label: str) -> bytes:
        """A secondary secret for one purpose (`label`), from the master key; the same every time."""
        return hmac.new(self._master, label.encode(), hashlib.sha256).digest()

    @staticmethod
    def generate() -> str:
        """A new master key, as the 64 hex digits HG_MASTER_KEY takes."""
        return secrets.token_hex(32)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Vault:
        env = os.environ if env is None else env
        if value := env.get("HG_MASTER_KEY"):
            return cls(bytes.fromhex(value.strip()))
        if path := env.get("HG_MASTER_KEY_FILE"):
            return cls(bytes.fromhex(Path(path).read_text().strip()))
        raise RuntimeError("set HG_MASTER_KEY (64 hex digits) or HG_MASTER_KEY_FILE (a file holding them)")

    def seal(self, value: str, aad: str) -> str:
        nonce = secrets.token_bytes(NONCE)
        return PREFIX + base64.b64encode(nonce + self._aead.encrypt(nonce, value.encode(), aad.encode())).decode()

    def open(self, sealed: str, aad: str) -> str:
        if not sealed.startswith(PREFIX):
            raise ValueError("not a sealed value")
        blob = base64.b64decode(sealed[len(PREFIX):])
        return self._aead.decrypt(blob[:NONCE], blob[NONCE:], aad.encode()).decode()
