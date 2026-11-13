"""Links to a session's files that a browser can open by itself (spec §13.11).

A page shows an image, plays a video or runs a page an agent built by giving the browser an address. The browser
sends no API key with such a request, so the address itself is the permission: it holds a token that names one
session, for a limited time, and is signed by the platform. Everything served this way is served sandboxed: what
an agent made runs in an origin of its own, and can neither read the page that shows it nor call the API as it.
"""

import base64
import binascii
import hmac
import mimetypes
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from harnessgateway.domain.names import check_id, session_of_container
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.sealing import keyed_hash
from harnessgateway.domain.snapshot import CHUNK, Entry, SnapshotManifest
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.platform import Platform

VIEW_S = 3600                                  # how long a link opens what it names
SANDBOX = ("sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; "
           "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'")
_WORK = "work/"
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_TEXT_TYPES = ("application/json", "application/javascript", "text/javascript", "image/svg+xml")
_MANIFESTS_KEPT = 16

for _extension, _kind in (
        (".md", "text/markdown"), (".markdown", "text/markdown"), (".mmd", "text/plain"), (".ts", "text/plain"),
        (".tsx", "text/plain"), (".jsx", "text/plain"), (".py", "text/plain"), (".sh", "text/plain"),
        (".yaml", "text/plain"), (".yml", "text/plain"), (".toml", "text/plain"), (".go", "text/plain"),
        (".rs", "text/plain"), (".ipynb", "application/json"), (".js", "text/javascript"),
        (".mjs", "text/javascript"), (".webp", "image/webp"), (".avif", "image/avif"), (".m4a", "audio/mp4"),
        (".opus", "audio/ogg"), (".mov", "video/quicktime"), (".csv", "text/csv"),
        (".tsv", "text/tab-separated-values")):
    mimetypes.add_type(_kind, _extension)


def sign_view(secret_key: bytes, org_id: str, session_id: str, *, expires_at: int) -> str:
    """The token of a link to one session's files, good until `expires_at`."""
    said = f"{org_id}\n{session_id}\n{expires_at}"
    return f"{base64.urlsafe_b64encode(said.encode()).decode().rstrip('=')}.{keyed_hash(secret_key, said)}"


def read_view(secret_key: bytes, token: str, *, now: float) -> tuple[str, str] | None:
    """The organization and session a token names; None when it was not made here, was changed, or is past its time."""
    encoded, _, signature = token.partition(".")
    try:
        said = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        org_id, session_id, expires_at = said.split("\n")
        if not hmac.compare_digest(keyed_hash(secret_key, said), signature) or int(expires_at) < now:
            return None
        return org_id, check_id(session_id, "session")
    except (binascii.Error, ValueError, Invalid):
        return None


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The one span a Range header asks for, first and last byte inclusive; None for the whole file (no header,
    or one not served: several spans, another unit). ValueError when the span lies outside the file."""
    match = _RANGE.fullmatch((header or "").strip())
    if not match or not (match[1] or match[2]) or size == 0:
        return None
    if match[1]:
        first, last = int(match[1]), min(int(match[2]), size - 1) if match[2] else size - 1
    else:                                                  # the last n bytes
        if int(match[2]) == 0:
            raise ValueError("an empty suffix")
        first, last = max(size - int(match[2]), 0), size - 1
    if first >= size or first > last:
        raise ValueError(f"bytes {first}-{last} of {size}")
    return first, last


def content_type(path: str) -> str:
    kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return f"{kind}; charset=utf-8" if kind.startswith("text/") or kind in _TEXT_TYPES else kind


def views_router(platform: Platform, caller: Any) -> APIRouter:
    router = APIRouter()
    stores = platform.stores
    key = stores.settings.secret_key
    manifests: OrderedDict[str, SnapshotManifest] = OrderedDict()     # by snapshot: a snapshot never changes
    Caller = Annotated[Principal, Depends(caller)]

    async def work_of(org_id: str, session_id: str) -> dict[str, Entry]:
        """The session's work as its latest turn left it, by path."""
        try:
            session = await stores.turns.session(org_id, session_id)
            head = await stores.turns.get(org_id, session.head_turn_id) if session.head_turn_id else None
        except NotFound:
            return {}
        if head is None or head.snapshot_id is None:
            return {}
        if head.snapshot_id not in manifests:
            manifests[head.snapshot_id] = await stores.snapshots.manifest(org_id, head.snapshot_id)
            while len(manifests) > _MANIFESTS_KEPT:
                manifests.popitem(last=False)
        manifests.move_to_end(head.snapshot_id)
        return manifests[head.snapshot_id].entries

    async def span(org_id: str, entry: Entry, first: int, last: int) -> AsyncIterator[bytes]:
        """Bytes `first` to `last` of a file, reading only the chunks that hold them."""
        for index in range(first // CHUNK, last // CHUNK + 1):
            start = index * CHUNK
            data = await stores.snapshots.chunk(org_id, entry.hashes[index])
            yield data[max(first - start, 0):last - start + 1]

    @router.post("/v1/containers/{container}/view")
    async def link(container: str, principal: Caller) -> Any:
        try:
            session = await stores.turns.session(principal.org_id, session_of_container(container))
        except (Invalid, NotFound):
            raise NotFound(f"there is no container {container}") from None
        expires_at = int(time.time()) + VIEW_S
        token = sign_view(key, principal.org_id, session.id, expires_at=expires_at)
        return {"url": f"/view/{token}/", "expires_at": expires_at}

    @router.get("/view/{token}/{path:path}")
    async def view(token: str, path: str, request: Request, download: str | None = None) -> Response:
        named = read_view(key, token, now=time.time())
        entry = (await work_of(*named)).get(_WORK + path) if named else None
        if named is None or entry is None or entry.kind != "f":
            raise NotFound("there is nothing at this address: the link is past its time, or the file is not there")
        kind = content_type(path)
        headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Access-Control-Allow-Origin": "*"}
        if not kind.startswith("application/pdf"):         # a browser shows no PDF in a sandboxed document
            headers["Content-Security-Policy"] = SANDBOX
        if download is not None:
            headers["Content-Disposition"] = f'attachment; filename="{path.rsplit("/", 1)[-1].replace(chr(34), "")}"'
        try:
            asked = byte_range(request.headers.get("range"), entry.size)
        except ValueError:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{entry.size}"})
        first, last = asked or (0, entry.size - 1)
        if asked:
            headers["Content-Range"] = f"bytes {first}-{last}/{entry.size}"
        headers["Content-Length"] = str(last - first + 1)
        body = span(named[0], entry, first, last) if entry.size else iter(())
        return StreamingResponse(body, status_code=206 if asked else 200, media_type=kind, headers=headers)

    return router
