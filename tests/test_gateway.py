"""Layer 4: the gateway swaps a session token for the provider's key, relays only the API shape's
paths, keeps the exchange as the turn's capture and counts it as usage. Against a TLS upstream of
our own, so no key and no byte leaves this machine."""
import asyncio
import datetime
import ipaddress
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from harnessgateway.blobs import DirBlobs
from harnessgateway.gateway import Gateway, allowed, credential
from harnessgateway.records import SqliteRecords
from harnessgateway.store import Store


def test_paths_and_credentials():
    assert allowed("anthropic-messages", "/v1/messages") and allowed("anthropic-messages", "/v1/models/claude-x")
    assert allowed("anthropic-messages", "/anthropic/v1/messages", "/anthropic") and not allowed("anthropic-messages", "/v1/messages", "/anthropic")
    assert not allowed("anthropic-messages", "/v1/files") and not allowed("openai-chat", "/v1/fine_tuning/jobs")
    assert allowed("openai-responses", "/v1/responses") and allowed("openai-responses", "/v1/responses/resp_1/cancel")
    assert allowed("openai-chat", "/v1/responses") and allowed("openai-responses", "/v1/chat/completions")
    assert allowed("none", "/anything/at/all")
    assert credential([("x-api-key", "tok")]) == "tok" and credential([("Authorization", "Bearer tok")]) == "tok"
    assert credential([("x-api-key", "tok"), ("authorization", "Bearer other")]) is None and credential([("accept", "*/*")]) is None


def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return tmp_path / "cert.pem", tmp_path / "key.pem"


def test_relay_capture_and_usage(tmp_path, vault):
    cert, key = certificate(tmp_path)
    seen = []

    async def upstream(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        body = await reader.readexactly(int(headers.get("Content-Length", "0")))
        seen.append((lines[0], headers, body))
        answer = b'{"ok": true, "echo": ' + body + b"}"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(answer)).encode() + b"\r\n\r\n" + answer)
        await writer.drain()
        writer.close()

    async def go():
        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_ctx.load_cert_chain(cert, key)
        server = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        store = Store(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "b", "http://api.test", b"s"), vault, tmp_path / "git", allow_private=True)
        store.create_org("acme")
        store.put_provider("acme", "prov", f"https://127.0.0.1:{port}/v1", "openai-chat", "sk-real-secret")
        token = store.issue_token("acme", "prov", "sess_1", "resp_1", "openai-chat", 60)
        client_ctx = ssl.create_default_context(cafile=str(cert))
        gw = Gateway(store, store.blobs, tls=client_ctx)
        gws = await asyncio.start_server(gw.handle, "127.0.0.1", 0)
        gport = gws.sockets[0].getsockname()[1]

        async def call(path, auth, body=b'{"q": 1}'):
            r, w = await asyncio.open_connection("127.0.0.1", gport)
            w.write(f"POST {path} HTTP/1.1\r\nHost: hg-provider:8080\r\nAuthorization: Bearer {auth}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await w.drain()
            data = await r.read()
            w.close()
            return data
        ok = await call("/v1/chat/completions", token)
        no = await call("/v1/chat/completions", "sk-hg-wrong")
        forbidden = await call("/v1/files", token)
        server.close()
        gws.close()
        return ok, no, forbidden, store
    ok, no, forbidden, store = asyncio.run(go())
    assert ok.startswith(b"HTTP/1.1 200") and b'"echo": {"q": 1}' in ok
    assert no.startswith(b"HTTP/1.1 401") and forbidden.startswith(b"HTTP/1.1 403")
    assert seen[0][0] == "POST /v1/chat/completions HTTP/1.1" and seen[0][1]["Authorization"] == "Bearer sk-real-secret"
    assert len(seen) == 1                                   # the refused calls never reached the provider
    usage = store.records.get("usage", "acme", "resp_1")
    assert usage["calls"] == 1 and usage["bytes_in"] == 8 and usage["bytes_out"] > 0
    captures = store.blobs.list("tenants/acme/sessions/sess_1/turns/resp_1/provider")
    assert captures == ["tenants/acme/sessions/sess_1/turns/resp_1/provider/0001.json"]
    import json
    cap = json.loads(store.blobs.get(captures[0]))
    assert cap["path"] == "/v1/chat/completions" and cap["status"] == "200" and "sk-real-secret" not in json.dumps(cap)
