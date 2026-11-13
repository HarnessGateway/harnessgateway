"""How a refusal reaches a caller: the OpenAI error envelope (spec §13.7)."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from harnessgateway.errors import Refused

log = logging.getLogger(__name__)


def envelope(message: str, kind: str, *, param: str | None = None, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


def install(app: FastAPI) -> None:
    @app.exception_handler(Refused)
    async def refused(_: Request, error: Refused) -> JSONResponse:
        return JSONResponse(envelope(error.message, error.type, param=error.param, code=error.code), error.status)

    @app.exception_handler(Exception)
    async def failed(request: Request, error: Exception) -> JSONResponse:
        log.error("%s %s failed", request.method, request.url.path, exc_info=error)
        return JSONResponse(envelope("the platform failed to carry out the request", "server_error"), 500)
