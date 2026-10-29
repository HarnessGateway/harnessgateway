"""Sealing secrets and signing short-lived links (SPEC §8.1, §16).

Each organization has a data key, wrapped by the key wrapper port. A value is sealed with AES-256-GCM under its
organization's data key, with the record it belongs to as associated data, so a sealed value cannot be moved to
another record or another organization. HG_SECRET_KEY derives, by HKDF with a label each, the local master key, the
pepper for token digests and the link-signing key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .ports.keys import KeyWrapper

SEALED = "v1:"


def derive(secret_key: bytes, label: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label.encode()).derive(secret_key)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text)


class DataKeys:
    """Creates and opens organizations' data keys through the wrapper, keeping opened keys in memory briefly."""
    CACHE_S = 300

    def __init__(self, wrapper: KeyWrapper):
        self._wrapper = wrapper
        self._cache: dict[str, tuple[float, bytes]] = {}

    def new(self, org_id: str) -> str:
        return _b64(self._wrapper.wrap(AESGCM.generate_key(256), f"org:{org_id}"))

    def open(self, org_id: str, wrapped: str) -> bytes:
        hit = self._cache.get(org_id)
        if hit and time.monotonic() - hit[0] < self.CACHE_S:
            return hit[1]
        key = self._wrapper.unwrap(_unb64(wrapped), f"org:{org_id}")
        self._cache[org_id] = (time.monotonic(), key)
        return key


def seal(data_key: bytes, value: str, context: str) -> str:
    nonce = secrets.token_bytes(12)
    return SEALED + _b64(nonce + AESGCM(data_key).encrypt(nonce, value.encode(), context.encode()))


def unseal(data_key: bytes, sealed: str, context: str) -> str:
    assert sealed.startswith(SEALED), "a sealed value"
    raw = _unb64(sealed[len(SEALED):])
    return AESGCM(data_key).decrypt(raw[:12], raw[12:], context.encode()).decode()


class Signer:
    """Capability tokens for blob transfers: an HMAC over the key, the method and the expiry."""

    def __init__(self, secret_key: bytes):
        self._key = derive(secret_key, "harnessgateway/links")

    def sign(self, key: str, method: str, ttl_s: int) -> str:
        body = json.dumps({"k": key, "m": method, "e": int(time.time()) + ttl_s}, separators=(",", ":")).encode()
        return _b64(body) + "." + _b64(hmac.new(self._key, body, hashlib.sha256).digest())

    def verify(self, token: str, method: str) -> str | None:
        try:
            body_b64, mac_b64 = token.split(".", 1)
            body = _unb64(body_b64)
            if not hmac.compare_digest(_unb64(mac_b64), hmac.new(self._key, body, hashlib.sha256).digest()):
                return None
            doc = json.loads(body)
        except (ValueError, TypeError):
            return None
        if doc.get("m") != method or doc.get("e", 0) < time.time():
            return None
        return str(doc["k"])
