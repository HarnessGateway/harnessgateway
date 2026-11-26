"""A second extension, loaded after the sample edition: what shows that extensions are a chain. It adds its own
beside the first's and takes the place of nothing: a schema file of the same name, a console folder, a route, a
second say on who may, a second narrowing of the network."""

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from harnessgateway.edition import Api, Change, Connection, EgressPolicy, Extension, Forbidden, Platform, Principal

HERE = Path(__file__).parent
DENIED = "denied-by-second.example.com"


class Second(Extension):
    name, version = "second", "0.2.0"

    def __init__(self) -> None:
        self.heard: list[str] = []
        self.rounds: list[float] = []
        self.started_after: list[str] = []

    def migrations(self) -> Path | None:
        return HERE / "second_schema"

    async def start(self, platform: Platform) -> None:
        self.started_after = [each["name"] for each in platform.extensions.described()]

    async def authorize(self, who: Principal, action: str) -> None:
        if action == "notes.read" and who.subject == "stranger":
            raise Forbidden("the second extension does not know this caller")

    async def egress(self, org_id: str, policy: EgressPolicy) -> EgressPolicy:
        return EgressPolicy(policy.mode, policy.allow, (*policy.deny, DENIED))

    async def on_change(self, conn: Connection, change: Change) -> None:
        await conn.execute("INSERT INTO second_notes (org_id, said) VALUES (%s, %s)", [change.org_id, change.name])

    async def on_event(self, name: str, about: dict[str, Any]) -> None:
        self.heard.append(name)

    async def housekeep(self, platform: Platform, elapsed_s: float) -> None:
        self.rounds.append(elapsed_s)

    def routes(self, api: Api) -> list[BaseRoute]:
        async def notes(request: Request, who: Principal) -> Any:
            rows = await api.platform.db.all(who.org_id, "SELECT said FROM second_notes WHERE org_id = %s ORDER BY id",
                                             [who.org_id])
            return {"object": "list", "data": [row["said"] for row in rows]}
        return [api.route("/v1/notes", "GET", "notes.read", notes)]

    def console(self) -> Path | None:
        return HERE / "second_console"


class Clashing(Extension):
    """An extension that would take a route's place: it answers a path and method the core already does."""

    name = "clashing"

    def routes(self, api: Api) -> list[BaseRoute]:
        async def keys(request: Request, who: Principal) -> Any:
            return {}
        return [api.route("/v1/keys", "GET", "keys.read", keys)]
