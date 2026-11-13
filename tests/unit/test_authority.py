"""Spec §11.5: the platform's certificate authority, and the certificates it makes for intercepted hosts."""

import asyncio
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from harnessgateway.proxy.authority import Authority, bundle, new_authority


def test_a_new_authority_is_a_self_signed_ec_certificate_that_may_sign_others() -> None:
    cert_pem, key_pem = new_authority()
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
    assert cert.public_key().curve.name == "secp256r1"  # type: ignore[union-attr]
    assert cert.issuer == cert.subject
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert "BEGIN PRIVATE KEY" in key_pem
    assert new_authority()[0] != cert_pem


def test_the_bundle_is_the_public_roots_and_the_platform_s_own(tmp_path: Path) -> None:
    cert_pem, _ = new_authority()
    text = bundle(cert_pem)
    assert text.count("BEGIN CERTIFICATE") > 50
    assert cert_pem.strip() in text
    file = tmp_path / "ca.pem"
    file.write_text(text)
    ssl.create_default_context(cafile=str(file))               # it loads as a trust store


async def _handshake(server: ssl.SSLContext, client: ssl.SSLContext, name: str) -> str:
    """Serve one TLS connection with `server`, connect with `client` as if to `name`, and return what was read."""
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"hello")
        await writer.drain()
        writer.close()

    listening = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=server)
    port = listening.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client, server_hostname=name)
        data = await reader.read()
        writer.close()
        return data.decode()
    finally:
        listening.close()


async def test_a_host_s_certificate_is_trusted_by_whoever_trusts_the_authority(tmp_path: Path) -> None:
    cert_pem, key_pem = new_authority()
    authority = Authority(cert_pem, key_pem, tmp_path / "certs")
    trusting = ssl.create_default_context(cadata=cert_pem)
    assert await _handshake(authority.context_for("api.example.com"), trusting, "api.example.com") == "hello"
    assert authority.context_for("api.example.com") is authority.context_for("api.example.com")     # kept

    with pytest.raises(ssl.SSLCertVerificationError):          # a certificate for one host is not one for another
        await _handshake(authority.context_for("api.example.com"), trusting, "other.example.com")
    with pytest.raises(ssl.SSLCertVerificationError):          # and nobody else trusts it
        await _handshake(authority.context_for("api.example.com"), ssl.create_default_context(), "api.example.com")


async def test_an_address_gets_a_certificate_too(tmp_path: Path) -> None:
    cert_pem, key_pem = new_authority()
    authority = Authority(cert_pem, key_pem, tmp_path / "certs")
    trusting = ssl.create_default_context(cadata=cert_pem)
    assert await _handshake(authority.context_for("127.0.0.1"), trusting, "127.0.0.1") == "hello"
