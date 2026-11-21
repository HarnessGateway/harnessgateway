"""What belongs to the organization as a whole: what it used, its API keys, and what this installation is."""

from datetime import date
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

from harnessgateway import __version__
from harnessgateway.api.http import Api, listing, parse_object, read_body
from harnessgateway.domain import contract
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Invalid


def routes(api: Api) -> list[Route]:
    platform = api.platform
    keys = platform.keys

    def day(request: Request, name: str) -> date | None:
        text = request.query_params.get(name)
        try:
            return date.fromisoformat(text) if text else None
        except ValueError:
            raise Invalid(f"{name} is a day such as 2026-11-01", param=name) from None

    async def usage(request: Request, who: Principal) -> Any:
        """What the organization used: in its lifetime, or from the day `start` up to, not including, `end`."""
        start, end = day(request, "start"), day(request, "end")
        return {"object": "usage", "start": start and start.isoformat(), "end": end and end.isoformat(),
                **await platform.usage.total(who.org_id, start=start, end=end)}

    async def key_create(request: Request, who: Principal) -> Any:
        key, text = await keys.create(who.org_id, parse_object(await read_body(request)).get("name"))
        await platform.announce("keys.created", who.org_id, by=who, target=key.id)
        return {**key.shown(), "key": text}        # the only time the key itself is shown

    async def key_list(request: Request, who: Principal) -> Any:
        return listing([key.shown() for key in await keys.all(who.org_id)])

    async def key_revoke(request: Request, who: Principal) -> Any:
        await keys.revoke(who.org_id, request.path_params["id"])
        await platform.announce("keys.revoked", who.org_id, by=who, target=request.path_params["id"])
        return {"id": request.path_params["id"], "object": "key", "revoked": True}

    async def about(request: Request, who: Principal) -> Any:
        return {"object": "platform", "version": __version__, "contract": contract.VERSION,
                "driver": platform.driver.name, "isolation": await platform.driver.isolation()}

    return [
        api.route("/v1/usage", "GET", "usage.read", usage),
        api.route("/v1/keys", "POST", "keys.write", key_create),
        api.route("/v1/keys", "GET", "keys.read", key_list),
        api.route("/v1/keys/{id}", "DELETE", "keys.write", key_revoke),
        api.route("/v1/platform", "GET", "platform.read", about),
    ]
