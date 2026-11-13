"""The extension points (spec §19): how another edition adds to the core without changing it.

An extension is a class named in HG_EXTENSIONS as `module:Class`. It subclasses `Extension` and overrides the
points it uses; every point's default is to add nothing.
"""

import importlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from harnessgateway.domain.principal import Principal
from harnessgateway.drivers.base import Driver
from harnessgateway.settings import Settings
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.turns import Turn

DriverFactory = Callable[[Settings], Driver]
BlobStoreFactory = Callable[[str], BlobStore]


class Extension:
    def drivers(self) -> dict[str, DriverFactory]:
        """Drivers by name, chosen with HG_DRIVER."""
        return {}

    def blob_stores(self) -> dict[str, BlobStoreFactory]:
        """Blob stores by URL scheme, chosen with HG_DATA (`s3://bucket`, …). Each is given the whole URL."""
        return {}

    async def authenticate(self, request: Request) -> Principal | None:
        """Who a request comes from, if this extension knows; otherwise None, and the next is asked."""
        return None

    async def before_turn(self, principal: Principal, body: dict[str, Any]) -> None:
        """Called before a request becomes a turn. Raise `Forbidden` to refuse it, with a message for the caller."""

    async def after_turn(self, turn: Turn) -> None:
        """Called once a turn is final."""

    async def audit(self, record: dict[str, Any]) -> None:
        """Called with each audit record, after it is stored."""

    def routers(self, platform: Any) -> list[APIRouter]:
        """Routes added to the public API. `platform` is what the core's own routes are built from."""
        return []

    def migrations(self) -> list[Path]:
        """Folders of schema files, applied after the core's."""
        return []

    def console(self) -> list[Path]:
        """Folders of static files added to the console."""
        return []


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

    def drivers(self) -> dict[str, DriverFactory]:
        return {name: factory for extension in self._all for name, factory in extension.drivers().items()}

    def blob_stores(self) -> dict[str, BlobStoreFactory]:
        return {scheme: factory for extension in self._all for scheme, factory in extension.blob_stores().items()}

    async def authenticate(self, request: Request) -> Principal | None:
        for extension in self._all:
            principal = await extension.authenticate(request)
            if principal is not None:
                return principal
        return None

    async def before_turn(self, principal: Principal, body: dict[str, Any]) -> None:
        for extension in self._all:
            await extension.before_turn(principal, body)

    async def after_turn(self, turn: Turn) -> None:
        for extension in self._all:
            await extension.after_turn(turn)

    async def audit(self, record: dict[str, Any]) -> None:
        for extension in self._all:
            await extension.audit(record)

    def routers(self, platform: Any) -> list[APIRouter]:
        return [router for extension in self._all for router in extension.routers(platform)]

    def migrations(self) -> list[Path]:
        return [folder for extension in self._all for folder in extension.migrations()]

    def console(self) -> list[Path]:
        return [folder for extension in self._all for folder in extension.console()]
