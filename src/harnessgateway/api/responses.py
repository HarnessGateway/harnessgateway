"""The OpenAI Responses API, as it is: create (blocking, streamed or in the background), retrieve, cancel, delete
and input items. Nothing is added to it."""

import json
from collections.abc import AsyncIterator
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from harnessgateway.api.http import FILES_READ_ALL, Api, listing, parse_object, reaches, read_body, theirs
from harnessgateway.domain.line import Line
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.request import file_ids, input_items, validate, without_credentials
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.ids import is_id, new_placeholder
from harnessgateway.platform import Platform
from harnessgateway.store.turns import Turn

_MIB = 1024 * 1024
_STREAMED = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}      # a stream is useless once buffered


async def accept(platform: Platform, who: Principal, body: dict[str, Any]) -> Turn:
    """Queue the turn a valid request asks for; or refuse, naming what is wrong. A request continues from a turn
    that has ended and left a saved state, whose files fit the environment it now names, in a session the caller
    may change; and the uploaded files it names are ones the caller may read."""
    org_id = who.org_id
    resolved = await platform.resources.resolve(org_id, Line.parse(body.get("model")))
    parent, previous = None, body.get("previous_response_id")
    if previous is not None:
        try:
            parent = await platform.turns.get(org_id, previous)
        except NotFound:
            raise Invalid(f"previous_response_id names no response: {previous}",
                          param="previous_response_id") from None
        await reaches(platform, who, parent.session_id, to_change=True)
        if not parent.final:
            raise Conflict(f"the session has a turn in progress: {parent.id}")
        if parent.snapshot_id is None:
            raise Conflict(f"response {parent.id} ended as {parent.status} ({parent.reason}) with no saved state: "
                           "continue from the response before it")
        held, allowed = (await platform.snapshots.get(org_id, parent.snapshot_id)).bytes, resolved.environment.disk_mb
        if held > allowed * _MIB:
            raise Invalid(f"the session's files are {held // _MIB} MiB, and environment "
                          f"{resolved.line.environment!r} allows {allowed} MiB", param="model")
    for file_id in file_ids(body):
        try:
            file = await platform.files.get(org_id, file_id)
        except NotFound:
            raise Invalid(f"input names a file that does not exist: {file_id}", param="input") from None
        await theirs(platform, who, file.owner, FILES_READ_ALL)
    body, credentials = without_credentials(body, new_placeholder)
    await platform.extensions.before_turn(who, body)
    return await platform.turns.create(who, resolved=resolved, request=body, parent=parent, credentials=credentials)


def routes(api: Api) -> list[Route]:
    platform = api.platform
    turns = platform.turns

    def turn_id(request: Request) -> str:
        if not is_id(request.path_params["id"], "response"):
            raise NotFound(f"there is no response {request.path_params['id']}")
        return str(request.path_params["id"])

    def stream(org_id: str, turn: str, *, after: int = -1, forget: bool = False) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                async for event in turns.follow(org_id, turn, after=after):
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
            finally:
                if forget:
                    await platform.forget(org_id, turn)
        return StreamingResponse(events(), media_type="text/event-stream", headers=_STREAMED)

    async def create(request: Request, who: Principal) -> Any:
        body = validate(parse_object(await read_body(request)))
        forget = body.get("store") is False
        turn = await accept(platform, who, body)
        if body.get("stream"):
            return stream(who.org_id, turn.id, forget=forget)
        if body.get("background"):
            return turn.response()
        try:
            async for _ in turns.follow(who.org_id, turn.id):       # to its end: the answer is the final response
                pass
            return (await turns.get(who.org_id, turn.id)).response()
        finally:
            if forget:
                await platform.forget(who.org_id, turn.id)

    async def reached(request: Request, who: Principal, *, to_change: bool = False) -> Turn:
        """The response a request names, once its session is shown to be the caller's to reach."""
        turn = await turns.get(who.org_id, turn_id(request))
        await reaches(platform, who, turn.session_id, to_change=to_change)
        return turn

    async def retrieve(request: Request, who: Principal) -> Any:
        turn = await reached(request, who)
        if request.query_params.get("stream") == "true":
            after = request.query_params.get("starting_after", "-1")
            return stream(who.org_id, turn.id, after=int(after) if after.lstrip("-").isdigit() else -1)
        return turn.response()

    async def cancel(request: Request, who: Principal) -> Any:
        turn = await reached(request, who, to_change=True)
        return (await turns.cancel(who.org_id, turn.id, by=who.subject)).response()

    async def delete(request: Request, who: Principal) -> Any:
        turn = await reached(request, who, to_change=True)
        await turns.delete(who.org_id, turn.id, by=who.subject)
        return {"id": request.path_params["id"], "object": "response", "deleted": True}

    async def inputs(request: Request, who: Principal) -> Any:
        turn = await reached(request, who)
        return listing(input_items(turn.id, turn.request or {}))

    return [
        api.route("/v1/responses", "POST", "responses.write", create),
        api.route("/v1/responses/{id}", "GET", "responses.read", retrieve),
        api.route("/v1/responses/{id}", "DELETE", "responses.write", delete),
        api.route("/v1/responses/{id}/cancel", "POST", "responses.write", cancel),
        api.route("/v1/responses/{id}/input_items", "GET", "responses.read", inputs),
    ]
