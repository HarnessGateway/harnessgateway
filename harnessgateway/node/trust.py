"""The node's certificate authority, for the doors' interception of secret-bound hosts (SPEC §8.2), and the trust
bundles boxes see. The CA's key stays on the node and in its doors; a box gets only certificates."""
from __future__ import annotations

import datetime
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

DOOR_UID = 65534


class Trust:
    def __init__(self, node_dir: Path, node_name: str, operator_ca: str):
        self.ca_dir, self.trust_dir = node_dir / "ca", node_dir / "trust"
        self.node_name, self.operator_ca = node_name, operator_ca

    @property
    def bundle(self) -> Path:
        """Every CA a box trusts: the image's own, the node's, the operator's."""
        return self.trust_dir / "bundle.pem"

    @property
    def extra(self) -> Path:
        """Only the node's and the operator's CAs, for runtimes that add to their own store (NODE_EXTRA_CA_CERTS)."""
        return self.trust_dir / "extra.pem"

    def ensure(self, image_bundle: bytes) -> None:
        self.ca_dir.mkdir(parents=True, exist_ok=True)
        self.trust_dir.mkdir(parents=True, exist_ok=True)
        key_file, cert_file = self.ca_dir / "ca.key", self.ca_dir / "ca.crt"
        if not (key_file.is_file() and cert_file.is_file()):
            key, cert = self._new_ca()
            key_file.write_bytes(key)
            cert_file.write_bytes(cert)
        os.chown(self.ca_dir, DOOR_UID, DOOR_UID)
        self.ca_dir.chmod(0o500)
        for f in (key_file, cert_file):
            os.chown(f, DOOR_UID, DOOR_UID)
            f.chmod(0o400)
        ours = cert_file.read_bytes() + (Path(self.operator_ca).read_bytes() if self.operator_ca else b"")
        self.extra.write_bytes(ours)
        self.bundle.write_bytes(image_bundle.rstrip(b"\n") + b"\n" + ours)
        for f in (self.extra, self.bundle):
            f.chmod(0o644)

    def _new_ca(self) -> tuple[bytes, bytes]:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"HarnessGateway node {self.node_name}"[:64])])
        now = datetime.datetime.now(datetime.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(hours=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                             content_commitment=False, key_encipherment=False, data_encipherment=False,
                                             key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()), cert.public_bytes(serialization.Encoding.PEM))
