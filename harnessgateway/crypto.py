"""Secrets at rest: envelope encryption. Every sealed value gets its own random data key (AES-256-GCM); the data key is
wrapped by the master key, which is either derived from HG_SECRET_KEY (self-hosted) or held in AWS KMS (SaaS) and never
leaves it. The record's identity is the associated data, so a sealed value cannot be moved to another record.

HG_SECRET_KEY also derives the pepper for secret digests and the key that signs node blob URLs, each by HKDF with its
own label, so no two purposes share key material."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "v1:"


def derive(secret_key: bytes, label: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label.encode()).derive(secret_key)


class KeyWrapper(Protocol):
    def wrap(self, dek: bytes, context: str) -> bytes: ...
    def unwrap(self, wrapped: bytes, context: str) -> bytes: ...


class LocalWrapper:
    """The master key derived from HG_SECRET_KEY."""

    def __init__(self, secret_key: bytes):
        self._aead = AESGCM(derive(secret_key, "harnessgateway/seal"))

    def wrap(self, dek: bytes, context: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aead.encrypt(nonce, dek, context.encode())

    def unwrap(self, wrapped: bytes, context: str) -> bytes:
        return self._aead.decrypt(wrapped[:12], wrapped[12:], context.encode())


class KmsWrapper:
    """The master key in AWS KMS: data keys are wrapped with Encrypt and unwrapped with Decrypt, bound to the record by
    the encryption context. The process needs kms:Encrypt and kms:Decrypt on HG_KMS_KEY_ID and nothing else."""

    def __init__(self, key_id: str):
        import boto3
        self._kms, self._key_id = boto3.client("kms"), key_id

    def wrap(self, dek: bytes, context: str) -> bytes:
        return self._kms.encrypt(KeyId=self._key_id, Plaintext=dek, EncryptionContext={"record": context})["CiphertextBlob"]

    def unwrap(self, wrapped: bytes, context: str) -> bytes:
        return self._kms.decrypt(CiphertextBlob=wrapped, EncryptionContext={"record": context})["Plaintext"]


class Sealer:
    def __init__(self, wrapper: KeyWrapper):
        self._wrapper = wrapper

    def seal(self, value: str, context: str) -> str:
        dek, nonce = AESGCM.generate_key(256), secrets.token_bytes(12)
        doc = {"k": _b64(self._wrapper.wrap(dek, context)), "n": _b64(nonce),
               "c": _b64(AESGCM(dek).encrypt(nonce, value.encode(), context.encode()))}
        return PREFIX + _b64(json.dumps(doc, separators=(",", ":")).encode())

    def open(self, sealed: str, context: str) -> str:
        if not sealed.startswith(PREFIX):
            raise ValueError("not a sealed value")
        doc = json.loads(base64.urlsafe_b64decode(sealed[len(PREFIX):]))
        dek = self._wrapper.unwrap(base64.urlsafe_b64decode(doc["k"]), context)
        return AESGCM(dek).decrypt(base64.urlsafe_b64decode(doc["n"]), base64.urlsafe_b64decode(doc["c"]),
                                   context.encode()).decode()


class UrlSigner:
    """Short-lived capability tokens for blob transfers between nodes and the API: an HMAC over the object key, the
    allowed method and the expiry. The token is the only authorization the blob route needs."""

    def __init__(self, secret_key: bytes):
        self._key = derive(secret_key, "harnessgateway/blob-url")

    def sign(self, key: str, method: str, ttl_s: int) -> str:
        body = json.dumps({"k": key, "m": method, "e": int(time.time()) + ttl_s}, separators=(",", ":")).encode()
        mac = hmac.new(self._key, body, hashlib.sha256).digest()
        return _b64(body) + "." + _b64(mac)

    def verify(self, token: str, method: str) -> str | None:
        try:
            body_b64, mac_b64 = token.split(".", 1)
            body = base64.urlsafe_b64decode(body_b64)
            if not hmac.compare_digest(base64.urlsafe_b64decode(mac_b64),
                                       hmac.new(self._key, body, hashlib.sha256).digest()):
                return None
            doc = json.loads(body)
        except (ValueError, TypeError):
            return None
        if doc.get("m") != method or doc.get("e", 0) < time.time():
            return None
        return str(doc["k"])


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def build_sealer(secret_key: bytes, kms_key_id: str) -> Sealer:
    return Sealer(KmsWrapper(kms_key_id) if kms_key_id else LocalWrapper(secret_key))
