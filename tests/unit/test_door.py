"""The door (SPEC §11) against local servers: a fake api answering policies and taking records, a plain TCP echo
server, and a TLS server that echoes the request head it received, so interception and substitution are visible."""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import ssl
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from harnessgateway.config import Settings
from harnessgateway.crypto import DoorCA
from harnessgateway.door.main import Door, credential, rewrite

TOKEN = "hgb_box_token"
SECRET = "dGVzdC1zZWNyZXQta2V5LW9mLXRoaXJ0eS10d28tYnl0ZXM="


def basic(token: str) -> bytes:
    return b"Proxy-Authorization: Basic " + base64.b64encode(f"box:{token}".encode())


class FakeApi:
    """POST /internal/door/policy and /records over plain HTTP/1.1, one request per connection."""

    def __init__(self, policy: dict[str, Any]):
        self.policy = policy
        self.records: list[dict[str, Any]] = []
        self.asked: list[str] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        path = lines[0].split(" ")[1]
        length = next((int(v) for k, _, v in (ln.partition(":") for ln in lines[1:]) if k.lower() == "content-length"), 0)
        body = json.loads(await reader.readexactly(length)) if length else {}
        auth = next((v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k.lower() == "authorization"), "")
        if auth != "Bearer hgd_test":
            status, doc = 401, {"error": "door key"}
        elif path == "/internal/door/policy":
            self.asked.append(body["token"])
            status, doc = (200, self.policy) if body["token"] == TOKEN else (404, {"error": "no box"})
        else:
            self.records.extend(body["records"])
            status, doc = 200, {"ok": True}
        data = json.dumps(doc).encode()
        writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + data)
        await writer.drain()
        writer.close()


async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    data = await reader.read(65536)
    writer.write(data)
    await writer.drain()
    writer.close()


async def echo_head(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """An HTTP server returning the request head it got, as the body."""
    head = await reader.readuntil(b"\r\n\r\n")
    writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(head)}\r\nConnection: close\r\n\r\n".encode() + head)
    await writer.drain()
    writer.close()


def server_cert(tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "upstream")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp / "up.crt", tmp / "up.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


@pytest_asyncio.fixture
async def world(tmp_path):
    """The fake api, a TCP echo, a TLS head-echo, and the door in front of them, all on loopback."""
    cert_file, key_file = server_cert(tmp_path)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    api = FakeApi({})
    api_server = await asyncio.start_server(api.handle, "127.0.0.1", 0)
    plain = await asyncio.start_server(echo, "127.0.0.1", 0)
    secure = await asyncio.start_server(echo_head, "127.0.0.1", 0, ssl=tls)
    api_port, plain_port, tls_port = (s.sockets[0].getsockname()[1] for s in (api_server, plain, secure))
    api.policy = {"version": 1, "org_id": "o", "turn_id": "resp_1", "allow": [f"127.0.0.1:{plain_port}", f"127.0.0.1:{tls_port}"],
                  "bindings": [{"placeholder": "hgs_PLACEHOLDER", "value": "the-secret", "hosts": [f"127.0.0.1:{tls_port}"]}],
                  "platform": [f"127.0.0.1:{api_port}"]}
    settings = Settings.load(("door",), env={"HG_SECRET_KEY": SECRET, "HG_DOOR_KEY": "hgd_test",
                                             "HG_API_URL": f"http://127.0.0.1:{api_port}", "HG_CA_FILE": str(cert_file),
                                             "HG_ALLOW_PRIVATE_EGRESS": "1"})
    door = Door(settings)
    door_server = await asyncio.start_server(door.connect, "127.0.0.1", 0)
    door_port = door_server.sockets[0].getsockname()[1]
    try:
        yield {"api": api, "door": door, "door_port": door_port, "plain_port": plain_port, "tls_port": tls_port,
               "api_port": api_port}
    finally:
        for s in (api_server, plain, secure, door_server):
            s.close()
        await door.platform.http.aclose()


async def connect(port: int, target: str, auth: bytes | None = basic(TOKEN)
                  ) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode() + ((auth + b"\r\n") if auth else b"") + b"\r\n"
    writer.write(head)
    await writer.drain()
    reply = await reader.readuntil(b"\r\n\r\n")
    return reply, reader, writer


async def test_credentials_decide_who_may_knock(world):
    reply, _, w = await connect(world["door_port"], f"127.0.0.1:{world['plain_port']}", auth=None)
    assert reply.startswith(b"HTTP/1.1 407")
    w.close()
    reply, _, w = await connect(world["door_port"], f"127.0.0.1:{world['plain_port']}", auth=basic("hgb_unknown"))
    assert reply.startswith(b"HTTP/1.1 403")
    w.close()
    assert world["api"].asked == ["hgb_unknown"]
    assert credential(b"CONNECT x HTTP/1.1\r\nProxy-Authorization: Bearer x\r\n\r\n") is None
    assert credential(b"CONNECT x HTTP/1.1\r\n" + basic("hgb_t") + b"\r\n\r\n") == "hgb_t"


async def test_an_allowed_host_is_tunnelled_untouched_and_a_denied_one_refused(world):
    reply, reader, writer = await connect(world["door_port"], f"127.0.0.1:{world['plain_port']}")
    assert reply == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    writer.write(b"hello hgs_PLACEHOLDER")
    await writer.drain()
    assert await reader.read(100) == b"hello hgs_PLACEHOLDER"                 # no binding on this host: nothing replaced
    writer.close()
    reply, _, w = await connect(world["door_port"], "evil.example.com:443")
    assert reply.startswith(b"HTTP/1.1 403")
    w.close()
    reply, _, w = await connect(world["door_port"], f"127.0.0.1:{world['api_port']}")
    assert reply.startswith(b"HTTP/1.1 200")                                   # the platform host, always
    w.close()
    await asyncio.sleep(0.05)
    await world["door"].platform.flush()
    verdicts = {(r["host"], r["port"], r["verdict"]) for r in world["api"].records}
    assert ("127.0.0.1", world["plain_port"], "allowed") in verdicts and ("evil.example.com", 443, "denied") in verdicts
    allowed = next(r for r in world["api"].records if r["port"] == world["plain_port"])
    assert allowed["out"] == 21 and allowed["in"] == 21 and allowed["turn"] == "resp_1" and not allowed["intercepted"]


async def test_a_bound_host_is_intercepted_and_the_placeholder_replaced_in_headers_only(world, tmp_path):
    reply, reader, writer = await connect(world["door_port"], f"127.0.0.1:{world['tls_port']}")
    assert reply.startswith(b"HTTP/1.1 200")
    trust = ssl.create_default_context(cadata=DoorCA(base64.b64decode(SECRET)).cert_pem.decode())
    await writer.start_tls(trust, server_hostname="127.0.0.1")                 # the box trusts the door's CA
    writer.write(b"POST /v1/x?k=hgs_PLACEHOLDER HTTP/1.1\r\nHost: api\r\nAuthorization: Bearer hgs_PLACEHOLDER\r\n"
                 b"Content-Length: 15\r\n\r\nhgs_PLACEHOLDER")
    await writer.drain()
    data = await reader.read(65536)
    writer.close()
    assert data.startswith(b"HTTP/1.1 200")
    head = data.split(b"\r\n\r\n", 1)[1]
    assert b"Authorization: Bearer the-secret" in head                        # replaced in a header
    assert b"POST /v1/x?k=hgs_PLACEHOLDER" in head                              # never in the request line
    assert b"Connection: close" in head
    await asyncio.sleep(0.1)
    await world["door"].platform.flush()
    rec = next(r for r in world["api"].records if r["port"] == world["tls_port"])
    assert rec["intercepted"] and rec["method"] == "POST" and rec["path"].startswith("/v1/x") and rec["status"] == 200


async def test_a_refusal_is_checked_against_a_fresh_policy_first(world):
    """A box's policy is cached; when a host is not in it, the door asks the api again before refusing, so the first
    connection of a turn that began after the fetch is not refused on the standby policy."""
    api = world["api"]
    reply, _, w = await connect(world["door_port"], "late.example.com:443")
    assert reply.startswith(b"HTTP/1.1 403")
    w.close()
    asked_before = len(api.asked)
    api.policy = {**api.policy, "version": 2, "allow": [*api.policy["allow"], f"127.0.0.1:{world['plain_port']}"],
                  "platform": [*api.policy["platform"], f"127.0.0.1:{world['plain_port']}"]}
    world["door"].platform.cache[TOKEN].policy = world["door"].platform.cache[TOKEN].policy.__class__.of([])   # a stale cache
    reply, _, w = await connect(world["door_port"], f"127.0.0.1:{world['plain_port']}")
    assert reply.startswith(b"HTTP/1.1 200") and len(api.asked) == asked_before + 1
    w.close()


def test_rewrite_keeps_upgrades_and_drops_hop_headers():
    head = b"GET / HTTP/1.1\r\nHost: h\r\nConnection: keep-alive\r\nX-Key: hgs_A\r\n\r\n"
    out = rewrite(head, [(b"hgs_A", b"secret")])
    assert out == b"GET / HTTP/1.1\r\nHost: h\r\nX-Key: secret\r\nConnection: close\r\n\r\n"
    ws = b"GET / HTTP/1.1\r\nHost: h\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
    assert rewrite(ws, []) == ws


@pytest.mark.parametrize("value", ["hgd_test", "hgd_other"])
def test_settings_for_a_door_need_the_door_key_and_the_secret(value):
    s = Settings.load(("door",), env={"HG_SECRET_KEY": SECRET, "HG_DOOR_KEY": value, "HG_DOOR": "hg-door:3128"})
    assert s.door_key == value and s.door == "hg-door:3128"
