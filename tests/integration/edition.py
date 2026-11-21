"""A sample edition: an extension that uses every seam the core has. It is here so that a seam which stops
working fails a test, and as the shortest example of how an edition is written."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from harnessgateway.api.http import Api
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.sealing import Sealer
from harnessgateway.driver.base import Driver
from harnessgateway.errors import Forbidden, Invalid
from harnessgateway.extensions import Extension
from harnessgateway.ids import DEFAULT_ORG
from harnessgateway.settings import Settings
from harnessgateway.store.blobs import BlobMissing, BlobStore
from tests.helpers import FakeDriver


class MemoryBlobs:
    """A blob store somewhere other than a directory."""

    def __init__(self) -> None:
        self.kept: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self.kept[key] = data

    async def get(self, key: str) -> bytes:
        if key not in self.kept:
            raise BlobMissing(key)
        return self.kept[key]

    async def write(self, key: str, parts: AsyncIterator[bytes]) -> int:
        self.kept[key] = b"".join([part async for part in parts])
        return len(self.kept[key])

    async def read(self, key: str) -> AsyncIterator[bytes]:
        yield await self.get(key)

    async def delete(self, key: str) -> None:
        self.kept.pop(key, None)


class MarkingSealer:
    """A sealer that is not the core's: what it sealed can be told apart."""

    def seal(self, value: str, *where: str) -> str:
        return f"edition:{'/'.join(where)}:{value}"

    def open(self, sealed: str, *where: str) -> str:
        return sealed.removeprefix(f"edition:{'/'.join(where)}:")


class Edition(Extension):
    def __init__(self) -> None:
        self.blobs = MemoryBlobs()
        self.heard: list[tuple[str, dict[str, Any]]] = []
        self.api: Api | None = None

    def drivers(self) -> dict[str, Callable[[Settings], Driver]]:
        return {"edition": lambda settings: FakeDriver(settings.data / "folders")}

    def blob_stores(self) -> dict[str, Callable[[str], BlobStore]]:
        return {"memory": lambda url: self.blobs}

    def sealer(self, settings: Settings) -> Sealer | None:
        return MarkingSealer()

    async def authenticate(self, request: Request) -> Principal | None:
        user = request.headers.get("x-edition-user")           # single sign-on, in miniature
        return Principal(DEFAULT_ORG, user) if user else None

    async def authorize(self, who: Principal, action: str) -> None:
        if who.subject == "reader" and action.endswith(".write"):
            raise Forbidden(f"{who.subject} may read, and {action} is not reading")

    async def before_turn(self, who: Principal, request: dict[str, Any]) -> None:
        if who.subject == "over-quota":
            raise Invalid("this organization has used its quota")

    async def on_event(self, name: str, about: dict[str, Any]) -> None:
        self.heard.append((name, about))
        if self.api is not None:
            await self.api.platform.db.run(about["org_id"], "INSERT INTO edition_audit (org_id, name, target) "
                                                            "VALUES (%s, %s, %s)", [about["org_id"], name, about.get("target")])

    def routes(self, api: Api) -> list[BaseRoute]:
        self.api = api

        async def audit(request: Request, who: Principal) -> Any:
            rows = await api.platform.db.all(who.org_id, "SELECT name, target FROM edition_audit WHERE org_id = %s "
                                                         "ORDER BY id", [who.org_id])
            return {"object": "list", "data": rows}
        return [api.route("/v1/audit", "GET", "audit.read", audit)]

    def migrations(self) -> list[Path]:
        return [Path(__file__).parent / "edition_schema"]
