"""Layer 1: the egress door, a sandbox's only way out. One process, run as a script in a container of
its own on the sandbox's internal network, standard library only:

- the CONNECT door tunnels to an allowlist of host:port and refuses anything else with a 403. It
  resolves names itself, so the sandbox needs no DNS, and it connects to the address it checked, so a
  name that resolves to a private address is refused and cannot be rebound past the check;
- the provider forwarder relays bytes between the sandbox and the platform's gateway, which holds the
  keys. The sandbox calls http://hg-provider:8080 with its session token; the forwarder opens the
  gateway (TLS when its URL is https) and copies both ways. It reads nothing.

It logs one line per decision (host, or the forward) on stderr, never a token or a body.

    python3 door.py LISTEN [HOST ...] [--gateway LISTEN URL] [--allow-private]
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import sys
from urllib.parse import urlsplit

READY = "door listening"
DOOR_PORT, GATEWAY_PORT = 3128, 8080
END = b"\r\n\r\n"
REFUSED = b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
ESTABLISHED = b"HTTP/1.1 200 Connection established\r\n\r\n"
FAILURES = (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError, ValueError)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def normalize(host: str) -> str:
    """'api.x.com' -> 'api.x.com:443'; a URL -> its host:port; a host with a port keeps it."""
    host = host.strip().lower().rstrip("/")
    if host.startswith(("https://", "http://")):
        u = urlsplit(host)
        return f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}"
    return host if ":" in host else f"{host}:443"


def is_public(address: str) -> bool:
    """A routable address: not loopback, link-local, private, multicast, reserved or unspecified."""
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


def check_host(host: str, allow_private: bool = False) -> str:
    """The host as a plain name or address, or ValueError saying why it is refused before any
    resolution: an address literal that is not public, or a name that is not a name."""
    name = normalize(host).rsplit(":", 1)[0].strip("[]")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        if not name or name == "localhost" or name.endswith(".localhost") or name.startswith("-") \
                or any(c in name for c in " /\\@"):
            raise ValueError(f"{host!r} is not a host name") from None
        return name
    if not allow_private and not is_public(name):
        raise ValueError(f"{host!r} is not a public address")
    return name


async def resolve(host: str, port: int, allow_private: bool) -> list[str]:
    """The addresses `host` resolves to that may be connected to, in order."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [info[4][0] for info in infos]
    return [a for a in addresses if allow_private or is_public(a)]


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    finally:
        dst.close()


async def connect(addresses: list[str], port: int, tls: ssl.SSLContext | None = None, server_hostname: str | None = None):
    """A connection to the first address that answers."""
    last: Exception = OSError("no address")
    for address in addresses:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port, ssl=tls, server_hostname=server_hostname), timeout=15)
        except (OSError, TimeoutError) as e:
            last = e
    raise last


class Door:
    def __init__(self, allowed: list[str], allow_private: bool):
        self.allowed = {normalize(h) for h in allowed if h.strip()}
        self.allow_private = allow_private

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(END), timeout=10)
            words = head.split(b"\r\n", 1)[0].decode(errors="replace").split()
            target = words[1].lower() if len(words) == 3 and words[0] == "CONNECT" else ""
            allowed = target in self.allowed
            addresses: list[str] = []
            if allowed:
                host, port = target.rsplit(":", 1)
                try:
                    addresses = await resolve(host, int(port), self.allow_private)
                except OSError:
                    addresses = []
            log(f"door {'allow' if allowed and addresses else 'deny'} {target or '(not a CONNECT)'}")
            if not addresses:
                writer.write(REFUSED)
                await writer.drain()
                return
            try:
                up_r, up_w = await connect(addresses, int(port))
            except (OSError, TimeoutError):
                writer.write(BAD_GATEWAY)
                await writer.drain()
                return
            writer.write(ESTABLISHED)
            await writer.drain()
            await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer), return_exceptions=True)
        except FAILURES:
            pass
        finally:
            writer.close()


class Forwarder:
    """Relays a sandbox's provider calls to the gateway, as bytes."""

    def __init__(self, url: str):
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"the gateway URL must be http(s)://host[:port]: {url!r}")
        self.host, self.port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        self.tls = ssl.create_default_context() if u.scheme == "https" else None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                up_r, up_w = await connect([self.host], self.port, self.tls, self.host if self.tls else None)
            except (OSError, TimeoutError) as e:
                log(f"gateway unreachable {type(e).__name__}")
                writer.write(BAD_GATEWAY)
                await writer.drain()
                return
            log("gateway forward")
            await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer), return_exceptions=True)
        except FAILURES:
            pass
        finally:
            writer.close()


async def serve(listen: str, allowed: list[str], forward: Forwarder | None, forward_listen: str, allow_private: bool) -> None:
    servers = []
    handlers = [(Door(allowed, allow_private).handle, listen)]
    if forward:
        handlers.append((forward.handle, forward_listen))
    for handle, address in handlers:
        host, port = address.rsplit(":", 1)
        servers.append(await asyncio.start_server(handle, host, int(port)))
    log(READY)
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main(argv: list[str]) -> None:
    allow_private = "--allow-private" in argv
    argv = [a for a in argv if a != "--allow-private"]
    forward, forward_listen = None, ""
    if "--gateway" in argv:
        i = argv.index("--gateway")
        forward_listen, url = argv[i + 1:i + 3]
        argv = argv[:i]
        forward = Forwarder(url)
    asyncio.run(serve(argv[0], argv[1:], forward, forward_listen, allow_private))


if __name__ == "__main__":
    main(sys.argv[1:])
