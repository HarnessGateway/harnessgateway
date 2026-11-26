"""What belongs to the organization as a whole: what it used, its API keys, what a caller may do, and what this
installation is."""

from datetime import date
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

from harnessgateway import __version__
from harnessgateway.api.http import (
    KEYS_CHANGE_ALL,
    KEYS_READ_ALL,
    Api,
    listing,
    make_key,
    may,
    parse_object,
    read_body,
    theirs,
)
from harnessgateway.domain import contract, schema
from harnessgateway.domain.permission import ANYONE
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
        """A key that acts as the caller."""
        return await make_key(platform, who, parse_object(await read_body(request)), subject=who.subject)

    async def key_list(request: Request, who: Principal) -> Any:
        """The caller's own keys; everyone's, for a caller who may read those."""
        mine = None if await may(platform, who, KEYS_READ_ALL) else who.subject
        return listing([key.shown() for key in await keys.all(who.org_id, subject=mine)])

    async def key_revoke(request: Request, who: Principal) -> Any:
        key = await keys.get(who.org_id, request.path_params["id"])
        await theirs(platform, who, key.subject, KEYS_CHANGE_ALL)
        await keys.revoke(who.org_id, key.id, by=who.subject)
        return {"id": key.id, "object": "key", "revoked": True}

    async def permissions(request: Request, who: Principal) -> Any:
        """What the caller may do, of every action this installation's routes name: what a client draws itself
        from."""
        return {"object": "permissions", "subject": who.subject, "key": who.key,
                "allows": [action for action in sorted(api.actions) if await may(platform, who, action)]}

    async def about(request: Request, who: Principal) -> Any:
        return {"object": "platform", "version": __version__, "contract": contract.VERSION, "sdk": schema.SDK,
                "driver": platform.driver.name, "isolation": await platform.driver.isolation(),
                "extensions": platform.extensions.described()}

    return [
        api.route("/v1/usage", "GET", "usage.read", usage),
        api.route("/v1/keys", "POST", "keys.write", key_create),
        api.route("/v1/keys", "GET", "keys.read", key_list),
        api.route("/v1/keys/{id}", "DELETE", "keys.write", key_revoke),
        api.route("/v1/permissions", "GET", ANYONE, permissions),
        api.route("/v1/platform", "GET", "platform.read", about),
    ]
