"""What every route shares: who is calling and whether they may, whose a thing is, reading a body no further than a
limit, pages of a list, and how a refusal reaches the caller (the OpenAI error envelope)."""

import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from harnessgateway.domain.events import storable
from harnessgateway.domain.permission import ANYONE, allows
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Forbidden, Invalid, Refused, TooLarge, Unauthorized
from harnessgateway.platform import Platform

log = logging.getLogger(__name__)

BODY_MAX = 32 * 1024 * 1024
SUBJECT = "hg-subject"                         # the header an answer names its caller in
READ_ALL, CHANGE_ALL = "sessions.read_all", "sessions.write_all"      # reaching sessions that are someone else's
KEYS_READ_ALL, KEYS_CHANGE_ALL = "keys.read_all", "keys.write_all"    # and keys that are,
FILES_READ_ALL, FILES_CHANGE_ALL = "files.read_all", "files.write_all"      # and uploaded files
KEY_FIELDS = ("name", "expires_at", "scopes")                         # what a key is made from
Handler = Callable[[Request, Principal], Awaitable[Any]]
_calling: ContextVar[Principal | None] = ContextVar("calling", default=None)


def calling() -> Principal | None:
    """Who the request being answered came from, with the key they came with; None where no authenticated request
    is being answered (the platform's own work, a route with no credential). It is how an extension that records a
    change knows which key made it, without every change being handed one."""
    return _calling.get()


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
    actions: set[str] = field(default_factory=lambda: {
        ANYONE, READ_ALL, CHANGE_ALL, KEYS_READ_ALL, KEYS_CHANGE_ALL, FILES_READ_ALL, FILES_CHANGE_ALL})

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
        self.actions.add(action)                   # every action there is: what a caller is told they may do

        async def handle(request: Request) -> Any:
            who = await self.who(request)
            held = _calling.set(who)                   # for as long as the request is answered, and no longer
            try:
                await permit(self.platform, who, action)
                answer = await handler(request, who)
            finally:
                _calling.reset(held)
            response = answer if isinstance(answer, Response) else JSONResponse(answer)
            response.headers[SUBJECT] = who.subject        # so a client knows which owner is itself
            return response
        return Route(path, answered(handle), methods=[method])


async def permit(platform: Platform, who: Principal, action: str) -> None:
    """Refuse unless the caller may do `action`: the key they came with is not limited away from it, and no
    extension refuses. It is the one place that is decided. Asking what one may do is never limited."""
    if action != ANYONE and who.scopes and not allows(who.scopes, action):
        raise Forbidden(f"this API key is limited to {', '.join(who.scopes)}, which does not allow {action}")
    await platform.extensions.authorize(who, action)


async def may(platform: Platform, who: Principal, action: str) -> bool:
    """Whether the caller may do `action`."""
    try:
        await permit(platform, who, action)
    except Forbidden:
        return False
    return True


async def make_key(platform: Platform, who: Principal, sent: dict[str, Any], *, subject: str) -> dict[str, Any]:
    """Make a key that acts as `subject` from what a caller sent, and answer it with the key itself: the only time
    that is shown. A caller whose own key is limited makes none: a key it made could reach further, or last
    longer, than itself."""
    if unknown := sorted(set(sent) - set(KEY_FIELDS)):
        raise Invalid(f"a key is made from {', '.join(KEY_FIELDS)}", param=unknown[0])
    if who.key is not None and (await platform.keys.get(who.org_id, who.key)).limited:
        raise Forbidden("a key that is limited makes no key: make one with a key that has no scopes and no end, or "
                        "signed in")
    key, text = await platform.keys.create(who.org_id, sent.get("name"), by=who.subject, subject=subject,
                                           expires_at=sent.get("expires_at"), scopes=sent.get("scopes"))
    return {**key.shown(), "key": text}


async def theirs(platform: Platform, who: Principal, owner: str | None, everyones: str) -> None:
    """What someone made is theirs: a session they started, a key that acts as them, a file they uploaded. Its owner
    reaches it. Anyone else who does is reaching everyone's, and is asked about as that (`everyones`: an action such
    as `sessions.read_all`). The core alone allows it, and has nobody else."""
    if owner != who.subject:
        await permit(platform, who, everyones)


async def reaches(platform: Platform, who: Principal, session_id: str, *, to_change: bool = False) -> None:
    """Refuse unless the caller may reach this session: it is theirs, or everyone's are theirs to reach."""
    await theirs(platform, who, await platform.sessions.owner(who.org_id, session_id),
                 CHANGE_ALL if to_change else READ_ALL)


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
