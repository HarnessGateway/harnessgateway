"""The egress proxy (spec §11): the only thing a sandbox can connect to.

It speaks HTTP proxy: CONNECT for HTTPS, and absolute http:// requests. Every connection is checked against
its turn's policy and against the addresses nothing may reach. Bytes to a host with no secret pass untouched.
A host a secret is bound to is answered with a certificate of the platform's own authority, so the request
can be read, the secret put in, and the request sent on over a verified connection of the proxy's own.

An intercepted connection carries one request: the proxy asks the host to close when it has answered. That
keeps the proxy from having to understand responses at all; they are relayed as they arrive.
"""

import asyncio
import base64
import binascii
import contextlib
import ipaddress
import logging
import socket
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from harnessgateway.domain.hosts import HostEntry, is_forbidden_address
from harnessgateway.domain.policy import BoundSecret, Policy
from harnessgateway.domain.sealing import substitute
from harnessgateway.proxy.authority import Authority
from harnessgateway.proxy.platform import PlatformClient

log = logging.getLogger(__name__)

HEAD_MAX = 64 * 1024
CONNECT_TIMEOUT_S = 30
_HOP = frozenset({"proxy-authorization", "proxy-connection"})
_GONE = (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError, TimeoutError, OSError)


@dataclass
class Head:
    """The first lines of an HTTP request: what it asks for, and its headers in order."""

    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key.lower() == name), None)

    def drop(self, *names: str) -> None:
        self.headers = [(key, value) for key, value in self.headers if key.lower() not in names]

    def encode(self) -> bytes:
        lines = [f"{self.method} {self.target} {self.version}", *(f"{key}: {value}" for key, value in self.headers)]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def read_head(reader: asyncio.StreamReader) -> Head | None:
    """The head of the next request; None when the client went away or sent something else."""
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ssl.SSLError):
        return None
    first, *rest = raw[:-4].decode("latin-1").split("\r\n")
    parts = first.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    headers = []
    for line in rest:
        key, colon, value = line.partition(":")
        if not colon:
            return None
        headers.append((key.strip(), value.strip()))
    return Head(parts[0], parts[1], parts[2], headers)


def apply_secrets(head: Head, secrets: Sequence[BoundSecret]) -> None:
    """Put a host's secrets into a request bound for it (spec §10.4), and ask for the connection to be closed
    after the answer."""
    values = {secret.placeholder: secret.value for secret in secrets if secret.placeholder}
    path, question, query = head.target.partition("?")
    head.target = path + question + substitute(query, values)
    head.headers = [(key, substitute(value, values)) for key, value in head.headers]
    for secret in secrets:
        if secret.header:
            head.drop(secret.header.lower())
            head.headers.append((secret.header, secret.prefix + secret.value))
    if "upgrade" not in (head.header("connection") or "").lower():
        head.drop("connection", "keep-alive")
        head.headers.append(("Connection", "close"))


def _credentials(head: Head) -> tuple[str, str] | None:
    scheme, _, encoded = (head.header("proxy-authorization") or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, colon, password = base64.b64decode(encoded, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return (user, password) if colon and user and password else None


def _refusal(status: str, text: str, *extra: str) -> bytes:
    body = (text + "\n").encode()
    head = [f"HTTP/1.1 {status}", "Content-Type: text/plain; charset=utf-8", f"Content-Length: {len(body)}",
            "Connection: close", *extra]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, counts: list[int], index: int) -> None:
    with contextlib.suppress(*_GONE):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
            counts[index] += len(data)


async def _relay(client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                 upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter], counts: list[int]) -> None:
    """Pass bytes both ways until the far side has finished answering."""
    up = asyncio.create_task(_pipe(client[0], upstream[1], counts, 0))
    down = asyncio.create_task(_pipe(upstream[0], client[1], counts, 1))
    try:
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        if not down.done():                                     # the client has finished sending: say so, and
            with contextlib.suppress(*_GONE):                   # let the answer finish
                if upstream[1].can_write_eof():
                    upstream[1].write_eof()
            await down
    finally:
        up.cancel()
        down.cancel()
        await asyncio.gather(up, down, return_exceptions=True)


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(*_GONE):
        writer.close()
        await writer.wait_closed()


class EgressProxy:
    def __init__(self, platform: PlatformClient, authority: Authority, *, private_hosts: Sequence[HostEntry] = (),
                 upstream_tls: ssl.SSLContext | None = None) -> None:
        self._platform = platform
        self._authority = authority
        self._private_hosts = tuple(private_hosts)
        self._upstream_tls = upstream_tls or ssl.create_default_context()

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=HEAD_MAX)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except _GONE:
            pass
        except Exception:
            log.exception("a connection through the proxy failed")
        finally:
            await _close(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await read_head(reader)
        if head is None:
            return
        credentials = _credentials(head)
        policy = await self._platform.policy(*credentials) if credentials else None
        if policy is None:
            writer.write(_refusal("407 Proxy Authentication Required",
                                  "this proxy serves sandboxes of running turns only",
                                  'Proxy-Authenticate: Basic realm="harnessgateway"'))
            return await writer.drain()

        if head.method == "CONNECT":
            host, _, port_text = head.target.rpartition(":")
            host, port = host.strip("[]"), int(port_text) if port_text.isdigit() else 0
        else:
            try:
                url = urlsplit(head.target)
                host, port = (url.hostname or "", url.port or 80) if url.scheme == "http" else ("", 0)
            except ValueError:                                  # not a URL at all
                host, port = "", 0
        if not host or not 0 < port < 65536:
            writer.write(_refusal("400 Bad Request", "this proxy takes CONNECT host:port, and http:// URLs"))
            return await writer.drain()

        started, counts = time.time(), [0, 0]
        address, refused = await self._address(policy, host, port)
        if address is None:
            self._platform.record(policy, host=host, port=port, allowed=False, reason=refused, bytes_up=0,
                                  bytes_down=0, started_at=started)
            writer.write(_refusal("403 Forbidden", refused))
            return await writer.drain()
        try:
            secrets = policy.secrets_for(host, port)
            if head.method == "CONNECT":
                await self._tunnel((reader, writer), host, port, address, secrets, counts)
            else:
                await self._plain((reader, writer), head, port, address, secrets, counts)
        finally:
            self._platform.record(policy, host=host, port=port, allowed=True, reason="", bytes_up=counts[0],
                                  bytes_down=counts[1], started_at=started)

    async def _address(self, policy: Policy, host: str, port: int) -> tuple[str | None, str]:
        """The address to connect to; or None and why the connection is refused (spec §11.3, §11.4)."""
        decision = policy.egress.decide(host, port)
        if not decision.allowed:
            return None, f"{decision.reason} (environment {policy.environment!r})"
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError:
                return None, f"{host} could not be resolved"
            addresses = [str(info[4][0]) for info in found]
        if not any(entry.matches(host, port) for entry in self._private_hosts):
            for address in addresses:
                if is_forbidden_address(address):
                    return None, (f"{host} resolves to {address}, an address on a private or local network, "
                                  f"which a sandbox may not reach")
        return (addresses[0], "") if addresses else (None, f"{host} could not be resolved")

    async def _connect(self, client: asyncio.StreamWriter, address: str, port: int, *, tls_for: str | None = None,
                       ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                if tls_for is None:
                    return await asyncio.open_connection(address, port)
                return await asyncio.open_connection(address, port, ssl=self._upstream_tls, server_hostname=tls_for)
        except _GONE as e:
            with contextlib.suppress(*_GONE):
                client.write(_refusal("502 Bad Gateway", f"the host could not be reached: {e}"))
                await client.drain()
            return None

    async def _tunnel(self, client: tuple[asyncio.StreamReader, asyncio.StreamWriter], host: str, port: int,
                      address: str, secrets: Sequence[BoundSecret], counts: list[int]) -> None:
        reader, writer = client
        established = b"HTTP/1.1 200 Connection established\r\n\r\n"
        if not secrets:                                         # nothing to add: pass the bytes, unread
            upstream = await self._connect(writer, address, port)
            if upstream is None:
                return
            try:
                writer.write(established)
                await writer.drain()
                await _relay(client, upstream, counts)
            finally:
                await _close(upstream[1])
            return
        writer.write(established)
        await writer.drain()
        await writer.start_tls(self._authority.context_for(host))
        head = await read_head(reader)
        if head is None:
            return
        apply_secrets(head, secrets)
        upstream = await self._connect(writer, address, port, tls_for=host)
        if upstream is None:
            return
        await self._send(client, upstream, head, counts)

    async def _plain(self, client: tuple[asyncio.StreamReader, asyncio.StreamWriter], head: Head, port: int,
                     address: str, secrets: Sequence[BoundSecret], counts: list[int]) -> None:
        url = urlsplit(head.target)
        head.target = (url.path or "/") + (f"?{url.query}" if url.query else "")
        head.drop(*_HOP)
        apply_secrets(head, secrets)
        upstream = await self._connect(client[1], address, port)
        if upstream is None:
            return
        await self._send(client, upstream, head, counts)

    @staticmethod
    async def _send(client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter], head: Head, counts: list[int]) -> None:
        try:
            sent = head.encode()
            upstream[1].write(sent)
            await upstream[1].drain()
            counts[0] += len(sent)
            await _relay(client, upstream, counts)
        finally:
            await _close(upstream[1])
