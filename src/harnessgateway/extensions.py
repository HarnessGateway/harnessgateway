"""The seams: how an edition adds to the core without changing it.

An extension is a class named in HG_EXTENSIONS as `module:Class`. It subclasses `Extension` and overrides the
seams it uses; every seam's default is the core's own behaviour. An edition never patches the core. It replaces
a part (who is calling, what they may do, where sandboxes run, where bytes are kept, how secrets are sealed), is
asked before a turn starts, hears what happened, or adds routes, schema and console pages of its own.
"""

import importlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from harnessgateway.domain.principal import Principal
from harnessgateway.domain.sealing import Sealer
from harnessgateway.driver.base import Driver
from harnessgateway.settings import Settings
from harnessgateway.store.blobs import BlobStore

if TYPE_CHECKING:
    from harnessgateway.api.http import Api


class Extension:
    def drivers(self) -> dict[str, Callable[[Settings], Driver]]:
        """Drivers by name, chosen with HG_DRIVER."""
        return {}

    def blob_stores(self) -> dict[str, Callable[[str], BlobStore]]:
        """Blob stores by URL scheme, chosen with HG_BLOBS (`s3://bucket`, …). Each is given the whole URL."""
        return {}

    def sealer(self, settings: Settings) -> Sealer | None:
        """What seals secrets at rest in place of the installation's key (a key service); or None."""
        return None

    async def authenticate(self, request: Request) -> Principal | None:
        """Who a request comes from, if this extension knows; otherwise None, and the next way is tried, the
        last being an API key."""
        return None

    async def authorize(self, who: Principal, action: str) -> None:
        """Called before a route runs, with what it does (`responses.write`, `providers.read`, …). Raise a
        `Forbidden` to refuse it. The core allows whoever is authenticated to do everything."""

    async def before_turn(self, who: Principal, request: dict[str, Any]) -> None:
        """Called before a request becomes a turn. Raise a `Refused` to refuse it, with words for the caller."""

    async def on_event(self, name: str, about: dict[str, Any]) -> None:
        """Called with what happened, as it happens: `turn.finished` with what the turn used, and each change to
        a definition, a key or a session (`providers.applied`, `harnesses.deleted`, `keys.created`,
        `sessions.deleted`, …), each with `org_id`, `at`, `by` and `target`, and no secret in any of them. The
        core keeps no record of changes: an audit log is an edition's to keep, from these."""

    def routes(self, api: "Api") -> list[BaseRoute]:
        """Routes added to the public API. `api.route` makes one that is authenticated and authorized as the
        core's own are, and `api.platform` is what they are built from."""
        return []

    def migrations(self) -> list[Path]:
        """Folders of schema files, applied after the core's."""
        return []

    def console(self) -> Path | None:
        """A folder of files served with the console's own, at the same paths. Its `edition.js` names the pages
        it adds; a file of any other of the console's names takes that file's place."""
        return None


class Extensions:
    """Every loaded extension, asked in the order they were named."""

    def __init__(self, extensions: Sequence[Extension] = ()) -> None:
        self._all = list(extensions)

    @classmethod
    def load(cls, names: Sequence[str]) -> "Extensions":
        loaded = []
        for name in names:
            module, _, attribute = name.partition(":")
            made = getattr(importlib.import_module(module), attribute)()
            if not isinstance(made, Extension):
                raise TypeError(f"{name} is not an Extension")
            loaded.append(made)
        return cls(loaded)

    def drivers(self) -> dict[str, Callable[[Settings], Driver]]:
        return {name: make for extension in self._all for name, make in extension.drivers().items()}

    def blob_stores(self) -> dict[str, Callable[[str], BlobStore]]:
        return {scheme: make for extension in self._all for scheme, make in extension.blob_stores().items()}

    def sealer(self, settings: Settings) -> Sealer | None:
        return next((made for extension in self._all if (made := extension.sealer(settings)) is not None), None)

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

    async def on_event(self, name: str, about: dict[str, Any]) -> None:
        for extension in self._all:
            await extension.on_event(name, about)

    def routes(self, api: "Api") -> list[BaseRoute]:
        return [route for extension in self._all for route in extension.routes(api)]

    def migrations(self) -> list[Path]:
        return [folder for extension in self._all for folder in extension.migrations()]

    def console(self) -> list[Path]:
        return [folder for extension in self._all if (folder := extension.console()) is not None]
