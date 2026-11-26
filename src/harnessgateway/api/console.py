"""The console: the pages people use the platform through, served beside the API at /console.

It is a client of the public API like any other. It has no endpoint of its own, and it can do only what the
credential someone signs in with can do. An installation that sets HG_CONSOLE=off is the API alone.
"""

from collections.abc import Sequence
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from harnessgateway import shipped
from harnessgateway.domain import view

FILES = shipped.folder("console")
# The libraries that highlight code and typeset mathematics in a reply: each pinned to a version by its address
# here, and to its content by an integrity hash where the console loads it. Without them everything still reads.
_LIBRARIES = "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.12.0/ https://cdn.jsdelivr.net/npm/katex@0.19.1/"
# What may run in the console's pages: its own scripts and those libraries, talking to its own origin. Nothing an
# agent wrote is ever markup there, and this is the second wall behind that.
_PAGE = (f"default-src 'self'; script-src 'self' {_LIBRARIES}; style-src 'self' 'unsafe-inline' {_LIBRARIES}; "
         f"font-src {_LIBRARIES}; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
         "frame-ancestors 'none'")
SANDBOXED = "components/viewer/"               # what renders a document an agent made runs apart, like the document
_HEADERS = {"X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache"}       # a page is asked for again after an update, never served stale


def _file(folder: Path, path: str, policy: str) -> Response:
    """One file of a folder, and nothing outside it."""
    file = (folder / path).resolve()
    if file.is_relative_to(folder) and file.is_file():
        return FileResponse(file, headers={"Content-Security-Policy": policy, **_HEADERS})
    return PlainTextResponse("Not Found", 404)


def routes(added: Sequence[tuple[str, Path]] = ()) -> list[BaseRoute]:
    """The console's routes. `added` are the extensions' folders of console files, by the extension's name, in
    the order the extensions were named. Each is served under /console/x/<name>/, so no file of an extension's is
    ever served in place of one of the console's, or of another extension's."""
    extensions = {name: folder.resolve() for name, folder in added}

    async def page(request: Request) -> Response:
        """One of the console's files. They are public: a page says nothing until someone signs in."""
        path = request.path_params["path"] or "index.html"
        return _file(FILES, path, view.POLICY if path.startswith(SANDBOXED) else _PAGE)

    async def theirs(request: Request) -> Response:
        """One of an extension's files."""
        folder = extensions.get(request.path_params["name"])
        return _file(folder, request.path_params["path"], _PAGE) if folder else PlainTextResponse("Not Found", 404)

    async def named(_: Request) -> Response:
        """Which extensions add to the console: what the console reads to find each one's `edition.js`."""
        return JSONResponse(list(extensions), headers={"Content-Security-Policy": _PAGE, **_HEADERS})

    async def home(_: Request) -> Response:
        return RedirectResponse("/console/")       # with the slash: the pages name their files beside themselves

    return [Route("/", home), Route("/console", home), Route("/console/extensions.json", named),
            Route("/console/x/{name}/{path:path}", theirs), Route("/console/{path:path}", page)]
