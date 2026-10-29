"""What every router shares: who is asking (an extension's principal or an API key, the operator key, a node
credential), bounded JSON bodies, uploads spooled to disk, the OpenAI error envelope, and the body-size guard."""
from __future__ import annotations

import contextlib
import hmac
import json
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile  # what request.form() yields (FastAPI's is a subclass)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import ApiError, Forbidden, Invalid, TooLarge, Unauthenticated
from ..limits import (
    CONTROL_BODY_MAX,
    FILE_UPLOAD_MAX,
    KB,
    NODE_BODY_MAX,
    NODE_EVENTS_BODY_MAX,
    PACKAGE_UPLOAD_MAX,
    REQUEST_BODY_MAX,
)
from ..services.platform import BLOB_ROUTE, Platform
from ..services.principal import NodeIdentity, Principal

MULTIPART_OVERHEAD = 64 * KB


def platform(request: Request) -> Platform:
    return request.app.state.platform  # type: ignore[no-any-return]


def bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        raise Unauthenticated("a credential is required: Authorization: Bearer <key>")
    return auth[7:].strip()


async def principal(request: Request) -> Principal:
    plat = platform(request)
    p = await plat.extensions.authenticate(request)
    if p is None:
        p = await plat.keys.authenticate(bearer(request))
    if p is None:
        raise Unauthenticated("the API key is unknown or revoked, or its organization is suspended")
    return p


async def admin(request: Request) -> Principal:
    p = await principal(request)
    p.require_admin()
    return p


async def operator(request: Request) -> None:
    key = platform(request).settings.operator_key
    if not key or not hmac.compare_digest(bearer(request).encode(), key.encode()):
        raise Forbidden("the operator key does this")


async def node(request: Request) -> NodeIdentity:
    return await platform(request).scheduler.authenticate(bearer(request))


async def read_json(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        raise Invalid("the request body is JSON") from None
    if not isinstance(body, dict):
        raise Invalid("the request body is a JSON object")
    return body


@contextlib.asynccontextmanager
async def spooled(upload: UploadFile) -> AsyncIterator[Path]:
    """An uploaded file on disk, removed afterwards."""
    with tempfile.NamedTemporaryFile(prefix="hg-upload-") as tmp:
        await upload.seek(0)
        shutil.copyfileobj(upload.file, tmp, 1024 * 1024)
        tmp.flush()
        yield Path(tmp.name)


async def form_file(request: Request, field: str) -> tuple[UploadFile, dict[str, str]]:
    form = await request.form()
    upload = form.get(field)
    if not isinstance(upload, UploadFile):
        raise Invalid(f"a multipart form with a `{field}` file part", param=field)
    return upload, {k: v for k, v in form.items() if isinstance(v, str)}


def error_response(e: ApiError) -> JSONResponse:
    return JSONResponse(e.body(), status_code=e.status)


def body_limit(path: str, method: str) -> int | None:
    if path.startswith(BLOB_ROUTE):
        return None                                  # a session's saved state: the platform never limits a harness
    if path.startswith("/internal/turns/") and path.endswith("/events"):
        return NODE_EVENTS_BODY_MAX
    if path.startswith("/internal/"):
        return NODE_BODY_MAX
    if path == "/v1/files" or (path.startswith("/v1/containers/") and path.endswith("/files")):
        return FILE_UPLOAD_MAX + MULTIPART_OVERHEAD
    if path == "/v1/harnesses" and method == "POST":
        return PACKAGE_UPLOAD_MAX + MULTIPART_OVERHEAD
    if path == "/v1/responses":
        return REQUEST_BODY_MAX
    return CONTROL_BODY_MAX


class BodyLimit:
    """ASGI middleware refusing a body over its route's limit: by Content-Length up front, then by counting."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit(scope["path"], scope["method"])
        declared = dict(scope.get("headers") or []).get(b"content-length")
        too_large = TooLarge(f"the body is over this route's limit of {limit} bytes")
        if declared is not None and (not declared.isdigit() or (limit is not None and int(declared) > limit)):
            await error_response(too_large)(scope, receive, send)
            return
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if limit is not None and seen > limit:
                    raise too_large
            return message

        await self.app(scope, counted, send)
