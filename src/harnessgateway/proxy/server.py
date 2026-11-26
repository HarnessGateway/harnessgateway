"""The egress proxy: the only thing a sandbox can connect to.

It speaks HTTP proxy: CONNECT for a tunnel, and absolute http:// requests. Every connection is checked against
its sandbox's policy and against the addresses nothing may reach. Bytes to a host with no secret pass unread. A
host a secret is bound to is answered in the installation's own name, so the request can be read, the secret put
in, and the request sent on over a connection the proxy itself verified.
"""

import asyncio
import base64
import binascii
import contextlib
import ipaddress
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol
from urllib.parse import urlsplit

from harnessgateway.domain.egress import BoundSecret, Connection, HostEntry, Policy, is_forbidden_address
from harnessgateway.proxy.authority import Authority
from harnessgateway.proxy.http import HEAD_MAX, Head, put_secrets, read_head, refusal
from harnessgateway.proxy.tunnel import Inside

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 30
_HOP_HEADERS = ("proxy-authorization", "proxy-connection")     # for the proxy alone: never sent on
_GONE = (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError, TimeoutError, OSError)
_GRANTED = b"HTTP/1.1 200 Connection established\r\n\r\n"

PolicyOf = Callable[[str, str], Awaitable[Policy | None]]      # a sandbox's id and token -> what it may reach
Report = Callable[[Connection], None]                          # a connection a sandbox made, or was refused
Upstream = tuple[asyncio.StreamReader, asyncio.StreamWriter]


class Client(Protocol):
    """Where the proxy answers a client: its connection, or the inside of a tunnel."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


async def _copy(reader: asyncio.StreamReader, writer: Client, counted: list[int], which: int) -> None:
    with contextlib.suppress(*_GONE):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
            counted[which] += len(data)


async def _relay(reader: asyncio.StreamReader, client: Client, upstream: Upstream, counted: list[int]) -> None:
    """Pass bytes both ways until the host has finished answering, then close it."""
    sending = asyncio.create_task(_copy(reader, upstream[1], counted, 0))
    receiving = asyncio.create_task(_copy(upstream[0], client, counted, 1))
    try:
        await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
        if not receiving.done():                                # the client has finished sending: say so, and
            with contextlib.suppress(*_GONE):                   # let the answer finish
                if upstream[1].can_write_eof():
                    upstream[1].write_eof()
            await receiving
    finally:
        sending.cancel()
        receiving.cancel()
        await asyncio.gather(sending, receiving, return_exceptions=True)
        with contextlib.suppress(*_GONE):
            upstream[1].close()
            await upstream[1].wait_closed()


class EgressProxy:
    def __init__(self, policy_of: PolicyOf, report: Report, authority: Authority, *,
                 private_hosts: Sequence[str] = (), verify: ssl.SSLContext | None = None) -> None:
        self._policy_of = policy_of
        self._report = report
        self._authority = authority
        self._private_hosts = [HostEntry.parse(entry) for entry in private_hosts]
        self._verify = verify or ssl.create_default_context()      # how the proxy verifies a host it reads for

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
            with contextlib.suppress(*_GONE):
                writer.close()
                await writer.wait_closed()

    async def _policy(self, head: Head) -> Policy | None:
        """The policy of the sandbox a request comes from: it says which in its proxy credentials."""
        scheme, _, encoded = head.header("proxy-authorization").partition(" ")
        try:
            subject, colon, token = base64.b64decode(encoded, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        known = scheme.lower() == "basic" and colon and subject and token
        return await self._policy_of(subject, token) if known else None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def refuse(status: str, why: str, *headers: str) -> None:
            writer.write(refusal(status, why, *headers))
            await writer.drain()
        head = await read_head(reader)
        if head is None:
            return
        policy = await self._policy(head)
        if policy is None:
            return await refuse("407 Proxy Authentication Required", "this proxy serves running sandboxes only",
                                'Proxy-Authenticate: Basic realm="harnessgateway"')
        tunnel = head.method == "CONNECT"
        if tunnel:
            host, _, port_text = head.target.rpartition(":")
            host, port = host.strip("[]"), int(port_text) if port_text.isdigit() else 0
        else:
            try:
                url = urlsplit(head.target)
                host, port = (url.hostname or "", url.port or 80) if url.scheme == "http" else ("", 0)
            except ValueError:                                  # not a URL at all
                host, port = "", 0
        if not host or not 0 < port < 65536:
            return await refuse("400 Bad Request", "this proxy takes CONNECT host:port, and http:// URLs")
        def report(refused: str = "", sent: int = 0, received: int = 0) -> None:
            self._report(Connection(policy.org_id, policy.subject_id, policy.environment, host, port, refused,
                                    sent, received))
        addresses, refused = await self._addresses(policy, host, port)
        if refused:
            report(refused)
            return await refuse("403 Forbidden", refused)

        counted, secrets = [0, 0], policy.secrets_for(host, port)
        try:
            if not tunnel:
                head.target = (url.path or "/") + (f"?{url.query}" if url.query else "")
                head.drop(*_HOP_HEADERS)
                await self._send(reader, writer, head, secrets, addresses, port, None, counted)
            elif not secrets:                                   # nothing to put in: pass the bytes, unread
                if upstream := await self._connect(writer, addresses, port, None):
                    writer.write(_GRANTED)
                    await _relay(reader, writer, upstream, counted)
            else:
                writer.write(_GRANTED)
                await writer.drain()
                inside = await Inside.open(reader, writer, self._authority, host)
                if inside is not None:
                    try:
                        if (asked := await read_head(inside.reader)) is not None:
                            await self._send(inside.reader, inside, asked, secrets, addresses, port,
                                             host if inside.encrypted else None, counted)
                    finally:
                        inside.close()
        finally:
            report(sent=counted[0], received=counted[1])

    async def _addresses(self, policy: Policy, host: str, port: int) -> tuple[list[str], str]:
        """The addresses the host may be connected to at; or none, and why the connection is refused."""
        if refused := policy.egress.decide(host, port):
            return [], f"{refused} (environment {policy.environment!r})"
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError:
                return [], f"{host} could not be resolved"
            addresses = list(dict.fromkeys(str(info[4][0]) for info in found))
        if not any(entry.matches(host, port) for entry in self._private_hosts):
            for address in addresses:                           # checked as resolved: a name may point anywhere
                if is_forbidden_address(address):
                    return [], (f"{host} resolves to {address}, an address on a private or local network, "
                                "which a sandbox may not reach")
        return addresses, "" if addresses else f"{host} could not be resolved"

    async def _connect(self, client: Client, addresses: Sequence[str], port: int, verify_as: str | None,
                       ) -> Upstream | None:
        """A connection to the host, at the first of its addresses that answers: a name may have one this host
        cannot reach (an IPv6 address where there is no IPv6). They share the time allowed. With `verify_as` it
        is TLS, verified as that name."""
        failure: BaseException | None = None
        for address in addresses:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_S / len(addresses)):
                    return await asyncio.open_connection(address, port, ssl=self._verify if verify_as else None,
                                                         server_hostname=verify_as)
            except _GONE as error:
                failure = error
        with contextlib.suppress(*_GONE):
            client.write(refusal("502 Bad Gateway", f"the host could not be reached: {failure}"))
            await client.drain()
        return None

    async def _send(self, reader: asyncio.StreamReader, client: Client, head: Head, secrets: Sequence[BoundSecret],
                    addresses: Sequence[str], port: int, verify_as: str | None, counted: list[int]) -> None:
        """Send one request the proxy has read on to its host, with the host's secrets in place, and relay."""
        put_secrets(head, secrets)
        if upstream := await self._connect(client, addresses, port, verify_as):
            sent = head.encode()
            upstream[1].write(sent)
            counted[0] += len(sent)
            await _relay(reader, client, upstream, counted)
