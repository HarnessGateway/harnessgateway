"""The console: the pages people use the platform through, served beside the API at /console.

It is a client of the public API like any other. It has no endpoint of its own, and it can do only what the API
key someone signs in with can do. An installation that sets HG_CONSOLE=off is the API alone.
"""

from collections.abc import Sequence
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

FILES = Path(__file__).resolve().parent.parent / "console"
# What may run in the console's pages: its own scripts and styles, talking to its own origin. Nothing an agent
# wrote is ever markup there, and this is the second wall behind that.
_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                               "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",               # a page is asked for again after an update, never served stale
}


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
                return FileResponse(file, headers=_HEADERS)
        return PlainTextResponse("Not Found", 404)

    async def home(_: Request) -> Response:
        return RedirectResponse("/console/")       # with the slash: the pages name their files beside themselves

    return [Route("/", home), Route("/console", home), Route("/console/{path:path}", page)]
