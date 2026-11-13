"""The public API: OpenAI Responses, and the parts of the OpenAI API that go with it (spec §13)."""

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from harnessgateway.api import errors
from harnessgateway.api.auth import Authenticator
from harnessgateway.domain.names import check_id
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.request import validate
from harnessgateway.errors import Invalid, NotFound, TooLarge
from harnessgateway.extensions import Extensions
from harnessgateway.intake import Intake
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores

BODY_MAX = 32 * 1024 * 1024
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def read_json(request: Request) -> Any:
    """The request's body as JSON, read no further than the limit."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > BODY_MAX:
            raise TooLarge(f"a request body is at most {BODY_MAX >> 20} MiB")
    try:
        return json.loads(body)
    except ValueError:
        raise Invalid("the request body is not JSON") from None


def _turn_id(value: str) -> str:
    try:
        return check_id(value, "turn")
    except Invalid:
        raise NotFound(f"there is no response {value}") from None


def _input_items(turn: Turn) -> list[dict[str, Any]]:
    """The turn's input as a list of items, each with an id (spec §13.1)."""
    given = (turn.request or {}).get("input")
    items = given if isinstance(given, list) else [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": given}]}]
    listed = []
    for number, item in enumerate(items):
        item = dict(item) if isinstance(item, dict) else {"type": "message", "role": "user", "content": str(item)}
        item.setdefault("type", "message")
        item.setdefault("id", f"msg_{turn.id.partition('_')[2]}_{number}")
        listed.append(item)
    return listed


def responses_router(stores: Stores, extensions: Extensions, caller: Any) -> APIRouter:
    router = APIRouter()
    intake, turns = Intake(stores), stores.turns
    Caller = Annotated[Principal, Depends(caller)]

    async def sse(org_id: str, turn_id: str, after: int, forget: bool) -> AsyncIterator[str]:
        try:
            async for event in turns.follow(org_id, turn_id, after=after):
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        finally:
            if forget:
                await turns.forget(org_id, turn_id)

    def stream(org_id: str, turn_id: str, *, after: int = -1, forget: bool = False) -> StreamingResponse:
        return StreamingResponse(sse(org_id, turn_id, after, forget), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)

    @router.post("/v1/responses")
    async def create(request: Request, principal: Caller) -> Any:
        body = validate(await read_json(request))
        forget = body.get("store") is False
        if forget and body.get("background"):
            raise Invalid("a background response is retrieved later, so it must be stored: store cannot be false",
                          param="store")
        await extensions.before_turn(principal, body)
        turn = await intake.accept(principal.org_id, body, key_id=principal.key_id)
        if body.get("stream"):
            return stream(principal.org_id, turn.id, forget=forget)
        if body.get("background"):
            return turn.response()
        try:
            async for _ in turns.follow(principal.org_id, turn.id):
                pass
            return (await turns.get(principal.org_id, turn.id)).response()
        finally:
            if forget:
                await turns.forget(principal.org_id, turn.id)

    @router.get("/v1/responses/{turn_id}")
    async def retrieve(turn_id: str, principal: Caller, following: Annotated[bool, Query(alias="stream")] = False,
                       starting_after: int = -1) -> Any:
        turn = await turns.get(principal.org_id, _turn_id(turn_id))
        if following:
            return stream(principal.org_id, turn.id, after=starting_after)
        return turn.response()

    @router.post("/v1/responses/{turn_id}/cancel")
    async def cancel(turn_id: str, principal: Caller) -> Any:
        return (await turns.cancel(principal.org_id, _turn_id(turn_id))).response()

    @router.delete("/v1/responses/{turn_id}")
    async def delete(turn_id: str, principal: Caller) -> Any:
        await turns.delete(principal.org_id, _turn_id(turn_id))
        return {"id": turn_id, "object": "response", "deleted": True}

    @router.get("/v1/responses/{turn_id}/input_items")
    async def input_items(turn_id: str, principal: Caller) -> Any:
        items = _input_items(await turns.get(principal.org_id, _turn_id(turn_id)))
        return {"object": "list", "data": items, "first_id": items[0]["id"] if items else None,
                "last_id": items[-1]["id"] if items else None, "has_more": False}

    def model(name: str, created: int) -> dict[str, Any]:
        return {"id": name, "object": "model", "created": created, "owned_by": "harnessgateway"}

    @router.get("/v1/models")
    async def models(principal: Caller) -> Any:
        harnesses = await stores.harnesses.all(principal.org_id)
        return {"object": "list", "data": [
            model(h.name, int(h.added_at.timestamp()) if h.added_at else 0) for h in harnesses]}

    @router.get("/v1/models/{name}")
    async def one_model(name: str, principal: Caller) -> Any:
        try:
            harness = await stores.harnesses.get(principal.org_id, name)
        except NotFound:
            raise NotFound(f"there is no model named {name!r}: a model is an installed harness") from None
        return model(harness.name, int(harness.added_at.timestamp()) if harness.added_at else 0)

    return router


def public_app(stores: Stores, extensions: Extensions) -> FastAPI:
    app = FastAPI(title="HarnessGateway", docs_url=None, redoc_url=None, openapi_url=None)
    errors.install(app)
    caller = Authenticator(stores, extensions)
    app.include_router(responses_router(stores, extensions, caller))
    for router in extensions.routers(stores):
        app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app
