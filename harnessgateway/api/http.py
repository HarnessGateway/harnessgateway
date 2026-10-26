"""What every router shares: authentication dependencies, bounded body reading, the OpenAI error envelope, and the
middleware that limits bodies and meters client traffic."""
from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..errors import Forbidden, Invalid, Refusal, TooLarge, Unauthenticated
from ..limits import CONTROL_BODY_MAX, FILE_UPLOAD_MAX, GB, NODE_BODY_MAX, PACKAGE_UPLOAD_MAX, REQUEST_BODY_MAX
from ..services.platform import Platform
from ..services.principal import NodeIdentity, Principal

log = logging.getLogger("harnessgateway.api")


def platform(request: Request) -> Platform:
    return request.app.state.platform


def bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        raise Unauthenticated("an API key is required: Authorization: Bearer <key>")
    return auth[7:].strip()


async def principal(request: Request) -> Principal:
    p = await platform(request).tenancy.authenticate(bearer(request))
    request.state.org_id = p.org_id                       # the traffic meter bills this request to the organization
    return p


async def operator(request: Request) -> None:
    key = platform(request).settings.operator_key
    if not key or not hmac.compare_digest(bearer(request).encode(), key.encode()):
        raise Forbidden("the operator key does this")


async def node(request: Request) -> NodeIdentity:
    return await platform(request).scheduler.authenticate(bearer(request))


async def read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise Invalid("the request body is JSON") from None
    if not isinstance(body, dict):
        raise Invalid("the request body is a JSON object")
    return body


def error_response(e: Refusal) -> JSONResponse:
    return JSONResponse({"error": {"message": e.message, "type": e.type, "param": e.param, "code": e.code}},
                        status_code=e.status)


def body_limit(path: str, method: str) -> int:
    if path.startswith("/internal/v1/blobs/"):
        return 1024 * GB                                  # bounded by the organization's workspace size, checked by the node
    if path.startswith("/internal/"):
        return NODE_BODY_MAX
    if path == "/v1/files" or (path.startswith("/v1/containers/") and path.endswith("/files")):
        return FILE_UPLOAD_MAX + 64 * 1024
    if path == "/v1/harnesses" and method == "POST":
        return PACKAGE_UPLOAD_MAX
    if path == "/v1/responses":
        return REQUEST_BODY_MAX
    return CONTROL_BODY_MAX


class Guard:
    """ASGI middleware: refuses bodies over the route's limit (by Content-Length up front, and by counting as the body
    streams), and meters every authenticated request's bytes in and out against its organization."""

    def __init__(self, app: ASGIApp, platform: Platform):
        self.app, self.platform = app, platform

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit(scope["path"], scope["method"])
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await error_response(TooLarge(f"the body is over this route's limit of {limit} bytes"))(scope, receive, send)
            return
        counts = {"in": 0, "out": 0}

        async def counted_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                counts["in"] += len(message.get("body", b""))
                if counts["in"] > limit:
                    raise TooLarge(f"the body is over this route's limit of {limit} bytes")
            return message

        async def counted_send(message: Message) -> None:
            if message["type"] == "http.response.body":
                counts["out"] += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, counted_receive, counted_send)
        finally:
            org = (scope.get("state") or {}).get("org_id")
            if org:
                self.platform.metering.count_api_bytes(org, counts["out"], counts["in"])
