"""A sample edition: an extension that uses every seam the core has. It is here so that a seam which stops
working fails a test, and as the shortest example of how an edition is written. Like any edition, it imports the
core only through `harnessgateway.edition`, and `harnessgateway.testing` for its stand-in sandbox."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from harnessgateway.edition import (
    Api,
    BlobMissing,
    BlobStore,
    Change,
    Connection,
    Driver,
    EgressPolicy,
    Extension,
    Forbidden,
    Invalid,
    Platform,
    Principal,
    Sealer,
    Settings,
)
from harnessgateway.testing import FakeDriver

HERE = Path(__file__).parent
DENIED = "denied-by-sample.example.com"


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
        return f"sample:{'/'.join(where)}:{value}"

    def open(self, sealed: str, *where: str) -> str:
        return sealed.removeprefix(f"sample:{'/'.join(where)}:")


class Edition(Extension):
    name, version = "sample", "0.1.0"

    def __init__(self) -> None:
        self.blobs = MemoryBlobs()
        self.platform: Platform | None = None
        self.heard: list[tuple[str, dict[str, Any]]] = []
        self.rounds: list[float] = []
        self.failing: set[str] = set()             # the seams a test has told it to fail at

    def drivers(self) -> dict[str, Callable[[Settings], Driver]]:
        return {"sample": lambda settings: FakeDriver(settings.data / "folders")}

    def blob_stores(self) -> dict[str, Callable[[str], BlobStore]]:
        return {"memory": lambda url: self.blobs}

    def sealer(self, settings: Settings) -> Sealer | None:
        return MarkingSealer()

    def migrations(self) -> Path | None:
        return HERE / "edition_schema"

    async def start(self, platform: Platform) -> None:
        self.platform = platform                   # where an extension is handed the platform

    async def authenticate(self, request: Request) -> Principal | None:
        org_id, _, user = request.headers.get("x-sample-user", "").partition("/")      # single sign-on, in miniature
        return Principal(org_id, user) if user else None

    async def authorize(self, who: Principal, action: str) -> None:
        if who.subject == "reader" and action.endswith(".write"):
            raise Forbidden(f"{who.subject} may read, and {action} is not reading")
        if who.subject.startswith("member") and action.endswith("_all"):     # roles, in miniature: their own, not everyone's
            raise Forbidden(f"{who.subject} reaches what is their own, and {action} is everyone's")

    async def before_turn(self, who: Principal, request: dict[str, Any]) -> None:
        if who.subject == "over-quota":
            raise Invalid("this organization has used its quota")

    async def egress(self, org_id: str, policy: EgressPolicy) -> EgressPolicy:
        if "egress" in self.failing:               # what an extension must never do: allow more
            return EgressPolicy("open")
        return EgressPolicy(policy.mode, policy.allow, (*policy.deny, DENIED))

    async def on_change(self, conn: Connection, change: Change) -> None:
        if change.target == "unrecorded":
            raise Forbidden("the sample edition could not record this change")
        await conn.execute("INSERT INTO sample_audit (org_id, name, target, by) VALUES (%s, %s, %s, %s)",
                           [change.org_id, change.name, change.target, change.by])

    async def on_event(self, name: str, about: dict[str, Any]) -> None:
        if "on_event" in self.failing:
            raise RuntimeError("the sample edition failed on an event")
        self.heard.append((name, about))

    async def housekeep(self, platform: Platform, elapsed_s: float) -> None:
        if "housekeep" in self.failing:
            raise RuntimeError("the sample edition failed in its housekeeping")
        self.rounds.append(elapsed_s)

    def routes(self, api: Api) -> list[BaseRoute]:
        async def audit(request: Request, who: Principal) -> Any:
            rows = await api.platform.db.all(who.org_id, "SELECT name, target, by FROM sample_audit WHERE org_id = %s "
                                                         "ORDER BY id", [who.org_id])
            return {"object": "list", "data": rows}
        return [api.route("/v1/audit", "GET", "audit.read", audit)]

    def console(self) -> Path | None:
        return HERE / "edition_console"
