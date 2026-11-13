"""Who is calling the public API (spec §16): an extension's answer first, then an API key."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request

from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Unauthorized
from harnessgateway.extensions import Extensions
from harnessgateway.stores import Stores


class Authenticator:
    def __init__(self, stores: Stores, extensions: Extensions) -> None:
        self._stores = stores
        self._extensions = extensions

    async def __call__(self, request: Request) -> Principal:
        principal = await self._extensions.authenticate(request)
        if principal is not None:
            return principal
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and key:
            principal = await self._stores.keys.authenticate(key.strip())
            if principal is not None:
                return principal
            raise Unauthorized("the API key is not valid, or was revoked", code="invalid_api_key")
        raise Unauthorized("an API key is required: Authorization: Bearer hg_...", code="invalid_api_key")

    def role(self, name: str) -> Callable[..., Awaitable[Principal]]:
        """A route's dependency: the caller, refused unless their role is `name` or above. An extension's
        routes use it as the core's own do."""
        async def allowed(principal: Annotated[Principal, Depends(self)]) -> Principal:
            principal.require(name)
            return principal
        return allowed
