"""Layer 4: the provider gateway, the one process where a provider's key is in the clear. A sandbox
reaches it through its door's forwarder with the turn's session token as its only credential; the
gateway swaps the token for the key, allows only the API shape's own paths, forwards over TLS,
counts the call for metering, and keeps the exchange (request and response bodies, bounded) as the
turn's capture. The key never enters a sandbox or a worker, and never appears in a capture.

It is a plain HTTP/1.1 server on its own port, standard library only, so the same code serves the
`gateway` role alone or inside `all`. One request per connection.
"""
from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from dataclasses import dataclass
from typing import Any

from .blobs import Blobs
from .store import Provider, Store

END = b"\r\n\r\n"
AUTH_HEADERS = {"x-api-key", "authorization", "api-key"}
HOP_HEADERS = {"host", "connection", "proxy-connection", "keep-alive", "proxy-authorization", "te", "upgrade", "content-length", "transfer-encoding"}
OPENAI = (r"/v1/chat/completions", r"/v1/responses(/[^/]+(/[a-z_]+)?)?", r"/v1/embeddings", r"/v1/models(/[^/]+)?")
PATHS = {"anthropic-messages": (r"/v1/messages(/count_tokens)?", r"/v1/models(/[^/]+)?"),
         "openai-chat": OPENAI, "openai-responses": OPENAI,          # one API, two header shapes; a CLI may use either endpoint
         "none": (r".*",)}
CAPTURE_MAX = 4 * 1024 * 1024                         # per body kept in a capture
FAILURES = (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError, ValueError)


def refusal(status: int, reason: str) -> bytes:
    body = json.dumps({"error": {"message": reason}}).encode()
    return (f"HTTP/1.1 {status} {reason[:40]}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n").encode() + body


def allowed(api: str, path: str, prefix: str = "") -> bool:
    """The path is under the provider's own path and ends in one of the API shape's endpoints, whatever
    the provider mounts the API at (`/v1/…` on one host, `/anthropic/v1/…` on another)."""
    return path.startswith(prefix) and any(re.fullmatch(r"(?:/[^/]+)*?" + p, path) for p in PATHS[api])


def credential(headers: list[tuple[str, str]]) -> str | None:
    """The one credential presented, if every credential header carries the same value."""
    presented = {v[7:].strip() if v.lower().startswith("bearer ") else v.strip() for k, v in headers if k.lower() in AUTH_HEADERS}
    return presented.pop() if len(presented) == 1 else None


def auth_header(api: str, key: str) -> str:
    if api == "none" or not key:
        return ""
    return f"x-api-key: {key}" if api == "anthropic-messages" else f"Authorization: Bearer {key}"


@dataclass
class Exchange:
    """One relayed call, as kept."""
    at: float
    method: str
    path: str
    status: str = ""
    request: bytes = b""
    response: bytes = b""
    bytes_in: int = 0
    bytes_out: int = 0


async def read_body(reader: asyncio.StreamReader, headers: list[tuple[str, str]]) -> bytes:
    """A request body, by its Content-Length or chunked encoding."""
    h = {k.lower(): v for k, v in headers}
    if "content-length" in h:
        return await reader.readexactly(int(h["content-length"]))
    if h.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                return body
            body += await reader.readexactly(size)
            await reader.readline()
    return b""


class Gateway:
    def __init__(self, store: Store, blobs: Blobs, tls: ssl.SSLContext | None = None):
        self.store, self.blobs = store, blobs
        self._tls = tls or ssl.create_default_context()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await asyncio.wait_for(reader.readuntil(END), timeout=30)).decode("latin-1")
            lines = head.split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = [(k.strip(), v.strip()) for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)]
            body = await read_body(reader, headers)
            token = credential(headers)
            grant = self.store.resolve_token(token) if token else None
            if not grant:
                writer.write(refusal(401, "the session token is missing, mixed or expired"))
                return
            provider = self.store.provider(grant["org"], grant["provider"])
            path = target.split("?", 1)[0]
            if not allowed(provider.api, path, provider.path):
                writer.write(refusal(403, f"the gateway does not relay {method} {path} for {provider.api}"))
                return
            exchange = Exchange(time.time(), method, path, request=body[:CAPTURE_MAX], bytes_in=len(body))
            try:
                await self._relay(provider, method, target, headers, body, writer, exchange)
            finally:
                self._record(grant, exchange)
        except FAILURES as e:
            try:
                writer.write(refusal(502, f"gateway error {type(e).__name__}"))
            except Exception:
                pass
        finally:
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()

    async def _relay(self, provider: Provider, method: str, target: str, headers: list[tuple[str, str]], body: bytes,
                     writer: asyncio.StreamWriter, exchange: Exchange) -> None:
        from urllib.parse import urlsplit
        u = urlsplit(provider.url)
        host, port = u.hostname, u.port or 443
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=self._tls, server_hostname=host), timeout=15)
        try:
            kept = [f"{k}: {v}" for k, v in headers if k.lower() not in AUTH_HEADERS | HOP_HEADERS]
            auth = auth_header(provider.api, self.store.open_key(provider))
            up_w.write("\r\n".join([f"{method} {target} HTTP/1.1", f"Host: {host if port == 443 else f'{host}:{port}'}",
                                    "Connection: close", f"Content-Length: {len(body)}", *([auth] if auth else []), *kept, "", ""]).encode("latin-1"))
            up_w.write(body)
            await up_w.drain()
            answer = (await up_r.readuntil(END)).decode("latin-1").split("\r\n")
            exchange.status = answer[0].split(" ", 2)[1] if answer[0].count(" ") >= 1 else "?"
            kept_answer = [line for line in answer[1:] if line and line.split(":", 1)[0].strip().lower() not in ("connection", "keep-alive")]
            writer.write("\r\n".join([answer[0], *kept_answer, "Connection: close", "", ""]).encode("latin-1"))
            await writer.drain()
            while chunk := await up_r.read(65536):
                exchange.bytes_out += len(chunk)
                if len(exchange.response) < CAPTURE_MAX:
                    exchange.response += chunk[:CAPTURE_MAX - len(exchange.response)]
                writer.write(chunk)
                await writer.drain()
        finally:
            up_w.close()

    def _record(self, grant: dict[str, Any], exchange: Exchange) -> None:
        """The exchange into the turn's capture and the turn's usage; a failure here never fails the call."""
        try:
            rid, tenant = grant["response"], grant["org"]
            usage = self.store.records.get("usage", tenant, rid) or {"calls": 0, "bytes_in": 0, "bytes_out": 0, "seconds": 0, "at": exchange.at}
            usage = {**usage, "calls": usage["calls"] + 1, "bytes_in": usage["bytes_in"] + exchange.bytes_in,
                     "bytes_out": usage["bytes_out"] + exchange.bytes_out}
            self.store.records.put("usage", tenant, rid, usage)
            key = f"tenants/{tenant}/sessions/{grant['session']}/turns/{rid}/provider/{usage['calls']:04d}.json"
            self.blobs.put(key, json.dumps({"at": exchange.at, "method": exchange.method, "path": exchange.path,
                                            "status": exchange.status, "request": exchange.request.decode("utf-8", "replace"),
                                            "response": exchange.response.decode("utf-8", "replace")}).encode())
        except Exception:
            pass


async def serve(gateway: Gateway, host: str, port: int) -> None:
    server = await asyncio.start_server(gateway.handle, host, port)
    async with server:
        await server.serve_forever()
