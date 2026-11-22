"""Links to a session's work that a browser opens by itself: how a client shows what an agent made (an image, a
video, a page that runs) without holding the files. `domain/view.py` says what a link is."""

import time
from collections.abc import AsyncIterator
from typing import Any

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from harnessgateway.api.containers import Work
from harnessgateway.api.http import Api, answered
from harnessgateway.domain import view
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.snapshot import CHUNK, Entry
from harnessgateway.errors import NotFound
from harnessgateway.ids import is_id, session_of


def routes(api: Api) -> list[Route]:
    platform, work = api.platform, Work(api.platform)
    key = platform.settings.secret_key

    async def link(request: Request, who: Principal) -> Any:
        container = request.path_params["id"]
        if not is_id(container, "container"):
            raise NotFound(f"there is no container {container}")
        try:
            session = await platform.sessions.get(who.org_id, session_of(container))
        except NotFound:
            raise NotFound(f"there is no container {container}") from None
        expires_at = int(time.time()) + view.LASTS_S
        token = view.sign(key, who.org_id, session.id, expires_at=expires_at)
        return {"url": f"/view/{token}/", "expires_at": expires_at}

    async def span(org_id: str, entry: Entry, first: int, last: int) -> AsyncIterator[bytes]:
        """Bytes `first` to `last` of a file, reading only the chunks that hold them."""
        chunks = platform.snapshots.chunks(org_id)
        for index in range(first // CHUNK, last // CHUNK + 1):
            start = index * CHUNK
            yield (await chunks.get(entry.hashes[index]))[max(first - start, 0):last - start + 1]

    async def file(request: Request) -> Any:
        named, path = view.read(key, request.path_params["token"], now=time.time()), request.path_params["path"]
        entry = (await work.files(*named)).get(path) if named else None
        if named is None or entry is None:
            raise NotFound("there is nothing at this address: the link is past its time, or the file is not there")
        kind = view.content_type(path)
        headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer",
                   "Access-Control-Allow-Origin": "*"}     # a sandboxed page has no origin, and reads its own files
        if kind != "application/pdf":              # a browser shows no PDF in a sandboxed document
            headers["Content-Security-Policy"] = view.POLICY
        if "download" in request.query_params:
            headers["Content-Disposition"] = f'attachment; filename="{path.rpartition("/")[2].replace(chr(34), "")}"'
        try:
            asked = view.byte_range(request.headers.get("range"), entry.size)
        except ValueError:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{entry.size}"})
        first, last = asked or (0, entry.size - 1)
        if asked:
            headers["Content-Range"] = f"bytes {first}-{last}/{entry.size}"
        headers["Content-Length"] = str(last - first + 1)
        return StreamingResponse(span(named[0], entry, first, last), status_code=206 if asked else 200,
                                 media_type=kind, headers=headers)

    return [api.route("/v1/containers/{id}/view", "POST", "containers.read", link),
            Route("/view/{token}/{path:path}", answered(file), methods=["GET"])]
