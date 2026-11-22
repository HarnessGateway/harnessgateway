"""Providers, environments and harnesses: one shape with one set of verbs. PUT makes or replaces."""

from functools import partial
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from harnessgateway.api.http import Api, page_of, parse_object, read_body
from harnessgateway.domain.package import PACKED_MAX, UNPACKED_MAX, PackageFile, write_archive
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.resources import KINDS
from harnessgateway.errors import Invalid, TooLarge
from harnessgateway.ids import is_id

_GZIP = b"\x1f\x8b"                            # how a package archive begins, and a JSON document never does
_LEFT_OUT = frozenset({".git", "__pycache__", ".DS_Store"})     # what a working folder holds that a package does not


def routes(api: Api) -> list[Route]:
    platform = api.platform
    resources, sessions, snapshots = platform.resources, platform.sessions, platform.snapshots

    async def listed(kind: str, request: Request, who: Principal) -> Any:
        found = await resources.all(who.org_id, kind)
        return page_of([resource.shown() for resource in found], request, key="name")

    async def one(kind: str, request: Request, who: Principal) -> Any:
        return (await resources.get(who.org_id, kind, request.path_params["name"])).shown()

    async def package_from(org_id: str, session_id: object, path: object) -> bytes:
        """A package taken from a folder of a session's work, as it was last saved: how a package an agent wrote
        becomes a harness."""
        if not isinstance(path, str) or not path.strip("/"):
            raise Invalid("path is the package's folder in the session's work", param="path")
        if not is_id(session_id, "session"):
            raise Invalid(f"session names no session: {session_id!r}", param="session")
        saved = await sessions.saved(org_id, (await sessions.get(org_id, str(session_id))).id)
        found = (await snapshots.manifest(org_id, saved)).files(f"work/{path.strip('/')}/")
        entries = {name: entry for name, entry in found.items() if not _LEFT_OUT & set(name.split("/"))}
        if not entries:
            raise Invalid(f"the session's saved work has no folder {path.strip('/')!r}", param="path")
        if sum(entry.size for entry in entries.values()) > UNPACKED_MAX:
            raise TooLarge("the folder is too large to be a package")
        return write_archive([
            PackageFile(name, b"".join([piece async for piece in snapshots.content(org_id, entry)]),
                        bool(entry.mode & 0o100)) for name, entry in entries.items()])

    async def apply(kind: str, request: Request, who: Principal) -> Any:
        """Make or replace a definition. A harness is made from its package: an archive sent as the body, or a
        folder of a session's work (`{"session", "path"}`). After that, its settings and secrets are set here."""
        name = request.path_params["name"]
        body = await read_body(request, PACKED_MAX)
        if kind == "harnesses" and body[:2] == _GZIP:
            made = await resources.add_package(who.org_id, name, body)
        else:
            sent = parse_object(body)
            if kind == "harnesses" and "session" in sent:
                archive = await package_from(who.org_id, sent["session"], sent.get("path"))
                made = await resources.add_package(who.org_id, name, archive)
            else:
                made = await resources.apply(who.org_id, kind, name, sent)
        await platform.announce(f"{kind}.applied", who.org_id, by=who, target=name)
        return made.shown()

    async def package(request: Request, who: Principal) -> Any:
        """A harness's package, as the archive PUT takes: so a harness can be kept, moved and changed."""
        found = await resources.get(who.org_id, "harnesses", request.path_params["name"])
        filename = f"{found.name}-{found.spec['manifest']['version']}.tar.gz"
        return Response(await resources.package(who.org_id, found.spec["digest"]), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    async def delete(kind: str, request: Request, who: Principal) -> Any:
        name = request.path_params["name"]
        await resources.delete(who.org_id, kind, name)
        await platform.announce(f"{kind}.deleted", who.org_id, by=who, target=name)
        return {"name": name, "deleted": True}

    return [
        api.route("/v1/harnesses/{name}/package", "GET", "harnesses.read", package),
        *(route for kind in KINDS for route in (              # the same four verbs for each of the three kinds
            api.route(f"/v1/{kind}", "GET", f"{kind}.read", partial(listed, kind)),
            api.route(f"/v1/{kind}/{{name}}", "GET", f"{kind}.read", partial(one, kind)),
            api.route(f"/v1/{kind}/{{name}}", "PUT", f"{kind}.write", partial(apply, kind)),
            api.route(f"/v1/{kind}/{{name}}", "DELETE", f"{kind}.write", partial(delete, kind)))),
    ]
