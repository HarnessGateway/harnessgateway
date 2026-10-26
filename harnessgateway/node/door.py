"""The egress door: a sandbox's only way out. One process, standard library only, run in a small container of its own
that sits on the sandbox's internal network (as `hg-egress`) and on the node's uplink network.

- port 3128, a CONNECT proxy to an allowlist of host[:port] (default port 443). It resolves names itself, so the
  sandbox needs no DNS, and connects only to the address it checked: a name that resolves to a private address is
  refused unless private egress is allowed. Anything else gets 403.
- port 8080, the provider forwarder: bytes relayed to the platform's gateway (over TLS when its URL is https). The
  sandbox calls http://hg-egress:8080 with its turn token; the gateway holds the key.

Every byte through either port is counted, and the counters are written to a file the node meters. One line per
decision goes to stderr; never a token or a body.

    python -m harnessgateway.node.door --gateway URL --counters FILE [--allow HOST ...] [--upstream-proxy URL]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

COUNTERS = {"out": 0, "in": 0}
IDLE_S = 600


def log(message: str) -> None:
    print(f"door: {message}", file=sys.stderr, flush=True)


def public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def permitted(host: str, port: int, allow: set[str]) -> bool:
    return f"{host}:{port}" in allow or (port == 443 and host in allow)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str) -> None:
    try:
        while data := await asyncio.wait_for(reader.read(65536), IDLE_S):
            COUNTERS[direction] += len(data)
            writer.write(data)
            await writer.drain()
    except (TimeoutError, ConnectionError, OSError):
        pass                                               # either side went away: the tunnel is over
    finally:
        with contextlib.suppress(Exception):               # the peer already went away
            writer.close()


async def splice(a_r, a_w, b_r, b_w) -> None:
    await asyncio.gather(pipe(a_r, b_w, "out"), pipe(b_r, a_w, "in"))


async def open_upstream(host: str, port: int, upstream: str | None) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A TCP stream to host:port, directly or through the operator's upstream CONNECT proxy."""
    if not upstream:
        return await asyncio.wait_for(asyncio.open_connection(host, port), 15)
    u = urlsplit(upstream)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or 3128), 15)
    writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
    if b" 200" not in head.split(b"\r\n", 1)[0]:
        writer.close()
        raise ConnectionError(f"upstream proxy refused {host}:{port}")
    return reader, writer


class Door:
    def __init__(self, allow: set[str], gateway: str, upstream: str | None, allow_private: bool):
        self.allow, self.upstream, self.allow_private = allow, upstream, allow_private
        g = urlsplit(gateway)
        self.gw_host, self.gw_port = g.hostname or "", g.port or (443 if g.scheme == "https" else 80)
        self.gw_tls = ssl.create_default_context() if g.scheme == "https" else None

    async def connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            method, target = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[:2]
            if method != "CONNECT":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                log(f"refused {method} (only CONNECT)")
                return
            host, _, port_s = target.rpartition(":")
            host, port = host.strip("[]").lower(), int(port_s or 443)
            if not permitted(host, port, self.allow):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                log(f"refused {host}:{port} (not allowed)")
                return
            target_host = host
            if not self.upstream:
                infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
                addresses = [i[4][0] for i in infos if self.allow_private or public(i[4][0])]
                if not addresses:
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    log(f"refused {host}:{port} (no public address)")
                    return
                target_host = addresses[0]               # connect to the address checked, not the name
            up_r, up_w = await open_upstream(target_host, port, self.upstream)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            log(f"tunnel {host}:{port}")
            await splice(reader, writer, up_r, up_w)
        except (TimeoutError, ConnectionError, OSError, ValueError, asyncio.IncompleteReadError) as e:
            log(f"connect failed: {type(e).__name__}")
            with contextlib.suppress(Exception):           # the client is gone
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        finally:
            writer.close()

    async def provider(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(
                self.gw_host, self.gw_port, ssl=self.gw_tls, server_hostname=self.gw_host if self.gw_tls else None), 15)
            await splice(reader, writer, up_r, up_w)
        except (TimeoutError, ConnectionError, OSError) as e:
            log(f"gateway unreachable: {type(e).__name__}")
            writer.close()


async def write_counters(path: str) -> None:
    while True:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({**COUNTERS, "at": time.time()}, f)
        os.replace(tmp, path)
        await asyncio.sleep(2)


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--counters", required=True)
    ap.add_argument("--allow", nargs="*", default=[])
    ap.add_argument("--upstream-proxy", default="")
    ap.add_argument("--allow-private", action="store_true")
    a = ap.parse_args(argv)
    door = Door({h.lower() for h in a.allow}, a.gateway, a.upstream_proxy or None, a.allow_private)
    s1 = await asyncio.start_server(door.connect, "0.0.0.0", 3128)        # noqa: S104 - only reachable on its networks
    s2 = await asyncio.start_server(door.provider, "0.0.0.0", 8080)      # noqa: S104
    log(f"ready: {len(door.allow)} hosts allowed")
    async with s1, s2:
        await asyncio.gather(s1.serve_forever(), s2.serve_forever(), write_counters(a.counters))


if __name__ == "__main__":
    asyncio.run(main())
