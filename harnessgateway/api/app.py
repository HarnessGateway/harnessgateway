"""The api role's application: the core's routers and every extension's, the error envelope and the body-size guard,
bound to one Platform. The api runs no loops of its own: boxes call in, the worker does the rest."""
from __future__ import annotations

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
from . import identity, internal, manage, responses, storage
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


def create_app(plat: Platform) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await plat.open()
        try:
            yield
        finally:
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
        ready = await plat.operations.ready()
        return JSONResponse({"ok": all(ready.values()), **ready}, status_code=200 if all(ready.values()) else 503)

    for module in (responses, storage, manage, identity, internal):
        app.include_router(module.router)
    for router in plat.extensions.routers():
        app.include_router(router)
    app.add_middleware(BodyLimit)
    return app
