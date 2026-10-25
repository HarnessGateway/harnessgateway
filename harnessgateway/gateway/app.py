"""The provider gateway, the only process where an organization's provider key is in the clear.

A sandbox calls its provider through the egress door, which relays to this gateway, presenting the turn's token as its
API key. The gateway swaps the token for the organization's real key, allows only the provider API's own paths,
forwards over TLS and streams the answer back. With observability on, it keeps each exchange (bodies bounded) as a
capture of the turn. The key never enters a sandbox or a node and never appears in a capture or a log."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import ids
from ..blobs import org_key
from ..limits import CAPTURE_BODY_MAX
from ..netguard import check_url
from ..services.platform import Platform
from ..services.providers import context

log = logging.getLogger("harnessgateway.gateway")
AUTH_HEADERS = {"x-api-key", "authorization", "api-key"}
HOP = {"host", "connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer",
       "transfer-encoding", "upgrade", "content-length"}
PATHS = {
    "anthropic": [r"/v1/messages(/count_tokens)?", r"/v1/models(/[^/]+)?"],
    "openai": [r"/v1/chat/completions", r"/v1/completions", r"/v1/responses(/[A-Za-z0-9_-]+(/[a-z_]+)?)?",
               r"/v1/embeddings", r"/v1/models(/[^/]+)?"],
}
CACHE_S = 10


@dataclass(frozen=True)
class Grant:
    turn_id: str
    org_id: str
    provider_id: str
    capture: bool
    api: str
    base_url: str
    key: str


def credential(headers: Any) -> str | None:
    """The one credential presented, if every credential header carries the same value."""
    values = set()
    for name in AUTH_HEADERS:
        v = headers.get(name)
        if v:
            values.add(v[7:].strip() if v.lower().startswith("bearer ") else v.strip())
    return values.pop() if len(values) == 1 else None


def allowed(api: str, path: str) -> bool:
    return any(re.fullmatch(p, path) for p in PATHS.get(api, ()))


def auth_header(api: str, key: str) -> tuple[str, str]:
    return ("x-api-key", key) if api == "anthropic" else ("authorization", f"Bearer {key}")


class Gateway:
    def __init__(self, plat: Platform):
        self.plat = plat
        self._grants: dict[str, tuple[float, Grant | None]] = {}
        self._checked: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=900, write=60, pool=30), trust_env=True,
                                        follow_redirects=False, http2=False)

    async def grant(self, token: str) -> Grant | None:
        digest = ids.digest(token, self.plat.pepper)
        hit = self._grants.get(digest)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        async with self.plat.db.platform() as tx:
            row = await tx.one("SELECT t.turn_id, t.org_id, t.provider_id, t.capture, p.api, p.base_url, p.sealed_key "
                               "FROM turn_tokens t JOIN providers p ON p.id = t.provider_id WHERE t.digest = %s "
                               "AND t.revoked_at IS NULL AND t.expires_at > now()", digest)
        grant = None
        if row:
            key = await asyncio.to_thread(self.plat.sealer.open, row["sealed_key"], context(row["provider_id"]))
            grant = Grant(row["turn_id"], row["org_id"], row["provider_id"], row["capture"], row["api"], row["base_url"], key)
        if len(self._grants) > 10_000:
            self._grants.clear()
        self._grants[digest] = (time.monotonic(), grant)
        return grant

    def _check_host(self, base_url: str) -> None:
        if time.monotonic() - self._checked.get(base_url, 0) > 60:
            check_url(base_url, allow_private=self.plat.settings.allow_private_egress)
            self._checked[base_url] = time.monotonic()

    async def forward(self, request: Request, path: str) -> Any:
        token = credential(request.headers)
        grant = await self.grant(token) if token else None
        if grant is None:
            return JSONResponse({"error": {"type": "authentication_error",
                                           "message": "the turn token is missing, mixed or expired"}}, status_code=401)
        if not allowed(grant.api, path):
            return JSONResponse({"error": {"type": "permission_error",
                                           "message": f"the gateway does not relay {request.method} {path} for {grant.api}"}},
                                status_code=403)
        await asyncio.to_thread(self._check_host, grant.base_url)
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in AUTH_HEADERS | HOP}
        name, value = auth_header(grant.api, grant.key)
        headers[name] = value
        headers["accept-encoding"] = request.headers.get("accept-encoding", "identity")   # relayed raw: the client decodes
        url = grant.base_url + path + (f"?{request.url.query}" if request.url.query else "")
        started = time.time()
        upstream = await self.client.send(self.client.build_request(request.method, url, headers=headers, content=body),
                                          stream=True)
        captured = bytearray()

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    if grant.capture and len(captured) < CAPTURE_BODY_MAX:
                        captured.extend(chunk[:CAPTURE_BODY_MAX - len(captured)])
                    yield chunk
            finally:
                if grant.capture:                         # its own task: a client that hangs up cancels this one
                    self._spawn(self.capture(grant, request.method, path, upstream.status_code, body, bytes(captured),
                                             started))
                await upstream.aclose()

        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}
        return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)

    def _spawn(self, work: Any) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def capture(self, grant: Grant, method: str, path: str, status: int, request: bytes, response: bytes,
                      started: float) -> None:
        """One exchange into the turn's captures; a failure here never fails the call."""
        try:
            key = org_key(grant.org_id, "turns", grant.turn_id, "provider", f"{int(started * 1000):013d}-{ids.new('op')[4:12]}.json")
            doc = {"at": started, "duration_s": round(time.time() - started, 3), "method": method, "path": path,
                   "status": status, "host": urlsplit(grant.base_url).hostname,
                   "request": request[:CAPTURE_BODY_MAX].decode("utf-8", "replace"),
                   "response": response.decode("utf-8", "replace")}
            await self.plat.blobs.put(key, json.dumps(doc).encode(), org_id=grant.org_id, kind="capture")
        except Exception:
            log.exception("capturing a provider exchange failed")


def create_gateway(plat: Platform) -> FastAPI:
    gw = Gateway(plat)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await plat.open()
        try:
            yield
        finally:
            if gw._tasks:
                await asyncio.wait(gw._tasks, timeout=10)  # captures in flight
            await gw.client.aclose()
            await plat.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(request: Request, path: str) -> Any:
        try:
            return await gw.forward(request, "/" + path)
        except httpx.HTTPError as e:
            return JSONResponse({"error": {"type": "api_error", "message": f"provider unreachable: {type(e).__name__}"}},
                                status_code=502)

    return app
