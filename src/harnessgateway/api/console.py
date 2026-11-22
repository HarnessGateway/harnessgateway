"""The console: the pages people use the platform through, served beside the API at /console.

It is a client of the public API like any other. It has no endpoint of its own, and it can do only what the API
key someone signs in with can do. An installation that sets HG_CONSOLE=off is the API alone.
"""

from collections.abc import Sequence
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
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


def _headers(path: str) -> dict[str, str]:
    return {"Content-Security-Policy": view.POLICY if path.startswith(SANDBOXED) else _PAGE,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache"}       # a page is asked for again after an update, never served stale


def routes(added: Sequence[Path] = ()) -> list[BaseRoute]:
    """The console's routes. `added` are editions' folders of files, in the order the editions were named: a
    file is looked for in the last one first and in the core's own last, so an edition adds files, and may put
    one of its own in place of the core's."""
    folders = [*(folder.resolve() for folder in reversed(added)), FILES]

    async def page(request: Request) -> Response:
        """One of the console's files. They are public: a page says nothing until someone signs in."""
        path = request.path_params["path"] or "index.html"
        for folder in folders:
            file = (folder / path).resolve()
            if file.is_relative_to(folder) and file.is_file():
                return FileResponse(file, headers=_headers(path))
        return PlainTextResponse("Not Found", 404)

    async def home(_: Request) -> Response:
        return RedirectResponse("/console/")       # with the slash: the pages name their files beside themselves

    return [Route("/", home), Route("/console", home), Route("/console/{path:path}", page)]
