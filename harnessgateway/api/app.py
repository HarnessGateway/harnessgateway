"""The api role's application: routers, error envelope, guard middleware and the background loops (event bus,
housekeeping, the traffic meter's flusher), all bound to one `Platform`."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .. import __version__
from ..errors import Invalid, Refusal
from ..services.platform import Platform
from . import admin, internal, org, responses, storage
from .http import Guard, error_response

log = logging.getLogger("harnessgateway.api")


def create_app(plat: Platform, *, background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await plat.open()
        await plat.blobs.ensure_bucket()
        if plat.settings.node_join_token:
            await plat.scheduler.ensure_join_token(plat.settings.node_join_token)
        published = await plat.packages.publish_builtins()
        log.info("built-in harnesses: %s", ", ".join(sorted(published)) or "none")
        plat.bus.start()
        tasks = [asyncio.create_task(plat.housekeeping.run()), asyncio.create_task(plat.metering.flusher())] \
            if background else []
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await plat.bus.stop()
            await plat.metering.flush()
            await plat.close()

    app = FastAPI(title="HarnessGateway", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/v1/openapi.json")
    app.state.platform = plat

    @app.exception_handler(Refusal)
    async def refusal(_: Request, e: Refusal) -> JSONResponse:
        return error_response(e)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, e: RequestValidationError) -> JSONResponse:
        err = e.errors()[0] if e.errors() else {}
        return error_response(Invalid(f"{'.'.join(map(str, err.get('loc', ())))}: {err.get('msg', 'invalid')}"))

    @app.exception_handler(Exception)
    async def crash(request: Request, e: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": {"message": "internal error", "type": "server_error", "param": None,
                                       "code": None}}, status_code=500)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            async with plat.db.platform() as tx:
                await tx.value("SELECT 1")
            checks["database"] = "ok"
        except Exception as e:                            # readiness reports which dependency fails, never raises
            checks["database"] = f"failed: {type(e).__name__}"
        try:
            await plat.blobs.probe()
            checks["storage"] = "ok"
        except Exception as e:
            checks["storage"] = f"failed: {type(e).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)

    for module in (responses, storage, org, admin, internal):
        app.include_router(module.router)
    app.add_middleware(Guard, platform=plat)
    return app
