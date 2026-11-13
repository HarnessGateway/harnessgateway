"""The internal API: what trusted steps and the egress proxy ask of the platform (spec §15).

It listens on its own port, on the control network only. A step proves itself with the token made for one
turn or one install, and can reach only that turn's or that install's organization. The proxy proves itself
with the key it shares with the platform.
"""

import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from harnessgateway.api import errors
from harnessgateway.domain.handover import gap
from harnessgateway.domain.request import file_ids
from harnessgateway.errors import Forbidden, Invalid, NotFound, Unauthorized
from harnessgateway.policies import Policies
from harnessgateway.proxy.authority import bundle
from harnessgateway.store.blobs import BlobMissing
from harnessgateway.store.egress import Connection
from harnessgateway.store.snapshots import chunk_key
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores

_MIB = 1024 * 1024


@dataclass(frozen=True)
class Step:
    """Who is calling: the step of one turn, or of one install."""

    org_id: str
    turn: Turn | None = None
    install_id: str | None = None
    archive_key: str | None = None

    def turn_named(self, turn_id: str) -> Turn:
        if self.turn is None or self.turn.id != turn_id:
            raise Forbidden("this token is not for that turn")
        return self.turn

    def may_read(self, key: str) -> None:
        """An install's step reads its package; a turn's step reads its organization's snapshots and files."""
        allowed = [self.archive_key] if self.turn is None else None
        prefixes = (f"orgs/{self.org_id}/snapshots/", f"orgs/{self.org_id}/files/")
        if (allowed is not None and key not in allowed) or (allowed is None and not key.startswith(prefixes)):
            raise Forbidden("this token may not read that object")


def internal_app(stores: Stores) -> FastAPI:
    app = FastAPI(title="HarnessGateway internal API", docs_url=None, redoc_url=None, openapi_url=None)
    errors.install(app)

    async def step(authorization: Annotated[str | None, Header()] = None) -> Step:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized("a step token is required")
        token_hash = stores.hash(token)
        turn = await stores.turns.by_step_token(token_hash)
        if turn is not None:
            return Step(turn.org_id, turn=turn)
        install = await stores.harnesses.install_of_token(token_hash)
        if install is not None:
            return Step(install[0], install_id=install[1], archive_key=install[2])
        raise Unauthorized("the step token is not valid, or its turn is over")

    Caller = Annotated[Step, Depends(step)]

    async def parent_manifest_key(turn: Turn) -> tuple[str | None, str | None]:
        """The parent turn's snapshot: its id and the key of its manifest; or neither."""
        if turn.parent_id is None:
            return None, None
        parent = await stores.turns.get(turn.org_id, turn.parent_id)
        if parent.snapshot_id is None:
            raise NotFound(f"the parent turn {parent.id} has no snapshot")
        return parent.snapshot_id, (await stores.snapshots.get(turn.org_id, parent.snapshot_id)).manifest_key

    @app.get("/internal/steps/restore/{turn_id}")
    async def restore_plan(turn_id: str, caller: Caller) -> dict[str, Any]:
        turn = caller.turn_named(turn_id)
        snapshot_id, key = await parent_manifest_key(turn)
        session = await stores.turns.session(turn.org_id, turn.session_id)
        chain = await stores.turns.chain(turn.org_id, turn.id)
        unseen = chain[len(chain) - len(gap([earlier.past for earlier in chain], turn.harness)):]
        history = [
            {"id": earlier.id, "harness": earlier.harness, "status": earlier.status, "input": earlier.input,
             "output": earlier.output}
            for earlier in unseen if earlier.input is not None             # a deleted turn has nothing to show
        ]
        files = [await stores.files.get(turn.org_id, file_id) for file_id in file_ids(turn.request or {})]
        return {
            "manifest_key": key,
            "warm": snapshot_id is not None and session.volume_snapshot_id == snapshot_id,
            "request": turn.request or {},
            "history": history,
            "handover": turn.handover,
            "files": [{"id": file.id, "filename": file.filename, "key": file.object_key} for file in files],
        }

    @app.get("/internal/steps/save/{turn_id}")
    async def save_plan(turn_id: str, caller: Caller) -> dict[str, Any]:
        _, key = await parent_manifest_key(caller.turn_named(turn_id))
        return {"manifest_key": key}

    @app.put("/internal/steps/save/{turn_id}/manifest")
    async def record(turn_id: str, request: Request, caller: Caller) -> dict[str, Any]:
        turn = caller.turn_named(turn_id)
        environment = await stores.environments.get(turn.org_id, turn.environment)
        snapshot = await stores.snapshots.record(
            turn.org_id, session_id=turn.session_id, turn_id=turn.id, manifest=(await request.body()).decode(),
            limit_bytes=environment.disk_mb * _MIB)
        return {"snapshot_id": snapshot.id}

    @app.get("/internal/steps/unpack/{install_id}")
    async def unpack_plan(install_id: str, caller: Caller) -> dict[str, Any]:
        if caller.install_id != install_id:
            raise Forbidden("this token is not for that install")
        return {"archive_key": caller.archive_key}

    async def stream(key: str) -> StreamingResponse:
        pieces = stores.blobs.read(key)
        try:
            first = await anext(pieces, b"")
        except BlobMissing:
            raise NotFound("there is no such object") from None

        async def body() -> AsyncIterator[bytes]:
            yield first
            async for piece in pieces:
                yield piece
        return StreamingResponse(body(), media_type="application/octet-stream")

    @app.get("/internal/objects/{key:path}")
    async def read_object(key: str, caller: Caller) -> StreamingResponse:
        caller.may_read(key)
        return await stream(key)

    @app.post("/internal/blobs/missing")
    async def missing(request: Request, caller: Caller) -> dict[str, Any]:
        hashes = (await request.json()).get("hashes")
        if not isinstance(hashes, list):
            raise Invalid("hashes is a list of hashes")
        return {"missing": await stores.snapshots.missing(caller.org_id, hashes)}

    @app.get("/internal/blobs/{digest}")
    async def get_chunk(digest: str, caller: Caller) -> StreamingResponse:
        await stores.snapshots.chunk(caller.org_id, digest)            # refuses what is not a stored chunk
        return await stream(chunk_key(caller.org_id, digest))

    @app.put("/internal/blobs/{digest}")
    async def put_chunk(digest: str, request: Request, caller: Caller) -> dict[str, Any]:
        await stores.snapshots.put_chunk(caller.org_id, digest, await request.body())
        return {}

    # for the egress proxy (spec §15.2)

    def proxy(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, key = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), stores.settings.proxy_key.encode()):
            raise Unauthorized("the proxy key is required")

    Proxy = Depends(proxy)
    policies = Policies(stores)

    @app.get("/internal/ca", response_class=PlainTextResponse)
    async def ca_bundle() -> str:
        """What a sandbox trusts: public, so anything on the control network may read it."""
        certificate, _ = await stores.platform.authority()
        return bundle(certificate)

    @app.get("/internal/proxy/authority", dependencies=[Proxy])
    async def authority() -> dict[str, Any]:
        certificate, key = await stores.platform.authority()
        return {"certificate": certificate, "key": key}

    @app.post("/internal/proxy/policy", dependencies=[Proxy])
    async def policy(request: Request) -> dict[str, Any]:
        asked = await request.json()
        found = await policies.of(str(asked.get("id")), str(asked.get("token")))
        if found is None:
            raise NotFound("no running turn or install has that id and token")
        return found.to_json()

    @app.post("/internal/proxy/records", dependencies=[Proxy])
    async def records(request: Request) -> dict[str, Any]:
        by_org: dict[str, list[Connection]] = {}
        for record in (await request.json())["records"]:
            by_org.setdefault(record.pop("org_id"), []).append(Connection(
                **{**record, "started_at": datetime.fromtimestamp(record["started_at"], UTC),
                   "ended_at": datetime.fromtimestamp(record["ended_at"], UTC)}))
        for org_id, connections in by_org.items():
            await stores.egress.add(org_id, connections)
        return {}

    return app
