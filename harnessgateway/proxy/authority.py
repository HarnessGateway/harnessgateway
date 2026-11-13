"""The platform's certificate authority (spec §11.5).

It signs a certificate for each host whose traffic the egress proxy must read in order to put a secret into
it. A sandbox trusts it because its certificate is in the bundle mounted at /etc/hg/ca.pem.
"""

import datetime
import ipaddress
import ssl
import time
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

AUTHORITY_DAYS = 3650
HOST_HOURS = 24
_NAME = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HarnessGateway"),
                   x509.NameAttribute(NameOID.COMMON_NAME, "HarnessGateway egress authority")])


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def new_authority() -> tuple[str, str]:
    """A new authority: its certificate and its private key, both as PEM text."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_NAME).issuer_name(_NAME).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=AUTHORITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False, encipher_only=False,
            decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode(), _pem(key)


def bundle(authority_cert: str, *, extra: str = "") -> str:
    """What a sandbox trusts: the public roots, the platform's own authority, and any the operator added."""
    parts = [certifi.contents().rstrip(), f"# HarnessGateway egress authority\n{authority_cert.strip()}"]
    if extra.strip():
        parts.append(f"# Added by the operator (HG_EXTRA_CA)\n{extra.strip()}")
    return "\n\n".join(parts) + "\n"


class Authority:
    """Makes, on demand, the TLS identity the proxy shows a sandbox in place of a host's own."""

    def __init__(self, cert_pem: str, key_pem: str, folder: Path) -> None:
        self._cert = x509.load_pem_x509_certificate(cert_pem.encode())
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        self._key = key
        self._folder = folder                                   # the ssl module reads identities from files only
        folder.mkdir(parents=True, exist_ok=True)
        self._host_key = ec.generate_private_key(ec.SECP256R1())           # one key for every host's certificate
        self._host_key_file = folder / "host.key"
        self._host_key_file.touch(mode=0o600)
        self._host_key_file.write_text(_pem(self._host_key))
        self._contexts: dict[str, tuple[float, ssl.SSLContext]] = {}

    def context_for(self, host: str) -> ssl.SSLContext:
        """A server-side TLS context that proves to be `host`, to whoever trusts this authority."""
        kept = self._contexts.get(host)
        if kept and kept[0] > time.monotonic():
            return kept[1]
        try:
            name: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            name = x509.DNSName(host)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
            .issuer_name(self._cert.subject).public_key(self._host_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(minutes=5))
            .not_valid_after(_now() + datetime.timedelta(hours=HOST_HOURS))
            .add_extension(x509.SubjectAlternativeName([name]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=False, crl_sign=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False, encipher_only=False,
                decipher_only=False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._host_key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        file = self._folder / f"{cert.serial_number:x}.pem"
        file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(file), str(self._host_key_file))
        context.set_alpn_protocols(["http/1.1"])                # interception speaks HTTP/1.1
        file.unlink()
        self._contexts[host] = (time.monotonic() + (HOST_HOURS - 1) * 3600, context)
        return context
