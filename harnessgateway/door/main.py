"""The door (SPEC §11): a box's only way out. One listener, a CONNECT proxy with proxy authentication by box token.

Per connection: the credential names a box; the api says what that box may reach now and which placeholders are
bound to which hosts (cached 30 s, or 3 s while bindings are in it); a host outside the policy is refused; the door
resolves the host and connects only to an address it checked; a host no secret is bound to is tunnelled untouched; a
host a secret is bound to is intercepted (TLS with the box as that host, TLS to the real server, placeholders replaced
in the one request head, the rest spliced). Bytes are counted and one record per connection goes back to the api in batches.
The door holds no database and no secret of its own but the door key and the CA it derives from the secret key."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import ipaddress
import logging
import secrets
import socket
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..config import Settings
from ..crypto import DoorCA
from ..domain.egress import Policy
from ..netguard import is_public

HEAD_MAX = 256 * 1024
HANDSHAKE_S = 30
CONNECT_S = 15
POLICY_TTL_S = 30                # a policy without bindings (standby, an install box)
BINDINGS_TTL_S = 3               # a policy with bindings: a turn's secrets outlive its end by at most this
RECORDS_EVERY_S = 3
CHUNK = 65536
FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
AUTH_REQUIRED = (b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"door\"\r\n"
                 b"Content-Length: 0\r\n\r\n")
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
log = logging.getLogger("harnessgateway.door")


class Rules:
    """One box's policy as the door applies it."""

    def __init__(self, doc: dict[str, Any]):
        self.version = int(doc.get("version", 0))
        self.turn = doc.get("turn_id")
        self.policy = Policy.of(doc.get("allow") or [], platform=doc.get("platform") or [])
        self.bindings = [(b["placeholder"].encode(), b["value"].encode(), Policy.of(b["hosts"]))
                         for b in doc.get("bindings") or []]
        self.at = time.monotonic()

    def substitutions(self, host: str, port: int) -> list[tuple[bytes, bytes]]:
        return [(p, v) for p, v, hosts in self.bindings if hosts.permits(host, port)]

    @property
    def fresh(self) -> bool:
        return time.monotonic() - self.at < (BINDINGS_TTL_S if self.bindings else POLICY_TTL_S)


class Platform:
    """The api as the door sees it: policies by box token, and the records it sends back."""

    def __init__(self, api_url: str, door_key: str):
        self.http = httpx.AsyncClient(base_url=api_url, headers={"Authorization": f"Bearer {door_key}"}, timeout=10)
        self.cache: dict[str, Rules] = {}
        self.pending: dict[str, list[dict[str, Any]]] = {}

    async def rules(self, token: str, fresh: bool = False) -> Rules | None:
        """The box's current rules; None for a token the api does not know. `fresh` skips the cache: a refusal is
        checked against the api before it stands, since a turn may have started since the policy was fetched. A
        policy that carries bindings is kept only briefly, so a turn's secrets stop being injected soon after it ends."""
        hit = self.cache.get(token)
        if hit and not fresh and hit.fresh:
            return hit
        r = await self.http.post("/internal/door/policy", json={"token": token})
        if r.status_code == 404:
            self.cache.pop(token, None)
            return None
        r.raise_for_status()
        rules = Rules(r.json())
        self.cache[token] = rules
        return rules

    def forget(self, token: str) -> None:
        self.cache.pop(token, None)

    def record(self, token: str, line: dict[str, Any]) -> None:
        self.pending.setdefault(token, []).append(line)

    async def flush(self) -> None:
        batch, self.pending = self.pending, {}
        for token, records in batch.items():
            try:
                r = await self.http.post("/internal/door/records", json={"token": token, "records": records})
                r.raise_for_status()
            except Exception as e:
                log.warning("%d records for a box could not be sent: %s", len(records), e)

    async def flush_forever(self) -> None:
        while True:
            await asyncio.sleep(RECORDS_EVERY_S)
            await self.flush()


class Issuer:
    """Certificates for intercepted hosts from the door's CA, one TLS server context per host."""

    def __init__(self, ca: DoorCA):
        self.ca = ca
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
        ca_ski = self.ca.cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
                .issuer_name(self.ca.cert.subject).public_key(self.key.public_key())
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
                .sign(self.ca.key, hashes.SHA256()))
        stem = self.dir / secrets.token_hex(8)
        cert_file, key_file = stem.with_suffix(".crt"), stem.with_suffix(".key")
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + self.ca.cert_pem)
        key_file.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        ctx.set_alpn_protocols(["http/1.1"])
        cert_file.unlink()
        key_file.unlink()
        self._contexts[host] = ctx
        return ctx


def rewrite(head: bytes, substitutions: list[tuple[bytes, bytes]]) -> bytes:
    """A request head with placeholders replaced in header values (never the request line) and `Connection: close`
    (unless it upgrades)."""
    lines = head.split(b"\r\n")
    upgrade = any(line.lower().startswith(b"upgrade:") for line in lines[1:])
    out = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(b":")
        if not upgrade and name.strip().lower() in (b"connection", b"keep-alive", b"proxy-connection"):
            continue
        for placeholder, secret in substitutions:
            value = value.replace(placeholder, secret)
        out.append(name + sep + value)
    if not upgrade:
        out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


def credential(head: bytes) -> str | None:
    """The box token from `Proxy-Authorization: Basic base64("box:<token>")`."""
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"proxy-authorization":
            scheme, _, blob = value.strip().partition(b" ")
            if scheme.lower() != b"basic":
                return None
            try:
                user, _, token = base64.b64decode(blob.strip(), validate=True).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
            return token if user == "box" and token.startswith("hgb_") else None
    return None


class Door:
    def __init__(self, settings: Settings):
        self.platform = Platform(settings.api_url, settings.door_key)
        self.issuer = Issuer(DoorCA(settings.secret_key))
        self.upstream, self.allow_private = settings.upstream_proxy or None, settings.allow_private_egress
        self.client_tls = ssl.create_default_context()
        if settings.ca_file:
            self.client_tls.load_verify_locations(settings.ca_file)
        self.client_tls.set_alpn_protocols(["http/1.1"])

    # ── plumbing ───────────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str, tally: dict[str, Any]
                   ) -> None:
        try:
            while data := await reader.read(CHUNK):
                tally[direction] += len(data)
                if direction == "in" and tally.get("status") is None and tally.get("intercepted"):
                    tally["status"] = _status(data)
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

    async def splice(self, a_r: asyncio.StreamReader, a_w: asyncio.StreamWriter, b_r: asyncio.StreamReader,
                     b_w: asyncio.StreamWriter, tally: dict[str, Any]) -> None:
        await asyncio.gather(self.pipe(a_r, b_w, "out", tally), self.pipe(b_r, a_w, "in", tally))

    async def open_tcp(self, host: str, port: int, platform_host: bool) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A TCP stream to host:port: directly, to an address checked here, or through the operator's upstream proxy."""
        if not self.upstream:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [str(i[4][0]) for i in infos if platform_host or self.allow_private or is_public(str(i[4][0]))]
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

    # ── one connection ─────────────────────────────────────────────────────────────────────────────────────────────
    async def connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.time()
        tally: dict[str, Any] = {"out": 0, "in": 0, "intercepted": False, "status": None}
        host, port, verdict, token, rules = "", 0, "failed", None, None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_S)
            method, target = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[:2]
            token = credential(head)
            if token is None:
                writer.write(AUTH_REQUIRED)
                verdict = "denied"
                return
            rules = await self.platform.rules(token)
            if method != "CONNECT" or rules is None:
                writer.write(FORBIDDEN)
                verdict = "denied"
                return
            name, _, port_s = target.rpartition(":")
            host, port = name.strip("[]").lower().rstrip("."), int(port_s or 443)
            if not rules.policy.permits(host, port):
                rules = await self.platform.rules(token, fresh=True) or rules
            if not rules.policy.permits(host, port):
                writer.write(FORBIDDEN)
                verdict = "denied"
                log.info("refused %s:%s for turn %s", host, port, rules.turn)
                return
            up_r, up_w = await self.open_tcp(host, port, rules.policy.is_platform(host, port))
            writer.write(ESTABLISHED)
            await writer.drain()
            verdict = "allowed"
            substitutions = rules.substitutions(host, port)
            if not substitutions:
                await self.splice(reader, writer, up_r, up_w, tally)
                return
            tally["intercepted"] = True
            await self.intercept(reader, writer, up_r, up_w, host, substitutions, tally)
        except PermissionError as e:
            verdict = "denied"
            log.info("refused %s:%s: %s", host, port, e)
            with contextlib.suppress(Exception):
                writer.write(FORBIDDEN)
        except (TimeoutError, ConnectionError, OSError, ValueError, ssl.SSLError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, httpx.HTTPError) as e:
            log.info("%s:%s failed: %s", host, port, type(e).__name__)
            with contextlib.suppress(Exception):
                writer.write(BAD_GATEWAY)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            if token and host:
                line = {"at": round(started, 3), "turn": rules.turn if rules else None, "host": host, "port": port,
                        "verdict": verdict, "intercepted": tally["intercepted"], "out": tally["out"], "in": tally["in"],
                        "s": round(time.time() - started, 3)}
                if tally["intercepted"]:
                    line.update(method=tally.get("method"), path=tally.get("path"), status=tally["status"])
                self.platform.record(token, line)

    async def intercept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, up_r: asyncio.StreamReader,
                        up_w: asyncio.StreamWriter, host: str, substitutions: list[tuple[bytes, bytes]],
                        tally: dict[str, Any]) -> None:
        """TLS with the box as the server for `host`, TLS with the real server as a client; then one request, its
        headers' placeholders replaced, and everything after it relayed until either side closes."""
        await writer.start_tls(self.issuer.context(host), ssl_handshake_timeout=HANDSHAKE_S)
        await up_w.start_tls(self.client_tls, server_hostname=host, ssl_handshake_timeout=HANDSHAKE_S)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_S)
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        tally["method"], tally["path"] = parts[0], parts[1][:200] if len(parts) > 1 else ""
        sent = rewrite(head, substitutions)
        tally["out"] += len(sent)
        up_w.write(sent)
        await up_w.drain()
        await self.splice(reader, writer, up_r, up_w, tally)


def _status(data: bytes) -> int | None:
    line = data.split(b"\r\n", 1)[0]
    parts = line.split(b" ")
    return int(parts[1]) if len(parts) > 1 and parts[0].startswith(b"HTTP/") and parts[1].isdigit() else None


async def serve(settings: Settings, host: str = "0.0.0.0", port: int | None = None) -> None:  # noqa: S104 - its networks only
    door = Door(settings)
    listen = port or int(settings.door.rpartition(":")[2] or 3128)
    server = await asyncio.start_server(door.connect, host, listen, limit=HEAD_MAX)
    log.info("door listening on %s:%s", host, listen)
    async with server:
        await asyncio.gather(server.serve_forever(), door.platform.flush_forever())


def run(settings: Settings) -> None:
    asyncio.run(serve(settings))
