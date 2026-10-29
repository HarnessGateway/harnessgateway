"""The provider gateway (SPEC §7.3): the only process where an organization's provider key is in the clear.

A box calls its provider at http://hg-door:8080 with its turn token as the API key; the door relays the call here,
adding its own door token. The gateway honours the pair only together and only for a live turn, swaps them for the
organization's key, relays the provider API's own paths to the provider's base URL, and streams the answer back with no
read timeout. Every exchange is recorded for observability; a body only when the organization's level is `full`."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import ids
from ..domain.provider import WIRES
from ..limits import CAPTURE_BODY_MAX
from ..netguard import check_url
from ..services.platform import Platform
from ..services.providers import key_context

log = logging.getLogger("harnessgateway.gateway")
PREFIX = "/gateway"
DOOR_HEADER = "hg-door-token"
AUTH_HEADERS = frozenset({"x-api-key", "authorization", "api-key", "x-goog-api-key", DOOR_HEADER})
HOP = frozenset({"host", "connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer",
                 "transfer-encoding", "upgrade", "content-length"})
GRANT_CACHE_S = 10
GRANT_CACHE_MAX = 10_000
HOST_CHECK_S = 60


@dataclass(frozen=True)
class Grant:
    turn_id: str
    org_id: str
    level: str
    api: str
    base_url: str
    key: str


def credential(headers: Any, query: Any) -> str | None:
    """The one credential presented, when every credential header (and a Gemini-style `key` parameter) agrees."""
    values = {v.strip() for v in query.getlist("key")}
    for name in AUTH_HEADERS - {DOOR_HEADER}:
        if v := headers.get(name):
            values.add(v[7:].strip() if v.lower().startswith("bearer ") else v.strip())
    return values.pop() if len(values) == 1 else None


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": kind, "message": message}}, status_code=status)


class Gateway:
    def __init__(self, plat: Platform):
        self.plat = plat
        self._grants: dict[str, tuple[float, Grant | None]] = {}
        self._checked: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=None, write=None, pool=30), trust_env=True,
                                        follow_redirects=False)

    async def grant(self, token: str, door: str) -> Grant | None:
        pepper = self.plat.pepper
        cache_key = ids.digest(f"{token}\0{door}", pepper)
        hit = self._grants.get(cache_key)
        if hit and time.monotonic() - hit[0] < GRANT_CACHE_S:
            return hit[1]
        async with self.plat.db.platform() as tx:
            row = await tx.one("SELECT t.turn_id, t.org_id, t.capture, p.id AS provider_id, p.api, p.base_url, p.sealed_key "
                               "FROM turn_tokens t JOIN providers p ON p.id = t.provider_id WHERE t.digest = %s "
                               "AND t.door_digest = %s AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR "
                               "t.expires_at > now())", ids.digest(token, pepper), ids.digest(door, pepper))
        grant = None
        if row:
            key = await self.plat.vault.open(row["org_id"], key_context(row["provider_id"]), row["sealed_key"])
            grant = Grant(row["turn_id"], row["org_id"], row["capture"], row["api"], row["base_url"], key)
        if len(self._grants) > GRANT_CACHE_MAX:
            self._grants.clear()
        self._grants[cache_key] = (time.monotonic(), grant)
        return grant

    def _check_host(self, base_url: str) -> None:
        """The provider's host still resolves to addresses the platform may reach (a name can be repointed)."""
        if time.monotonic() - self._checked.get(base_url, 0) > HOST_CHECK_S:
            check_url(base_url, allow_private=self.plat.settings.allow_private_egress)
            self._checked[base_url] = time.monotonic()

    async def forward(self, request: Request, path: str) -> Any:
        token = credential(request.headers, request.query_params)
        door = request.headers.get(DOOR_HEADER, "")
        grant = await self.grant(token, door) if token and door else None
        if grant is None:
            return _error(401, "authentication_error", "the turn token is missing, mixed, not presented through its "
                                                       "turn's door, or no longer valid")
        wire = WIRES[grant.api]
        if not wire.relays(path):
            return _error(403, "permission_error", f"the gateway relays {grant.api} API calls under "
                                                   f"{', '.join(wire.paths)} only")
        await asyncio.to_thread(self._check_host, grant.base_url)
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in AUTH_HEADERS | HOP}
        name, value = wire.auth(grant.key)
        headers[name] = value
        full = grant.level == "full"
        headers["accept-encoding"] = "identity" if full else request.headers.get("accept-encoding", "identity")
        query = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "key"])
        url = grant.base_url + path + (f"?{query}" if query else "")
        started = time.time()
        upstream = await self.client.send(self.client.build_request(request.method, url, headers=headers, content=body),
                                          stream=True)
        captured = bytearray()
        size = 0

        async def relay() -> AsyncIterator[bytes]:
            nonlocal size
            try:
                async for chunk in upstream.aiter_raw():
                    size += len(chunk)
                    if full and len(captured) < CAPTURE_BODY_MAX:
                        captured.extend(chunk[:CAPTURE_BODY_MAX - len(captured)])
                    yield chunk
            finally:
                await upstream.aclose()
                data: dict[str, Any] = {"method": request.method, "path": path,
                                        "host": urlsplit(grant.base_url).hostname, "status": upstream.status_code,
                                        "started_at": started, "duration_s": round(time.time() - started, 3),
                                        "request_bytes": len(body), "response_bytes": size}
                if full:
                    data.update(request=body[:CAPTURE_BODY_MAX].decode("utf-8", "replace"),
                                response=bytes(captured).decode("utf-8", "replace"))
                self._spawn(self.record(grant, data))

        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}
        return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)

    def _spawn(self, work: Any) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def record(self, grant: Grant, data: dict[str, Any]) -> None:
        try:
            await self.plat.traces.write(grant.org_id, grant.turn_id, "gateway", data)
        except Exception:
            log.exception("recording a gateway exchange failed")

    async def close(self) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=10)
        await self.client.aclose()


def gateway_router(gw: Gateway) -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def proxy(request: Request, path: str) -> Any:
        try:
            return await gw.forward(request, "/" + path)
        except httpx.HTTPError as e:
            return _error(502, "api_error", f"provider unreachable: {type(e).__name__}")

    return router


def create_gateway(plat: Platform) -> FastAPI:
    """The gateway alone, for deployments that run it apart from the API."""
    gw = Gateway(plat)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await plat.open()
        try:
            yield
        finally:
            await gw.close()
            await plat.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(gateway_router(gw))
    return app
