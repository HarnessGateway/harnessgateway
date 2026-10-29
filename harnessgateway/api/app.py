"""The API role's application: the core's routers and every extension's, the error envelope, the body-size guard, and
the background loops (event bus, sweeper), all bound to one Platform."""
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
from ..errors import ApiError, Invalid
from ..services.platform import Platform
from . import internal, manage, responses, storage
from .http import BodyLimit, error_response

log = logging.getLogger("harnessgateway.api")


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def refusal(_: Request, e: ApiError) -> JSONResponse:
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


def create_api(plat: Platform, *, background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await plat.open()
        if plat.settings.node_join_token:
            await plat.scheduler.ensure_token(plat.settings.node_join_token)
        plat.bus.start()
        tasks = [asyncio.create_task(plat.sweeper.run())] if background else []
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await plat.bus.stop()
            await plat.close()

    app = FastAPI(title="HarnessGateway", version=__version__, lifespan=lifespan, docs_url="/v1/docs", redoc_url=None,
                  openapi_url="/v1/openapi.json")
    app.state.platform = plat
    install_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        try:
            async with plat.db.platform() as tx:
                await tx.value("SELECT 1")
        except Exception as e:
            return JSONResponse({"ok": False, "database": type(e).__name__}, status_code=503)
        return JSONResponse({"ok": True})

    for module in (responses, storage, manage, internal):
        app.include_router(module.router)
    for router in plat.extensions.routers():
        app.include_router(router)
    app.add_middleware(BodyLimit)
    return app
