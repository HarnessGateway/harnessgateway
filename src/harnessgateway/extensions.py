"""The seams: how an edition adds to the core without changing it.

An extension is a class named in HG_EXTENSIONS as `module:Class`. It subclasses `Extension` and overrides the
seams it uses; every seam's default is the core's own behaviour. Extensions are a chain, asked in the order they
were named, and `Extensions` is what several of them together mean at each seam: a part is offered once, a
question is put to every one and any may refuse, and what one adds never takes the place of another's or of the
core's.
"""

import importlib
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from starlette.requests import Request
from starlette.routing import BaseRoute

from harnessgateway.domain.change import Change
from harnessgateway.domain.egress import EgressPolicy
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.sealing import Sealer
from harnessgateway.driver.base import Driver
from harnessgateway.errors import Invalid
from harnessgateway.ids import check_name
from harnessgateway.settings import Settings, SettingsError
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import CORE, Connection

if TYPE_CHECKING:
    from harnessgateway.api.http import Api
    from harnessgateway.platform import Platform

log = logging.getLogger(__name__)
Offered = TypeVar("Offered")


class Extension:
    name = ""                                  # a resource name: it owns its schema files and its console folder
    version = ""

    def drivers(self) -> dict[str, Callable[[Settings], Driver]]:
        """Drivers by name, chosen with HG_DRIVER."""
        return {}

    def blob_stores(self) -> dict[str, Callable[[str], BlobStore]]:
        """Blob stores by URL scheme, chosen with HG_BLOBS (`s3://bucket`, …). Each is given the whole URL."""
        return {}

    def sealer(self, settings: Settings) -> Sealer | None:
        """What seals secrets at rest in place of the installation's key (a key service); or None. With one,
        HG_SECRET_KEY is not needed."""
        return None

    def migrations(self) -> Path | None:
        """Its folder of schema files, applied after the core's and recorded under its name."""
        return None

    async def start(self, platform: "Platform") -> None:
        """Called once the schema is up to date and before anything is served, in `serve` and in `init`: where an
        extension is handed the platform. Raising stops the process."""

    async def authenticate(self, request: Request) -> Principal | None:
        """Who a request comes from, if this extension knows; otherwise None, and the next way is tried, the
        last being an API key."""
        return None

    async def authorize(self, who: Principal, action: str) -> None:
        """Called before a route runs, with what it does (`responses.write`, `providers.read`, …). Raise a
        `Forbidden` to refuse it. The core allows whoever is authenticated to do everything."""

    async def before_turn(self, who: Principal, request: dict[str, Any]) -> None:
        """Called before a request becomes a turn. Raise a `Refused` to refuse it, with words for the caller."""

    async def egress(self, org_id: str, policy: EgressPolicy) -> EgressPolicy:
        """What a sandbox of this organization may reach, given what its environment says: the same policy, or
        one narrowed. More may be denied; nothing more may be allowed."""
        return policy

    async def on_change(self, conn: Connection, change: Change) -> None:
        """Called inside the transaction that makes a change, with its connection: what is written with it is
        kept with the change, or neither is. Raising undoes the change, and refuses the request."""

    async def on_event(self, name: str, about: dict[str, Any]) -> None:
        """Called with what happened by itself, after it happened: `turn.finished`, `egress.connection`. Each
        carries `org_id` and `at`, and no secret. It cannot be refused, and it may be missed."""

    async def housekeep(self, platform: "Platform", elapsed_s: float) -> None:
        """Called at the end of every housekeeping round: once a minute, in one process, however many run.
        `elapsed_s` is the time since the last round."""

    def routes(self, api: "Api") -> list[BaseRoute]:
        """Routes added to the public API. `api.route` makes one that is authenticated and authorized as the
        core's own are."""
        return []

    def console(self) -> Path | None:
        """Its folder of console files, served under /console/x/<name>/. Its `edition.js` names the pages and
        the ways to sign in that it adds."""
        return None


class Extensions:
    """Every loaded extension, asked in the order they were named."""

    def __init__(self, extensions: Sequence[Extension] = ()) -> None:
        self._all = list(extensions)
        names = [extension.name for extension in self._all]
        for name in names:
            try:
                check_name(name, "an extension's name")
            except Invalid as refusal:
                raise SettingsError(f"HG_EXTENSIONS: {refusal.message}") from None
            if name == CORE or names.count(name) > 1:
                raise SettingsError(f"HG_EXTENSIONS: the name {name!r} is taken: each extension has a name of its own")

    @classmethod
    def load(cls, names: Sequence[str]) -> "Extensions":
        loaded = []
        for name in names:
            module, _, attribute = name.partition(":")
            try:
                made = getattr(importlib.import_module(module), attribute)()
            except (ImportError, AttributeError) as error:
                raise SettingsError(f"HG_EXTENSIONS names {name}, which could not be loaded: {error}") from None
            if not isinstance(made, Extension):
                raise SettingsError(f"HG_EXTENSIONS names {name}, which is not an Extension")
            loaded.append(made)
        return cls(loaded)

    def described(self) -> list[dict[str, str]]:
        return [{"name": extension.name, "version": extension.version} for extension in self._all]

    def named(self, name: str) -> Extension | None:
        """The loaded extension of this name; None when there is none. It is how an extension that stands on
        another finds it."""
        return next((extension for extension in self._all if extension.name == name), None)

    def _offered(self, what: str, each: Callable[[Extension], dict[str, Offered]], own: dict[str, Offered],
                 ) -> dict[str, Offered]:
        """Everything offered under a name, the core's own included. A name offered twice is refused."""
        offered = dict(own)
        for extension in self._all:
            for name, made in each(extension).items():
                if name in offered:
                    raise SettingsError(f"{extension.name} offers {what} {name!r}, which is already offered")
                offered[name] = made
        return offered

    def drivers(self, own: dict[str, Callable[[Settings], Driver]]) -> dict[str, Callable[[Settings], Driver]]:
        return self._offered("the driver", lambda extension: extension.drivers(), own)

    def blob_stores(self) -> dict[str, Callable[[str], BlobStore]]:
        return self._offered("a blob store for", lambda extension: extension.blob_stores(), {})

    def sealer(self, settings: Settings) -> Sealer | None:
        return next((made for extension in self._all if (made := extension.sealer(settings)) is not None), None)

    def migrations(self) -> list[tuple[str, Path]]:
        return [(extension.name, folder) for extension in self._all if (folder := extension.migrations()) is not None]

    async def start(self, platform: "Platform") -> None:
        for extension in self._all:
            await extension.start(platform)

    async def authenticate(self, request: Request) -> Principal | None:
        for extension in self._all:
            if (who := await extension.authenticate(request)) is not None:
                return who
        return None

    async def authorize(self, who: Principal, action: str) -> None:
        for extension in self._all:
            await extension.authorize(who, action)

    async def before_turn(self, who: Principal, request: dict[str, Any]) -> None:
        for extension in self._all:
            await extension.before_turn(who, request)

    async def egress(self, org_id: str, policy: EgressPolicy) -> EgressPolicy:
        for extension in self._all:
            narrowed = await extension.egress(org_id, policy)
            if not policy.narrowed_to(narrowed):
                raise ValueError(f"{extension.name} widened what a sandbox may reach: an extension only narrows it")
            policy = narrowed
        return policy

    async def on_change(self, conn: Connection, change: Change) -> None:
        for extension in self._all:
            await extension.on_change(conn, change)

    async def on_event(self, name: str, about: dict[str, Any]) -> None:
        """Tell each extension separately: one that fails is logged, and fails neither another nor what it was
        told about."""
        for extension in self._all:
            try:
                await extension.on_event(name, about)
            except Exception:
                log.exception("%s failed on %s", extension.name, name)

    async def housekeep(self, platform: "Platform", elapsed_s: float) -> None:
        for extension in self._all:
            try:
                await extension.housekeep(platform, elapsed_s)
            except Exception:
                log.exception("%s failed in its housekeeping; it is asked again next round", extension.name)

    def routes(self, api: "Api") -> list[BaseRoute]:
        return [route for extension in self._all for route in extension.routes(api)]

    def console(self) -> list[tuple[str, Path]]:
        return [(extension.name, folder) for extension in self._all if (folder := extension.console()) is not None]
