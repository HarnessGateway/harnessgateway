"""The egress door (SPEC §7.1, §8.2): a box's only way out. One process per box, in a small container of its own on the
box's internal network (as `hg-door`) and the node's uplink network.

- :3128, a CONNECT proxy. A host:port the turn's egress does not allow gets 403. The door resolves names itself and
  connects only to the address it checked; a private address is refused unless the operator allowed private egress.
  A host no secret is bound to is tunnelled untouched. A host a secret is bound to is intercepted: the door presents a
  certificate from the node's CA (which boxes trust), offers HTTP/1.1 only, verifies the real server normally, and in
  the request's headers replaces each placeholder bound to that host with its value. Bodies, URLs and responses are
  never touched; a placeholder sent anywhere else is sent as it is.
- :8080, the provider relay: plain HTTP from the box, relayed to the gateway with the turn's door token added.

Rules (the allowlist, the bindings, the door token) come from rules.json in the door's directory, which the node
rewrites per turn and the box cannot read. Every byte is counted into counters.json; every connection is one line in
connections.jsonl (host, port, verdict, intercepted, bytes), never a header, body or value.

    harnessgateway door --dir /door --ca /ca --gateway URL [--upstream-proxy URL] [--ca-file PEM] [--allow-private]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import ipaddress
import json
import os
import secrets
import socket
import ssl
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..domain.egress import Policy

HEAD_MAX = 256 * 1024
HANDSHAKE_S = 30
CONNECT_S = 15
COUNTERS_EVERY_S = 2
CHUNK = 65536
DOOR_HEADER = b"hg-door-token"


def log(message: str) -> None:
    print(f"door: {message}", file=sys.stderr, flush=True)


def public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


@dataclass
class Rules:
    """The current turn's rules, reloaded whenever the node rewrites the file."""
    path: Path
    mtime: float = -1
    turn: str | None = None
    policy: Policy = field(default_factory=Policy)
    bindings: list[tuple[bytes, bytes, Policy]] = field(default_factory=list)
    door_token: str = ""

    def refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self.mtime:
            return
        doc = json.loads(self.path.read_text())
        self.mtime, self.turn, self.door_token = mtime, doc.get("turn"), doc.get("door_token") or ""
        self.policy = Policy.of(doc.get("allow") or [])
        self.bindings = [(b["placeholder"].encode(), b["value"].encode(), Policy.of(b["hosts"]))
                         for b in doc.get("bindings") or []]

    def substitutions(self, host: str, port: int) -> list[tuple[bytes, bytes]]:
        return [(p, v) for p, v, hosts in self.bindings if hosts.permits(host, port)]


class Issuer:
    """Certificates for intercepted hosts, issued from the node's CA, one TLS server context per host."""

    def __init__(self, ca_dir: Path):
        self.ca_key = serialization.load_pem_private_key((ca_dir / "ca.key").read_bytes(), None)
        self.ca_cert = x509.load_pem_x509_certificate((ca_dir / "ca.crt").read_bytes())
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.dir = Path(tempfile.mkdtemp(prefix="door-"))
        self._contexts: dict[str, ssl.SSLContext] = {}

    def context(self, host: str) -> ssl.SSLContext:
        if host in self._contexts:
            return self._contexts[host]
        now = datetime.datetime.now(datetime.UTC)
        try:
            san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        ca_ski = self.ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
                .issuer_name(self.ca_cert.subject).public_key(self.key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(hours=1)).not_valid_after(now + datetime.timedelta(days=7))
                .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                             data_encipherment=False, key_agreement=True, key_cert_sign=False,
                                             crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
                .sign(self.ca_key, hashes.SHA256()))  # type: ignore[arg-type]
        stem = self.dir / secrets.token_hex(8)
        cert_file, key_file = stem.with_suffix(".crt"), stem.with_suffix(".key")
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM) +
                              self.ca_cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        ctx.set_alpn_protocols(["http/1.1"])
        cert_file.unlink()
        key_file.unlink()
        self._contexts[host] = ctx
        return ctx


def rewrite(head: bytes, substitutions: list[tuple[bytes, bytes]], extra: list[tuple[bytes, bytes]] = (),  # type: ignore[assignment]
            target: bytes | None = None) -> bytes:
    """A request head with placeholders replaced in header values (never the request line), `Connection: close` (unless
    it upgrades), any `extra` headers set, and optionally the request target replaced."""
    lines = head.split(b"\r\n")
    start = lines[0]
    if target is not None:
        method, _, rest = start.partition(b" ")
        _, _, version = rest.rpartition(b" ")
        start = method + b" " + target + b" " + version
    names = {name.lower() for name, _ in extra}
    upgrade = any(line.lower().startswith(b"upgrade:") for line in lines[1:])
    out = [start]
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(b":")
        lname = name.strip().lower()
        if lname in names or (not upgrade and lname in (b"connection", b"keep-alive", b"proxy-connection")):
            continue
        for placeholder, secret in substitutions:
            value = value.replace(placeholder, secret)
        out.append(name + sep + value)
    out += [name + b": " + value for name, value in extra]
    if not upgrade:
        out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


class Door:
    def __init__(self, folder: Path, ca_dir: Path, gateway: str, upstream: str | None, ca_file: str | None,
                 allow_private: bool):
        self.rules = Rules(folder / "rules.json")
        self.counters_file, self.connections_file = folder / "counters.json", folder / "connections.jsonl"
        self.counts = {"out": 0, "in": 0}
        self.issuer = Issuer(ca_dir)
        self.upstream, self.allow_private = upstream, allow_private
        self.client_tls = ssl.create_default_context()
        if ca_file:
            self.client_tls.load_verify_locations(ca_file)
        self.client_tls.set_alpn_protocols(["http/1.1"])
        g = urlsplit(gateway)
        self.gw_host, self.gw_port = g.hostname or "", g.port or (443 if g.scheme == "https" else 80)
        self.gw_prefix, self.gw_tls = g.path.rstrip("/").encode(), g.scheme == "https"

    # ── plumbing ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str,
                   tally: dict[str, int]) -> None:
        try:
            while data := await reader.read(CHUNK):
                self.counts[direction] += len(data)
                tally[direction] += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError, ssl.SSLError):
            pass
        finally:
            with contextlib.suppress(Exception):
                if writer.can_write_eof():
                    writer.write_eof()
                else:
                    writer.close()

    async def send(self, writer: asyncio.StreamWriter, data: bytes, tally: dict[str, int]) -> None:
        """Bytes the door itself writes upstream (a rewritten request head), counted like relayed ones."""
        self.counts["out"] += len(data)
        tally["out"] += len(data)
        writer.write(data)
        await writer.drain()

    async def splice(self, a_r: asyncio.StreamReader, a_w: asyncio.StreamWriter, b_r: asyncio.StreamReader,
                     b_w: asyncio.StreamWriter, tally: dict[str, int]) -> None:
        await asyncio.gather(self.pipe(a_r, b_w, "out", tally), self.pipe(b_r, a_w, "in", tally))

    async def open_tcp(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A TCP stream to host:port, directly (to an address checked here) or through the operator's upstream proxy."""
        if not self.upstream:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [str(i[4][0]) for i in infos if self.allow_private or public(str(i[4][0]))]
            if not addresses:
                raise PermissionError(f"{host} has no public address")
            return await asyncio.wait_for(asyncio.open_connection(addresses[0], port, limit=HEAD_MAX), CONNECT_S)
        u = urlsplit(self.upstream)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or 3128, limit=HEAD_MAX),
                                                CONNECT_S)
        writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_S)
        if b" 200" not in head.split(b"\r\n", 1)[0]:
            writer.close()
            raise ConnectionError(f"the upstream proxy refused {host}:{port}")
        return reader, writer

    def record(self, host: str, port: int, verdict: str, intercepted: bool, tally: dict[str, int], started: float) -> None:
        line = {"at": round(started, 3), "turn": self.rules.turn, "host": host, "port": port, "verdict": verdict,
                "intercepted": intercepted, "out": tally["out"], "in": tally["in"], "s": round(time.time() - started, 3)}
        with self.connections_file.open("a") as f:
            f.write(json.dumps(line) + "\n")

    # ── :3128 ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started, tally = time.time(), {"out": 0, "in": 0}
        host, port, verdict, intercepted = "", 0, "failed", False
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_S)
            method, target = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[:2]
            if method != "CONNECT":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                verdict = "denied"
                return
            name, _, port_s = target.rpartition(":")
            host, port = name.strip("[]").lower().rstrip("."), int(port_s or 443)
            self.rules.refresh()
            if not self.rules.policy.permits(host, port):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                verdict = "denied"
                log(f"refused {host}:{port}")
                return
            up_r, up_w = await self.open_tcp(host, port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            verdict = "allowed"
            substitutions = self.rules.substitutions(host, port)
            if not substitutions:
                await self.splice(reader, writer, up_r, up_w, tally)
                return
            intercepted = True
            await self.intercept(reader, writer, up_r, up_w, host, substitutions, tally)
        except PermissionError as e:
            verdict = "denied"
            log(f"refused {host}:{port}: {e}")
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        except (TimeoutError, ConnectionError, OSError, ValueError, ssl.SSLError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as e:
            log(f"{host}:{port} failed: {type(e).__name__}")
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            if host:
                self.record(host, port, verdict, intercepted, tally, started)

    async def intercept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, up_r: asyncio.StreamReader,
                        up_w: asyncio.StreamWriter, host: str, substitutions: list[tuple[bytes, bytes]],
                        tally: dict[str, int]) -> None:
        """TLS with the box as the server for `host`, TLS with the real server as a client; then one request, its
        headers' placeholders replaced, and everything after it relayed until either side closes."""
        await writer.start_tls(self.issuer.context(host), ssl_handshake_timeout=HANDSHAKE_S)
        await up_w.start_tls(self.client_tls, server_hostname=host, ssl_handshake_timeout=HANDSHAKE_S)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_S)
        await self.send(up_w, rewrite(head, substitutions), tally)
        await self.splice(reader, writer, up_r, up_w, tally)

    # ── :8080 ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def provider(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started, tally, verdict = time.time(), {"out": 0, "in": 0}, "failed"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_S)
            self.rules.refresh()
            if not self.rules.door_token:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                verdict = "denied"
                return
            target = head.split(b"\r\n", 1)[0].split(b" ")[1]
            host = f"{self.gw_host}:{self.gw_port}".encode()
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(
                self.gw_host, self.gw_port, ssl=self.client_tls if self.gw_tls else None,
                server_hostname=self.gw_host if self.gw_tls else None, limit=HEAD_MAX), CONNECT_S)
            await self.send(up_w, rewrite(head, [], [(b"Host", host), (DOOR_HEADER, self.rules.door_token.encode())],
                                          target=self.gw_prefix + target), tally)
            verdict = "allowed"
            await self.splice(reader, writer, up_r, up_w, tally)
        except (TimeoutError, ConnectionError, OSError, ValueError, IndexError, ssl.SSLError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
            log(f"provider relay failed: {type(e).__name__}")
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            self.record("gateway", 8080, verdict, False, tally, started)

    async def write_counters(self) -> None:
        while True:
            tmp = self.counters_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self.counts, "at": time.time()}))
            os.replace(tmp, self.counters_file)
            await asyncio.sleep(COUNTERS_EVERY_S)


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="harnessgateway door")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--ca", required=True)
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--upstream-proxy", default="")
    ap.add_argument("--ca-file", default="")
    ap.add_argument("--allow-private", action="store_true")
    a = ap.parse_args(argv)
    door = Door(Path(a.dir), Path(a.ca), a.gateway, a.upstream_proxy or None, a.ca_file or None, a.allow_private)
    s1 = await asyncio.start_server(door.connect, "0.0.0.0", 3128, limit=HEAD_MAX)  # noqa: S104 - its networks only
    s2 = await asyncio.start_server(door.provider, "0.0.0.0", 8080, limit=HEAD_MAX)  # noqa: S104
    log("ready")
    async with s1, s2:
        await asyncio.gather(s1.serve_forever(), s2.serve_forever(), door.write_counters())


def run(argv: list[str] | None = None) -> Any:
    return asyncio.run(main(argv))
