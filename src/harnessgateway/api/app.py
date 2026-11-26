"""The two applications a platform serves: the public API, for whoever is authenticated, with the console's pages
beside it; and the internal one, which only the egress proxy calls, on the installation's own network."""

import hmac
import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from harnessgateway.api import console, containers, files, organization, resources, responses, sessions, views
from harnessgateway.api.http import Api, answered, parse_object, read_body
from harnessgateway.domain.egress import Connection
from harnessgateway.errors import Invalid, NotFound, Unauthorized
from harnessgateway.platform import Platform
from harnessgateway.settings import SettingsError


def _added(own: Sequence[BaseRoute], added: Sequence[BaseRoute]) -> list[BaseRoute]:
    """The core's routes and then the extensions'. A path and method that already has a route is refused: an
    extension adds to the API, and never takes the place of a route that is there."""
    taken: set[tuple[str, str]] = set()
    for route in (*own, *added):
        assert isinstance(route, Route), "a route of the public API is a path and its methods"
        for method in sorted((route.methods or {"GET"}) - {"HEAD"}):
            if (route.path, method) in taken:
                raise SettingsError(f"HG_EXTENSIONS: two routes answer {method} {route.path}: an extension adds "
                                    "routes, and replaces none")
            taken.add((route.path, method))
    return [*own, *added]


def public_app(platform: Platform) -> Starlette:
    api = Api(platform)

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(_: Request) -> Response:
        """Whether the process can do its work: reach the database, and reach what runs its sandboxes."""
        try:
            await platform.db.one(None, "SELECT 1")
            await platform.driver.isolation()
        except Exception as error:
            return JSONResponse({"status": "not ready", "reason": str(error)[:200]}, 503)
        return JSONResponse({"status": "ready"})

    own = [Route("/healthz", healthz), Route("/readyz", readyz),
           *responses.routes(api), *files.routes(api), *containers.routes(api), *views.routes(api),
           *resources.routes(api), *sessions.routes(api), *organization.routes(api),
           *(console.routes(platform.extensions.console()) if platform.settings.console else ())]
    return Starlette(routes=_added(own, platform.extensions.routes(api)))


def internal_app(platform: Platform) -> Starlette:
    """What the egress proxy asks of the platform. The proxy proves itself with the key the two share."""
    def from_the_proxy(request: Request) -> None:
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), platform.settings.proxy_key.encode()):
            raise Unauthorized("the proxy key is required")

    async def authority(request: Request) -> Any:
        from_the_proxy(request)
        certificate, key = await platform.authority()
        return {"certificate": certificate, "key": key}

    async def policy(request: Request) -> Any:
        from_the_proxy(request)
        asked = json.loads(await read_body(request))
        found = await platform.grants.policy(str(asked.get("id")), str(asked.get("token")))
        if found is None:
            raise NotFound("no running turn or install has that id and token")
        return found.to_json()

    async def passed(request: Request) -> Any:
        """What passed through the proxy since it last said: each connection a sandbox made or was refused, and
        the bytes of connections it could no longer hold one by one. The bytes are added to what each
        organization used, and the extensions are told of each connection."""
        from_the_proxy(request)
        said = parse_object(await read_body(request))
        try:
            connections = [Connection(**each) for each in said["connections"]]
            lost = [(str(each["org_id"]), int(each["sent"]), int(each["received"])) for each in said["lost"]]
        except (KeyError, TypeError, ValueError):
            raise Invalid("what passed is `connections`, each one whole, and `lost`, each an organization's "
                          "bytes") from None
        sent: Counter[str] = Counter()
        received: Counter[str] = Counter()
        for org_id, out, back in [*lost, *((each.org_id, each.sent, each.received) for each in connections)]:
            sent[org_id] += out
            received[org_id] += back
        for org_id in sent.keys() | received.keys():
            if sent[org_id] or received[org_id]:
                await platform.usage.add(org_id, network_out_bytes=sent[org_id], network_in_bytes=received[org_id])
        for each in connections:
            about = each.to_json()
            await platform.announce("egress.connection", about.pop("org_id"), **about)
        return {}

    return Starlette(routes=[
        Route("/internal/authority", answered(authority), methods=["GET"]),
        Route("/internal/policy", answered(policy), methods=["POST"]),
        Route("/internal/passed", answered(passed), methods=["POST"]),
    ])
