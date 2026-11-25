"""The little HTTP the proxy reads, and the certificates it shows."""

import asyncio
import contextlib
import socket
import ssl

import pytest

from harnessgateway.domain.egress import BoundSecret
from harnessgateway.domain.sealing import KeySealer
from harnessgateway.proxy.authority import Authority, new_authority, trust_bundle
from harnessgateway.proxy.http import Head, put_secrets, read_head, refusal
from harnessgateway.settings import ProxySettings, Settings, SettingsError


async def test_a_request_head_is_read_and_a_secret_goes_into_its_headers_and_query_only():
    reader = asyncio.StreamReader()
    reader.feed_data(b"POST /v1/x?key=hgs_a&q=1 HTTP/1.1\r\nHost: api.example.com\r\nAuthorization: Bearer hgs_a\r\n"
                     b"X-Key: old\r\nConnection: keep-alive\r\n\r\nbody hgs_a")
    head = await read_head(reader)
    assert isinstance(head, Head) and (head.method, head.target, head.header("host")) == ("POST", "/v1/x?key=hgs_a&q=1", "api.example.com")
    put_secrets(head, [BoundSecret(("api.example.com",), "real", "hgs_a"), BoundSecret(("api.example.com",), "other", "hgs_b", "X-Key", "Key ")])
    sent = head.encode().decode()
    assert sent.startswith("POST /v1/x?key=real&q=1 HTTP/1.1\r\n") and "Authorization: Bearer real\r\n" in sent
    assert "X-Key: Key other\r\n" in sent and "X-Key: old" not in sent and sent.endswith("Connection: close\r\n\r\n")
    assert await reader.read(100) == b"body hgs_a"                      # the body is never read, so never changed
    for wrong in (b"GET / HTTP/1.1\r\nno colon here\r\n\r\n", b"nonsense\r\n\r\n", b"GET /"):
        broken = asyncio.StreamReader()
        broken.feed_data(wrong)
        broken.feed_eof()
        assert await read_head(broken) is None
    assert refusal("403 Forbidden", "no").startswith(b"HTTP/1.1 403 Forbidden\r\n") and refusal("403 Forbidden", "no").endswith(b"\r\n\r\nno\n")


def test_a_strict_client_trusts_what_the_authority_signs_for_a_host(tmp_path):
    certificate, key = new_authority()
    bundle = tmp_path / "ca.pem"
    bundle.write_text(trust_bundle(certificate))
    server_side, client_side = socket.socketpair()
    client = ssl.create_default_context(cafile=str(bundle))
    client.verify_flags |= ssl.VERIFY_X509_STRICT                       # what Python 3.13 and later do by default
    served: list[Exception] = []

    def serve() -> None:
        try:
            with Authority(certificate, key).context_for("api.example.com").wrap_socket(server_side, server_side=True) as tls:
                tls.sendall(b"ok")
        except Exception as error:
            served.append(error)
    import threading
    thread = threading.Thread(target=serve)
    thread.start()
    with client.wrap_socket(client_side, server_hostname="api.example.com") as tls:
        assert tls.recv(2) == b"ok"
    thread.join()
    assert served == []
    other_side, wrong_side = socket.socketpair()
    threading.Thread(target=lambda: _quietly(Authority(certificate, key).context_for("api.example.com"), other_side)).start()
    with pytest.raises(ssl.SSLCertVerificationError):
        client.wrap_socket(wrong_side, server_hostname="other.example.com")


def _quietly(context: ssl.SSLContext, sock: socket.socket) -> None:
    with contextlib.suppress(ssl.SSLError, OSError):
        context.wrap_socket(sock, server_side=True).close()


def test_a_sealed_value_opens_only_where_it_belongs_and_settings_are_checked_when_a_process_starts():
    sealer = KeySealer(bytes(range(32)))
    sealed = sealer.seal("sk-real", "org", "providers", "p", "key")
    assert "sk-real" not in sealed and sealed != sealer.seal("sk-real", "org", "providers", "p", "key")
    assert sealer.open(sealed, "org", "providers", "p", "key") == "sk-real"
    for attempt in (lambda: sealer.open(sealed, "org", "providers", "other", "key"), lambda: sealer.open(sealed[:-4] + "AAAA", "org", "providers", "p", "key"),
                    lambda: KeySealer(bytes(32)).open(sealed, "org", "providers", "p", "key")):
        with pytest.raises(ValueError, match="does not open"):
            attempt()
    good = {"HG_DATABASE_URL": "postgresql://x", "HG_SECRET_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", "HG_PROXY_KEY": "k"}
    settings = Settings.from_env({**good, "HG_EXTENSIONS": "a:B, c:D"})
    assert (settings.namespace, settings.port, settings.blobs, settings.extensions) == ("hg", 8000, "/data/blobs", ("a:B", "c:D"))
    assert Settings.from_env({**good, "HG_SECRET_KEY": ""}).secret_key is None       # an extension's sealer may stand in for it
    for wrong, why in (({"HG_SECRET_KEY": "short"}, "32 random bytes"), ({"HG_PORT": "0"}, "HG_PORT"), ({"HG_RUNTIME": "kata"}, "HG_RUNTIME"),
                       ({"HG_DATABASE_URL": ""}, "HG_DATABASE_URL is required")):
        with pytest.raises(SettingsError, match=why):
            Settings.from_env({**good, **wrong})
    assert ProxySettings.from_env({"HG_PROXY_KEY": "k", "HG_PRIVATE_HOSTS": "10.0.0.5:11434"}).private_hosts == ("10.0.0.5:11434",)
    with pytest.raises(SettingsError, match="HG_PRIVATE_HOSTS"):
        ProxySettings.from_env({"HG_PROXY_KEY": "k", "HG_PRIVATE_HOSTS": "http://x"})
