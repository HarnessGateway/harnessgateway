"""The installation's certificate authority.

To put a secret into a request the egress proxy must read the request, so for a host a secret is bound to it
answers with a certificate of this authority in place of the host's own. A sandbox trusts it because the
authority's certificate is in the bundle every sandbox is given. Its certificates carry every identifier a strict
verifier asks for (key identifiers, key usage): recent TLS clients refuse anything less.
"""

import datetime
import ipaddress
import ssl
import tempfile
import time
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_AUTHORITY_DAYS = 3650
_HOST_HOURS = 24
_NAME = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HarnessGateway"),
                   x509.NameAttribute(NameOID.COMMON_NAME, "HarnessGateway egress authority")])


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _certificate(subject: x509.Name, public: ec.EllipticCurvePublicKey, lasts: datetime.timedelta,
                 *, authority: bool) -> x509.CertificateBuilder:
    """What both kinds of certificate share: who it is for, how long, and what its key may do."""
    now = datetime.datetime.now(datetime.UTC)
    return (x509.CertificateBuilder().subject_name(subject).public_key(public)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))        # clocks disagree a little
            .not_valid_after(now + lasts)
            .add_extension(x509.BasicConstraints(ca=authority, path_length=0 if authority else None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=authority, crl_sign=authority, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False, encipher_only=False,
                decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False))


def new_authority() -> tuple[str, str]:
    """A new authority: its certificate and its private key, both as PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (_certificate(_NAME, key.public_key(), datetime.timedelta(days=_AUTHORITY_DAYS), authority=True)
                   .issuer_name(_NAME).sign(key, hashes.SHA256()))
    return certificate.public_bytes(serialization.Encoding.PEM).decode(), _pem(key)


def trust_bundle(authority_certificate: str) -> str:
    """What a sandbox trusts: the public roots, and the installation's own authority."""
    return f"{certifi.contents().rstrip()}\n\n# HarnessGateway egress authority\n{authority_certificate.strip()}\n"


class Authority:
    """Makes, on demand, the TLS identity the proxy shows a sandbox in place of a host's own."""

    def __init__(self, certificate_pem: str, key_pem: str) -> None:
        self._certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        self._key = key
        self._host_key = ec.generate_private_key(ec.SECP256R1())       # one key serves every host's certificate
        self._contexts: dict[str, tuple[float, ssl.SSLContext]] = {}   # by host: until when it is good, and it

    def context_for(self, host: str) -> ssl.SSLContext:
        """A server-side TLS context that proves to be `host`, to whoever trusts this authority."""
        kept = self._contexts.get(host)
        if kept and kept[0] > time.monotonic():
            return kept[1]
        try:
            name: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            name = x509.DNSName(host)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])])
        certificate = (
            _certificate(subject, self._host_key.public_key(), datetime.timedelta(hours=_HOST_HOURS), authority=False)
            .issuer_name(self._certificate.subject)
            .add_extension(x509.SubjectAlternativeName([name]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with tempfile.TemporaryDirectory(prefix="hg-proxy-") as folder:     # the ssl module reads an identity
            chain, key = Path(folder, "chain.pem"), Path(folder, "key.pem")  # from files only
            chain.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key.touch(mode=0o600)
            key.write_text(_pem(self._host_key))
            context.load_cert_chain(str(chain), str(key))
        context.set_alpn_protocols(["http/1.1"])                # what the proxy reads is HTTP/1.1
        self._contexts[host] = (time.monotonic() + (_HOST_HOURS - 1) * 3600, context)
        return context
