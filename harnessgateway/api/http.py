"""What every router shares: who is asking (an extension's principal, an API key or a sign-in, the operator key or a
platform admin, a box with its token, a door with the door key), bounded JSON bodies, uploads spooled to disk, the
OpenAI error envelope, and the body-size guard."""
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

from ..domain.access import ADMIN, Principal
from ..errors import ApiError, Forbidden, Invalid, TooLarge, Unauthenticated
from ..limits import (
    BOX_BODY_MAX,
    BOX_EVENTS_BODY_MAX,
    CONTROL_BODY_MAX,
    FILE_UPLOAD_MAX,
    KB,
    MANIFEST_MAX,
    PACKAGE_UPLOAD_MAX,
    REQUEST_BODY_MAX,
)
from ..services.platform import Platform

MULTIPART_OVERHEAD = 64 * KB
OPERATOR = "operator"                  # the actor the operator key acts as
UNKNOWN = "the credential is unknown, expired or revoked, or its organization is suspended"
UNBOUNDED_SUFFIXES = ("/archive", "/logs/stdout", "/logs/stderr")


def platform(request: Request) -> Platform:
    return request.app.state.platform  # type: ignore[no-any-return]


def bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        raise Unauthenticated("a credential is required: Authorization: Bearer <key or sign-in token>")
    return auth[7:].strip()


def client(request: Request) -> str:
    """Who signed in, for the sign-in row and the audit log: the address and the user agent."""
    return f"{request.client.host if request.client else ''} {request.headers.get('user-agent', '')}".strip()[:200]


async def signed_in(request: Request) -> dict[str, Any]:
    """The sign-in behind the bearer: {token, account, memberships}. For the routes about one's own account."""
    token = bearer(request)
    found = await platform(request).accounts.authenticate(token)
    if found is None:
        raise Unauthenticated("a sign-in token is required here" if token.startswith("hg_") else UNKNOWN)
    return {"token": token, **found}


async def principal(request: Request) -> Principal:
    """An extension's principal, an API key's, or a sign-in's in the organization it belongs to (SPEC §10.5)."""
    plat = platform(request)
    p = await plat.extensions.authenticate(request)
    if p is not None:
        return p
    secret = bearer(request)
    p = await plat.keys.authenticate(secret)
    if p is not None:
        return p
    found = await plat.accounts.authenticate(secret)
    if found is None:
        raise Unauthenticated(UNKNOWN)
    account, memberships = found["account"], found["memberships"]
    if not memberships:
        raise Forbidden("the account belongs to no organization; the platform console is at /internal")
    org_id, role = memberships[0]["org_id"], memberships[0]["role"]
    return Principal(org_id, f"account:{account['id']}", role, platform_admin=account["platform_admin"],
                     account_id=account["id"], name=account["name"] or account["email"])


async def anyone(request: Request) -> None:
    """Any live credential: for what every caller may learn."""
    plat = platform(request)
    if await plat.extensions.authenticate(request) is not None:
        return
    secret = bearer(request)
    if await plat.keys.authenticate(secret) is None and await plat.accounts.authenticate(secret) is None:
        raise Unauthenticated(UNKNOWN)


async def admin(request: Request) -> Principal:
    p = await principal(request)
    if p.role != ADMIN:
        raise Forbidden("an admin does this")
    return p


async def operator(request: Request) -> str:
    """The platform scope (SPEC §10.5): the operator key, or a platform admin's sign-in. The actor, for the audit log."""
    plat = platform(request)
    secret = bearer(request)
    key = plat.settings.operator_key
    if key and hmac.compare_digest(secret.encode(), key.encode()):
        return OPERATOR
    found = await plat.accounts.authenticate(secret)
    if found is None and await plat.keys.authenticate(secret) is None:
        raise Unauthenticated(UNKNOWN)
    if found is None or not found["account"]["platform_admin"]:
        raise Forbidden("the platform's operator does this")
    return f"account:{found['account']['id']}"


async def box(request: Request) -> dict[str, Any]:
    """The live box the path names, proven by its token; its lease renewed."""
    return await platform(request).boxes.authenticate(request.path_params["box_id"], bearer(request))


def door(request: Request) -> None:
    key = platform(request).settings.door_key
    if not hmac.compare_digest(bearer(request).encode(), key.encode()):
        raise Unauthenticated("the door key is required here")


async def read_json(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        raise Invalid("the request body is JSON") from None
    if not isinstance(body, dict):
        raise Invalid("the request body is a JSON object")
    return body


def only(body: dict[str, Any], *fields: str) -> None:
    unknown = sorted(set(body) - set(fields))
    if unknown:
        raise Invalid(f"the fields are {', '.join(fields)}, not {', '.join(unknown)}", param=unknown[0])


def integer(body: dict[str, Any], key: str, default: int) -> int:
    value = body.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise Invalid(f"{key} is an integer", param=key)
    return value


def strings(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Invalid(f"{key} is a list of strings", param=key)
    return value


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
    """None is unbounded: what a box uploads (blobs, archives, logs) is the harness's own and never limited."""
    if path.startswith("/internal/boxes/"):
        if method == "PUT" and ("/blobs/" in path or path.endswith(UNBOUNDED_SUFFIXES)):
            return None
        if path.endswith("/events"):
            return BOX_EVENTS_BODY_MAX
        if path.endswith("/manifest"):
            return MANIFEST_MAX
        return BOX_BODY_MAX
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
