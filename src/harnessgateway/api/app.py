"""The two applications a platform serves: the public API, for whoever is authenticated, with the console's pages
beside it; and the internal one, which only the egress proxy calls, on the installation's own network."""

import hmac
import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from harnessgateway.api import console, containers, files, organization, resources, responses, sessions, views
from harnessgateway.api.http import Api, answered, read_body
from harnessgateway.errors import NotFound, Unauthorized
from harnessgateway.platform import Platform
from harnessgateway.proxy.authority import new_authority


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

    return Starlette(routes=[
        Route("/healthz", healthz), Route("/readyz", readyz),
        *responses.routes(api), *files.routes(api), *containers.routes(api), *views.routes(api), *resources.routes(api),
        *sessions.routes(api), *organization.routes(api), *platform.extensions.routes(api),
        *(console.routes(platform.extensions.console()) if platform.settings.console else ()),
    ])


def internal_app(platform: Platform) -> Starlette:
    """What the egress proxy asks of the platform. The proxy proves itself with the key the two share."""
    def from_the_proxy(request: Request) -> None:
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), platform.settings.proxy_key.encode()):
            raise Unauthorized("the proxy key is required")

    async def authority(request: Request) -> Any:
        from_the_proxy(request)
        certificate, key = await platform.authority.get(new_authority)
        return {"certificate": certificate, "key": key}

    async def policy(request: Request) -> Any:
        from_the_proxy(request)
        asked = json.loads(await read_body(request))
        found = await platform.grants.policy(str(asked.get("id")), str(asked.get("token")))
        if found is None:
            raise NotFound("no running turn or install has that id and token")
        return found.to_json()

    async def passed(request: Request) -> Any:
        """The bytes each organization's sandboxes sent and received since the proxy last said: added to what
        each used."""
        from_the_proxy(request)
        for counted in json.loads(await read_body(request)):
            await platform.usage.add(counted["org_id"], network_out_bytes=int(counted["sent"]),
                                     network_in_bytes=int(counted["received"]))
        return {}

    return Starlette(routes=[
        Route("/internal/authority", answered(authority), methods=["GET"]),
        Route("/internal/policy", answered(policy), methods=["POST"]),
        Route("/internal/passed", answered(passed), methods=["POST"]),
    ])
