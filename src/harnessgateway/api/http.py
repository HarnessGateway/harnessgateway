"""What every route shares: who is calling and whether they may, reading a body no further than a limit, pages of
a list, and how a refusal reaches the caller (the OpenAI error envelope)."""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from harnessgateway.domain.events import storable
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Forbidden, Invalid, Refused, TooLarge, Unauthorized
from harnessgateway.platform import Platform

log = logging.getLogger(__name__)

BODY_MAX = 32 * 1024 * 1024
SUBJECT = "hg-subject"                         # the header an answer names its caller in
READ_ALL, CHANGE_ALL = "sessions.read_all", "sessions.write_all"      # reaching sessions that are someone else's
Handler = Callable[[Request, Principal], Awaitable[Any]]


def refused(refusal: Refused) -> JSONResponse:
    return JSONResponse({"error": {"message": refusal.message, "type": refusal.type, "param": refusal.param,
                                   "code": refusal.code}}, refusal.status)


def answered(handle: Callable[[Request], Awaitable[Any]]) -> Callable[[Request], Awaitable[Response]]:
    """An endpoint that answers with what `handle` returns (as JSON, unless it is a response already), tells a
    refusal plainly, and says no more than that it failed when the fault is the platform's own."""
    async def endpoint(request: Request) -> Response:
        try:
            answer = await handle(request)
            return answer if isinstance(answer, Response) else JSONResponse(answer)
        except Refused as refusal:
            return refused(refusal)
        except Exception:
            log.exception("%s %s failed", request.method, request.url.path)
            failure = Refused("the platform failed to carry out the request")
            failure.status, failure.type = 500, "server_error"
            return refused(failure)
    return endpoint


@dataclass(frozen=True)
class Api:
    """What the public API's routes are made with. An extension's routes are made with it too, so they are
    authenticated and authorized exactly as the core's own are."""

    platform: Platform

    async def who(self, request: Request) -> Principal:
        """Who is calling: an extension's answer first, then whoever an API key belongs to."""
        who = await self.platform.extensions.authenticate(request)
        if who is None:
            scheme, _, key = request.headers.get("authorization", "").partition(" ")
            who = await self.platform.keys.authenticate(key.strip()) if scheme.lower() == "bearer" and key else None
        if who is None:
            raise Unauthorized("a valid API key is required: Authorization: Bearer hg_...", code="invalid_api_key")
        return who

    def route(self, path: str, method: str, action: str, handler: Handler) -> Route:
        """A route for an authenticated caller who may do `action` (`responses.write`, `providers.read`, …)."""
        async def handle(request: Request) -> Any:
            who = await self.who(request)
            await self.platform.extensions.authorize(who, action)
            answer = await handler(request, who)
            response = answer if isinstance(answer, Response) else JSONResponse(answer)
            response.headers[SUBJECT] = who.subject        # so a client knows which owner is itself
            return response
        return Route(path, answered(handle), methods=[method])


async def reaches(platform: Platform, who: Principal, session_id: str, *, to_change: bool = False) -> None:
    """A session is its owner's: whoever started it reaches it. Anyone else who does is reaching everyone's, and
    the extensions are asked whether they may. The core alone allows it, as it allows everything."""
    if await platform.sessions.owner(who.org_id, session_id) != who.subject:
        await platform.extensions.authorize(who, CHANGE_ALL if to_change else READ_ALL)


async def reads_all(platform: Platform, who: Principal) -> bool:
    """Whether a caller may read every session of their organization, and not their own alone."""
    try:
        await platform.extensions.authorize(who, READ_ALL)
    except Forbidden:
        return False
    return True


async def read_body(request: Request, limit: int = BODY_MAX) -> bytes:
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > limit:
            raise TooLarge(f"a request body is at most {limit >> 20} MiB")
    return bytes(body)


def parse_object(body: bytes) -> dict[str, Any]:
    """A body as a JSON object, with no NUL character in it: the database holds none."""
    try:
        parsed = json.loads(body)
    except ValueError:
        raise Invalid("the request body is not JSON") from None
    if not isinstance(parsed, dict):
        raise Invalid("the request body is a JSON object")
    if storable(parsed) != parsed:
        raise Invalid("the request body holds a NUL character (\\u0000), which cannot be kept: leave it out")
    return parsed


def limit_of(request: Request) -> int:
    text = request.query_params.get("limit", "20")
    if not text.isdigit() or not 1 <= int(text) <= 100:
        raise Invalid("limit is 1 to 100", param="limit")
    return int(text)


def listing(shown: list[dict[str, Any]], *, has_more: bool = False, key: str = "id") -> dict[str, Any]:
    """A list in the shape the OpenAI SDK pages through."""
    return {"object": "list", "data": shown, "first_id": shown[0][key] if shown else None,
            "last_id": shown[-1][key] if shown else None, "has_more": has_more}


def page_of(items: list[dict[str, Any]], request: Request, key: str = "id") -> dict[str, Any]:
    """One page of a list that is held whole: `limit` items after the one `after` names."""
    limit, after, start = limit_of(request), request.query_params.get("after"), 0
    if after is not None:
        keys = [item[key] for item in items]
        if after not in keys:
            raise Invalid(f"after names nothing in this list: {after}", param="after")
        start = keys.index(after) + 1
    return listing(items[start:start + limit], has_more=start + limit < len(items), key=key)
