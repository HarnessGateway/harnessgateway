"""Sealing secrets and the door's certificate authority (SPEC §8.3).

Each organization has a data key, wrapped by the key wrapper port. A value is sealed with AES-256-GCM under its
organization's data key, with the record it belongs to as associated data, so a sealed value cannot be moved to
another record or another organization. HG_SECRET_KEY derives, by HKDF with a label each, the local master key, the
pepper for token digests, and the door's CA key, so every door and api process computes the same CA with no shared
file."""
from __future__ import annotations

import base64
import datetime
import secrets
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

from .ports.keys import KeyWrapper

SEALED = "v1:"
CA_NOT_BEFORE = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
CA_NOT_AFTER = datetime.datetime(2046, 1, 1, tzinfo=datetime.UTC)
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


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


class DoorCA:
    """The certificate authority doors issue interception certificates from: a P-256 key derived from the secret key,
    and a self-signed certificate with fixed dates. Every process derives the same key, subject and public key, which
    is what chain validation in a box depends on; the self-signature itself is randomized and may differ (SPEC §8.3)."""

    def __init__(self, secret_key: bytes):
        scalar = int.from_bytes(derive(secret_key, "harnessgateway/door-ca"), "big") % (_P256_ORDER - 1) + 1
        self.key = ec.derive_private_key(scalar, ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HarnessGateway door")])
        self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.key.public_key())
                     .serial_number(1).not_valid_before(CA_NOT_BEFORE).not_valid_after(CA_NOT_AFTER)
                     .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                     .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                  content_commitment=False, key_encipherment=False, data_encipherment=False,
                                                  key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
                     .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
                     .sign(self.key, hashes.SHA256()))

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        return self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
