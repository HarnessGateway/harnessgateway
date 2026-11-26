"""Providers, environments and harnesses: one shape with one set of verbs. PUT makes or replaces."""

from functools import partial
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from harnessgateway import defaults
from harnessgateway.api.http import Api, page_of, parse_object, reaches, read_body
from harnessgateway.domain.package import PACKED_MAX, UNPACKED_MAX, PackageFile, is_stray, write_archive
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.resources import KINDS
from harnessgateway.errors import Invalid, TooLarge
from harnessgateway.ids import is_id

_GZIP = b"\x1f\x8b"                            # how a package archive begins, and a JSON document never does


def routes(api: Api) -> list[Route]:
    platform = api.platform
    resources, sessions, snapshots = platform.resources, platform.sessions, platform.snapshots
    ships = frozenset(defaults.names())

    def not_the_platforms(kind: str, name: str) -> None:
        """A package that ships is the platform's: it follows the platform, and is not changed in place."""
        if kind == "harnesses" and name in ships:
            raise Invalid(f"{name} ships with the platform and follows it: to change it, export it and add it under "
                          "another name", param="name")

    async def listed(kind: str, request: Request, who: Principal) -> Any:
        found = await resources.all(who.org_id, kind)
        return page_of([resource.shown() for resource in found], request, key="name")

    async def one(kind: str, request: Request, who: Principal) -> Any:
        return (await resources.get(who.org_id, kind, request.path_params["name"])).shown()

    async def package_from(who: Principal, session_id: object, path: object) -> bytes:
        """A package taken from a folder of a session's work, as it was last saved: how a package an agent wrote
        becomes a harness."""
        if not isinstance(path, str) or not path.strip("/"):
            raise Invalid("path is the package's folder in the session's work", param="path")
        if not is_id(session_id, "session"):
            raise Invalid(f"session names no session: {session_id!r}", param="session")
        org_id = who.org_id
        await reaches(platform, who, str(session_id))
        saved = await sessions.saved(org_id, (await sessions.get(org_id, str(session_id))).id)
        found = (await snapshots.manifest(org_id, saved)).files(f"work/{path.strip('/')}/")
        entries = {name: entry for name, entry in found.items() if not is_stray(name)}
        if not entries:
            raise Invalid(f"the session's saved work has no folder {path.strip('/')!r}", param="path")
        if sum(entry.size for entry in entries.values()) > UNPACKED_MAX:
            raise TooLarge("the folder is too large to be a package")
        return write_archive([
            PackageFile(name, b"".join([piece async for piece in snapshots.content(org_id, entry)]),
                        bool(entry.mode & 0o100)) for name, entry in entries.items()])

    async def apply(kind: str, request: Request, who: Principal) -> Any:
        """Make or replace a definition. A harness is made from its package, and is nothing else: an archive
        sent as the body, or a folder of a session's work (`{"session", "path"}`)."""
        name = request.path_params["name"]
        not_the_platforms(kind, name)
        body = await read_body(request, PACKED_MAX)
        if kind == "harnesses" and body[:2] == _GZIP:
            made = await resources.add_package(who.org_id, name, body, by=who.subject)
        else:
            sent = parse_object(body)
            if kind == "harnesses" and "session" in sent:
                archive = await package_from(who, sent["session"], sent.get("path"))
                made = await resources.add_package(who.org_id, name, archive, by=who.subject)
            else:
                made = await resources.apply(who.org_id, kind, name, sent, by=who.subject)
        return made.shown()

    async def package(request: Request, who: Principal) -> Any:
        """A harness's package, as the archive PUT takes: so a harness can be kept, moved and changed."""
        found = await resources.get(who.org_id, "harnesses", request.path_params["name"])
        filename = f"{found.name}.tar.gz"
        return Response(await resources.package(who.org_id, found.spec["digest"]), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    async def delete(kind: str, request: Request, who: Principal) -> Any:
        name = request.path_params["name"]
        not_the_platforms(kind, name)
        await resources.delete(who.org_id, kind, name, by=who.subject)
        return {"name": name, "deleted": True}

    return [
        api.route("/v1/harnesses/{name}/package", "GET", "harnesses.read", package),
        *(route for kind in KINDS for route in (              # the same four verbs for each of the three kinds
            api.route(f"/v1/{kind}", "GET", f"{kind}.read", partial(listed, kind)),
            api.route(f"/v1/{kind}/{{name}}", "GET", f"{kind}.read", partial(one, kind)),
            api.route(f"/v1/{kind}/{{name}}", "PUT", f"{kind}.write", partial(apply, kind)),
            api.route(f"/v1/{kind}/{{name}}", "DELETE", f"{kind}.write", partial(delete, kind)))),
    ]
