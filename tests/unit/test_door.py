"""The door: deny and allow per the turn's rules, tunnel hosts without a binding untouched, and replace a bound host's
placeholders in request headers only."""
import asyncio
import json
import ssl
from pathlib import Path

import pytest

from harnessgateway.node.door import Door, Issuer, rewrite
from harnessgateway.node.trust import Trust

PLACEHOLDER, VALUE = "hgs_" + "0" * 32, "real-secret-value"


def make_ca(folder: Path) -> Path:
    folder.mkdir()
    key, cert = Trust(folder, "test", "")._new_ca()
    (folder / "ca.key").write_bytes(key)
    (folder / "ca.crt").write_bytes(cert)
    return folder


async def echo_server(ca: Path, host: str) -> tuple[asyncio.Server, list[bytes]]:
    """A TLS server for `host` that answers each request with the head it received."""
    seen: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        seen.append(head)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(head) + head)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0, ssl=Issuer(ca).context(host)), seen


@pytest.fixture
async def setup(tmp_path):
    node_ca, server_ca = make_ca(tmp_path / "node"), make_ca(tmp_path / "server")
    folder = tmp_path / "door"
    folder.mkdir()
    door = Door(folder, node_ca, "http://gateway.test:8000/gateway", None, str(server_ca / "ca.crt"), False)
    bound, bound_seen = await echo_server(server_ca, "api.example.com")
    free, free_seen = await echo_server(server_ca, "free.example.com")
    ports = {"api.example.com": bound.sockets[0].getsockname()[1], "free.example.com": free.sockets[0].getsockname()[1]}

    async def open_tcp(host, port):                # names resolve to the local servers; the policy is still the door's
        return await asyncio.open_connection("127.0.0.1", ports[host])

    door.open_tcp = open_tcp                       # type: ignore[method-assign]
    (folder / "rules.json").write_text(json.dumps({
        "turn": "resp_x", "allow": ["*.example.com", "!evil.example.com"],
        "bindings": [{"placeholder": PLACEHOLDER, "value": VALUE, "hosts": ["api.example.com"]}]}))
    server = await asyncio.start_server(door.connect, "127.0.0.1", 0)
    yield {"port": server.sockets[0].getsockname()[1], "folder": folder, "node_ca": node_ca, "server_ca": server_ca,
           "bound_seen": bound_seen, "free_seen": free_seen}
    for s in (server, bound, free):
        s.close()


async def through(door_port: int, host: str, trust: Path) -> tuple[bytes, bytes]:
    """CONNECT host:443 through the door, then one GET with the placeholder in its URL and a header."""
    reader, writer = await asyncio.open_connection("127.0.0.1", door_port)
    writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    status = await reader.readuntil(b"\r\n\r\n")
    if b" 200 " not in status:
        writer.close()
        return status, b""
    await writer.start_tls(ssl.create_default_context(cafile=str(trust / "ca.crt")), server_hostname=host)
    writer.write(f"GET /x?k={PLACEHOLDER} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {PLACEHOLDER}\r\n\r\n".encode())
    body = await reader.read()
    writer.close()
    return status, body


async def test_a_bound_host_is_intercepted_and_only_its_headers_get_the_value(setup):
    status, body = await through(setup["port"], "api.example.com", setup["node_ca"])
    assert b" 200 " in status
    head = setup["bound_seen"][0]
    assert f"Authorization: Bearer {VALUE}".encode() in head
    assert f"GET /x?k={PLACEHOLDER} ".encode() in head and b"Connection: close" in head
    log = setup["folder"] / "connections.jsonl"
    for _ in range(100):                           # the door records a connection once both sides have closed
        if log.exists():
            break
        await asyncio.sleep(0.02)
    line = json.loads(log.read_text().splitlines()[-1])
    assert line["intercepted"] and line["verdict"] == "allowed" and VALUE not in json.dumps(line)


async def test_a_host_without_a_binding_is_tunnelled_untouched(setup):
    status, _ = await through(setup["port"], "free.example.com", setup["server_ca"])  # the real server's certificate
    assert b" 200 " in status
    assert f"Authorization: Bearer {PLACEHOLDER}".encode() in setup["free_seen"][0]


@pytest.mark.parametrize("host", ["evil.example.com", "example.org"])
async def test_a_denied_or_unlisted_host_is_refused(setup, host):
    status, _ = await through(setup["port"], host, setup["node_ca"])
    assert status.startswith(b"HTTP/1.1 403")


async def test_rules_reload_when_the_node_rewrites_them(setup):
    (setup["folder"] / "rules.json").write_text(json.dumps({"turn": "resp_y", "allow": []}))
    status, _ = await through(setup["port"], "free.example.com", setup["server_ca"])
    assert status.startswith(b"HTTP/1.1 403")


def test_rewrite_sets_headers_and_target_and_keeps_upgrades():
    head = b"POST /v1/messages HTTP/1.1\r\nHost: hg-door:8080\r\nConnection: keep-alive\r\n\r\n"
    out = rewrite(head, [], [(b"Host", b"gw:80"), (b"hg-door-token", b"t")], target=b"/gateway/v1/messages")
    assert out == (b"POST /gateway/v1/messages HTTP/1.1\r\nHost: gw:80\r\nhg-door-token: t\r\nConnection: close\r\n\r\n")
    ws = b"GET / HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
    assert rewrite(ws, []) == ws
