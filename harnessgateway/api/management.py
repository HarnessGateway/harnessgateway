"""The management API: how an organization sets the platform up (spec §14). JSON, in shapes of our own."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from harnessgateway import __version__
from harnessgateway.api.files import page_of
from harnessgateway.api.public import read_json
from harnessgateway.catalog import Catalog
from harnessgateway.domain.archive import PACKED_MAX, PackageFile, write_archive
from harnessgateway.domain.contract import CONTRACT_VERSION
from harnessgateway.domain.environment import Environment
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.names import DEFAULT_ENVIRONMENT, check_id
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Invalid, NotFound, TooLarge
from harnessgateway.platform import Platform
from harnessgateway.store.harnesses import Install
from harnessgateway.store.keys import Key
from harnessgateway.store.providers import ProviderRecord
from harnessgateway.store.turns import SessionSummary, Turn

Role = Callable[[str], Any]


def _time(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise Invalid("the request body is a JSON object")
    return body


def _named(items: list[dict[str, Any]], *, after: str | None, limit: int) -> dict[str, Any]:
    """One page of things that are known by name (spec §14: `limit`, `after`, `has_more`, `last_id`)."""
    page = page_of([{**item, "id": item["name"]} for item in items], after=after, limit=limit)
    return {"object": "list", "data": [{k: v for k, v in item.items() if k != "id"} for item in page["data"]],
            "has_more": page["has_more"], "last_id": page["last_id"]}


# ── providers ───────────────────────────────────────────────────────────────────────────────────────

def _provider(record: ProviderRecord) -> dict[str, Any]:
    return {**record.provider.dump(), "has_key": record.has_key, "created_at": _time(record.created_at)}


def providers_router(platform: Platform, role: Role) -> APIRouter:
    router = APIRouter()
    providers = platform.stores.providers
    Developer = Annotated[Principal, Depends(role("developer"))]
    Admin = Annotated[Principal, Depends(role("admin"))]

    @router.post("/v1/providers")
    async def create(request: Request, principal: Admin) -> Any:
        body = _object(await read_json(request))
        record = await providers.create(principal.org_id, body)
        await platform.audit(principal, "provider.create", record.provider.name, {"fields": sorted(body)})
        return _provider(record)

    @router.get("/v1/providers")
    async def listing(principal: Developer, limit: int = 20, after: str | None = None) -> Any:
        return _named([_provider(r) for r in await providers.all(principal.org_id)], after=after, limit=limit)

    @router.get("/v1/providers/{name}")
    async def one(name: str, principal: Developer) -> Any:
        return _provider(await providers.get(principal.org_id, name))

    @router.patch("/v1/providers/{name}")
    async def update(name: str, request: Request, principal: Admin) -> Any:
        changes = _object(await read_json(request))
        record = await providers.update(principal.org_id, name, changes)
        await platform.audit(principal, "provider.update", name, {"fields": sorted(changes)})
        return _provider(record)

    @router.delete("/v1/providers/{name}")
    async def delete(name: str, principal: Admin) -> Any:
        await providers.delete(principal.org_id, name)
        await platform.audit(principal, "provider.delete", name)
        return {"name": name, "deleted": True}

    return router


# ── environments ────────────────────────────────────────────────────────────────────────────────────

def _environment(environment: Environment) -> dict[str, Any]:
    return environment.dump()                                   # secret values are never part of it


def environments_router(platform: Platform, role: Role) -> APIRouter:
    router = APIRouter()
    environments = platform.stores.environments
    Developer = Annotated[Principal, Depends(role("developer"))]
    Admin = Annotated[Principal, Depends(role("admin"))]

    @router.post("/v1/environments")
    async def create(request: Request, principal: Admin) -> Any:
        body = _object(await read_json(request))
        environment = await environments.create(principal.org_id, body)
        await platform.audit(principal, "environment.create", environment.name, {"fields": sorted(body)})
        return _environment(environment)

    @router.get("/v1/environments")
    async def listing(principal: Developer, limit: int = 20, after: str | None = None) -> Any:
        return _named([_environment(e) for e in await environments.all(principal.org_id)], after=after, limit=limit)

    @router.get("/v1/environments/{name}")
    async def one(name: str, principal: Developer) -> Any:
        return _environment(await environments.get(principal.org_id, name))

    @router.patch("/v1/environments/{name}")
    async def update(name: str, request: Request, principal: Admin) -> Any:
        changes = _object(await read_json(request))
        environment = await environments.update(principal.org_id, name, changes)
        await platform.audit(principal, "environment.update", name, {"fields": sorted(changes)})
        return _environment(environment)

    @router.delete("/v1/environments/{name}")
    async def delete(name: str, principal: Admin) -> Any:
        await environments.delete(principal.org_id, name)
        await platform.audit(principal, "environment.delete", name)
        return {"name": name, "deleted": True}

    return router


# ── harnesses ───────────────────────────────────────────────────────────────────────────────────────

def _install(install: Install) -> dict[str, Any]:
    return {"id": install.id, "image_id": install.image_id, "status": install.status,
            "updated_at": _time(install.updated_at)}


def harnesses_router(platform: Platform, role: Role) -> APIRouter:
    router = APIRouter()
    stores = platform.stores
    harnesses = stores.harnesses
    catalog = Catalog(Path(stores.settings.catalog))
    Developer = Annotated[Principal, Depends(role("developer"))]

    async def shown(org_id: str, harness: Harness) -> dict[str, Any]:
        manifest = harness.manifest
        return {
            "name": harness.name, "version": manifest.version, "description": manifest.description,
            "digest": harness.digest, "manifest": manifest.dump(), "provider": harness.provider,
            "model": harness.model, "settings": harness.setting_values(), "secrets_set": sorted(harness.secrets_set),
            "missing": harness.missing(), "added_at": _time(harness.added_at),
            "installs": [_install(i) for i in await harnesses.installs(org_id, harness.digest)],
        }

    async def from_session(org_id: str, session_id: object, path: object) -> bytes:
        """A package taken from a folder of a session's work, as its head turn left it."""
        if not isinstance(path, str) or not path.strip("/"):
            raise Invalid("path is the package's folder in the session's work", param="path")
        session = await stores.turns.session(org_id, check_id(session_id, "session"))
        head = await stores.turns.get(org_id, session.head_turn_id) if session.head_turn_id else None
        if head is None or head.snapshot_id is None:
            raise Invalid(f"session {session.id} has no saved work to take a package from", param="session")
        prefix = f"work/{path.strip('/')}/"
        manifest = await stores.snapshots.manifest(org_id, head.snapshot_id)
        entries = {p[len(prefix):]: e for p, e in manifest.entries.items() if e.kind == "f" and p.startswith(prefix)}
        if not entries:
            raise Invalid(f"the session's work has no folder {path.strip('/')!r}", param="path")
        if sum(entry.size for entry in entries.values()) > PACKED_MAX * 4:
            raise TooLarge("the folder is too large to be a package")
        files = []
        for name, entry in entries.items():
            content = b"".join([piece async for piece in stores.snapshots.content(org_id, entry)])
            files.append(PackageFile(name, bool(entry.mode & 0o100), content))
        return write_archive(files)

    @router.post("/v1/harnesses")
    async def add(request: Request, principal: Developer) -> Any:
        """Add or replace a harness: an uploaded archive, a catalog package, or a folder of a session's work."""
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            upload = (await request.form()).get("package")
            if upload is None or isinstance(upload, str):
                raise Invalid("upload the package archive as the form field `package`", param="package")
            archive, source = await upload.read(), "upload"
        else:
            body = _object(await read_json(request))
            if "catalog" in body:
                archive, source = catalog.archive(str(body["catalog"])), f"catalog:{body['catalog']}"
            elif "session" in body:
                archive = await from_session(principal.org_id, body["session"], body.get("path"))
                source = f"session:{body['session']}"
            else:
                raise Invalid('send a package archive, {"catalog": "<name>"}, or {"session": "sess_…", "path": '
                              '"<folder>"}')
        harness = await harnesses.add(principal.org_id, archive)
        await platform.audit(principal, "harness.add", harness.name,
                             {"version": harness.manifest.version, "digest": harness.digest, "source": source})
        platform.install_in_background(principal.org_id, harness.name, DEFAULT_ENVIRONMENT)
        return await shown(principal.org_id, harness)

    @router.get("/v1/harnesses")
    async def listing(principal: Developer, limit: int = 20, after: str | None = None) -> Any:
        found = [await shown(principal.org_id, harness) for harness in await harnesses.all(principal.org_id)]
        return _named(found, after=after, limit=limit)

    @router.get("/v1/harnesses/{name}")
    async def one(name: str, principal: Developer) -> Any:
        return await shown(principal.org_id, await harnesses.get(principal.org_id, name))

    @router.patch("/v1/harnesses/{name}")
    async def update(name: str, request: Request, principal: Developer) -> Any:
        changes = _object(await read_json(request))
        if "secrets" in changes:
            principal.require("admin")                          # secret values are an administrator's to set
        harness = await harnesses.update(principal.org_id, name, changes)
        detail = {"fields": sorted(changes), **{kind: sorted(changes[kind]) for kind in ("settings", "secrets")
                                               if isinstance(changes.get(kind), dict)}}
        await platform.audit(principal, "harness.update", name, detail)
        return await shown(principal.org_id, harness)

    @router.delete("/v1/harnesses/{name}")
    async def delete(name: str, principal: Developer) -> Any:
        await harnesses.delete(principal.org_id, name)
        await platform.audit(principal, "harness.delete", name)
        return {"name": name, "deleted": True}

    @router.post("/v1/harnesses/{name}/install")
    async def install(name: str, request: Request, principal: Developer) -> Any:
        body = _object(await read_json(request))
        harness = await harnesses.get(principal.org_id, name)
        environment = await stores.environments.get(principal.org_id, str(body.get("environment",
                                                                                 DEFAULT_ENVIRONMENT)))
        platform.install_in_background(principal.org_id, harness.name, environment.name)
        await platform.audit(principal, "harness.install", name, {"environment": environment.name})
        return {"name": name, "environment": environment.name, "status": "pending"}

    @router.get("/v1/harnesses/{name}/installs")
    async def installs(name: str, principal: Developer) -> Any:
        harness = await harnesses.get(principal.org_id, name)
        found = await harnesses.installs(principal.org_id, harness.digest)
        return {"object": "list", "data": [{**_install(i), "log": i.log_tail} for i in found], "has_more": False,
                "last_id": found[-1].id if found else None}

    @router.get("/v1/catalog")
    async def offered(principal: Developer) -> Any:
        installed = {harness.name: harness for harness in await harnesses.all(principal.org_id)}
        return {"object": "list", "has_more": False, "last_id": None, "data": [
            {"name": m.name, "version": m.version, "description": m.description, "apis": list(m.apis),
             "installed_version": installed[m.name].manifest.version if m.name in installed else None}
            for m in catalog.entries()]}

    return router


# ── sessions and turns ──────────────────────────────────────────────────────────────────────────────

def _session(summary: SessionSummary) -> dict[str, Any]:
    return {"id": summary.id, "head_response_id": summary.head_turn_id,
            "forked_from_response_id": summary.forked_from_turn_id, "turns": summary.turns, "bytes": summary.bytes,
            "created_at": _time(summary.created_at), "last_active_at": _time(summary.last_active_at)}


def _turn(turn: Turn) -> dict[str, Any]:
    return {"id": turn.id, "number": turn.number, "parent_id": turn.parent_id, "status": turn.status,
            "reason": turn.reason, "harness": turn.harness, "harness_version": turn.harness_version,
            "provider": turn.provider, "model": turn.model, "environment": turn.environment,
            "deleted": turn.deleted_at is not None, "created_at": _time(turn.created_at),
            "completed_at": _time(turn.completed_at)}


def sessions_router(platform: Platform, role: Role) -> APIRouter:
    router = APIRouter()
    stores = platform.stores
    turns = stores.turns
    Developer = Annotated[Principal, Depends(role("developer"))]

    def session_id(value: str) -> str:
        try:
            return check_id(value, "session")
        except Invalid:
            raise NotFound(f"there is no session {value}") from None

    async def snapshot_of(org_id: str, session: str, response_id: str | None) -> Any:
        """The snapshot of one of a session's turns: the one named, else its head."""
        found = await turns.session(org_id, session_id(session))
        turn_id = response_id or found.head_turn_id
        turn = await turns.get(org_id, turn_id) if turn_id else None
        if turn is None or turn.session_id != found.id:
            raise NotFound(f"session {found.id} has no response {response_id}")
        if turn.snapshot_id is None:
            raise NotFound(f"response {turn.id} has no saved files")
        return await stores.snapshots.manifest(org_id, turn.snapshot_id)

    @router.get("/v1/sessions")
    async def listing(principal: Developer, limit: int = 20, after: str | None = None) -> Any:
        if not 1 <= limit <= 100:
            raise Invalid("limit is 1 to 100", param="limit")
        found = await turns.sessions(principal.org_id, after=after, limit=limit + 1)
        shown = [_session(summary) for summary in found[:limit]]
        return {"object": "list", "data": shown, "has_more": len(found) > limit,
                "last_id": shown[-1]["id"] if shown else None}

    @router.get("/v1/sessions/{session}")
    async def one(session: str, principal: Developer) -> Any:
        return _session(await turns.session_summary(principal.org_id, session_id(session)))

    @router.get("/v1/sessions/{session}/turns")
    async def of(session: str, principal: Developer) -> Any:
        found = await turns.turns_of(principal.org_id, session_id(session))
        return {"object": "list", "data": [_turn(turn) for turn in found], "has_more": False,
                "last_id": found[-1].id if found else None}

    @router.get("/v1/sessions/{session}/files")
    async def files(session: str, principal: Developer, response_id: str | None = None) -> Any:
        manifest = await snapshot_of(principal.org_id, session, response_id)
        return {"object": "list", "has_more": False, "last_id": None, "data": [
            {"path": path, "bytes": entry.size, "modified_at": entry.mtime // 1_000_000_000}
            for path, entry in sorted(manifest.entries.items()) if entry.kind == "f"]}

    @router.get("/v1/sessions/{session}/files/content")
    async def content(session: str, path: str, principal: Developer, response_id: str | None = None) -> Any:
        manifest = await snapshot_of(principal.org_id, session, response_id)
        entry = manifest.entries.get(path)
        if entry is None or entry.kind != "f":
            raise NotFound(f"there is no file {path!r}: paths begin mind/ or work/")
        return StreamingResponse(stores.snapshots.content(principal.org_id, entry),
                                 media_type="application/octet-stream", headers={"Content-Length": str(entry.size)})

    @router.delete("/v1/sessions/{session}")
    async def delete(session: str, principal: Developer) -> Any:
        await platform.remove_session(principal.org_id, session_id(session))
        await platform.audit(principal, "session.delete", session)
        return {"id": session, "deleted": True}

    @router.get("/v1/responses/{turn_id}/trace")
    async def trace(turn_id: str, principal: Developer) -> Any:
        turn = await turns.get(principal.org_id, turn_id)
        steps = [("install and restore", turn.started_at, turn.restored_at), ("run", turn.ran_at, turn.exited_at),
                 ("save", turn.exited_at, turn.saved_at)]
        connections = await stores.egress.of(principal.org_id, turn.id)
        return {
            **_turn(turn), "session_id": turn.session_id, "code": turn.code, "message": turn.message,
            "digest": turn.digest, "image_id": turn.image_id, "api": turn.api, "handover": turn.handover,
            "snapshot_id": turn.snapshot_id, "command": turn.command, "log": turn.log_tail,
            "queued_for_s": (turn.started_at - turn.created_at).total_seconds() if turn.started_at else None,
            "steps": [{"name": name, "started_at": _time(start), "ended_at": _time(end)}
                      for name, start, end in steps if start is not None],
            "connections": [{"host": c.host, "port": c.port, "allowed": c.allowed, "reason": c.reason,
                             "bytes_up": c.bytes_up, "bytes_down": c.bytes_down, "started_at": _time(c.started_at),
                             "ended_at": _time(c.ended_at)} for c in connections],
        }

    return router


# ── keys, usage, audit, platform, organization ──────────────────────────────────────────────────────

def _key(key: Key) -> dict[str, Any]:
    return {"id": key.id, "name": key.name, "role": key.role, "created_at": _time(key.created_at),
            "last_used_at": _time(key.last_used_at), "revoked_at": _time(key.revoked_at)}


def _when(text: str | None, default: datetime, what: str) -> datetime:
    if text is None:
        return default
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Invalid(f"{what} is a time such as 2026-11-01T00:00:00Z", param=what) from None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def organization_router(platform: Platform, role: Role) -> APIRouter:
    router = APIRouter()
    stores = platform.stores
    Anyone = Annotated[Principal, Depends(role("runner"))]
    Developer = Annotated[Principal, Depends(role("developer"))]
    Admin = Annotated[Principal, Depends(role("admin"))]

    @router.post("/v1/keys")
    async def create_key(request: Request, principal: Admin) -> Any:
        body = _object(await read_json(request))
        key, text = await stores.keys.create(principal.org_id, name=body.get("name"), role=body.get("role"))
        await platform.audit(principal, "key.create", key.id, {"name": key.name, "role": key.role})
        return {**_key(key), "key": text}                       # the only time the key itself is shown

    @router.get("/v1/keys")
    async def keys(principal: Admin) -> Any:
        found = await stores.keys.all(principal.org_id)
        return {"object": "list", "data": [_key(key) for key in found], "has_more": False,
                "last_id": found[-1].id if found else None}

    @router.delete("/v1/keys/{key_id}")
    async def revoke(key_id: str, principal: Admin) -> Any:
        await stores.keys.revoke(principal.org_id, key_id)
        await platform.audit(principal, "key.revoke", key_id)
        return {"id": key_id, "revoked": True}

    @router.get("/v1/usage")
    async def usage(principal: Developer, start: str | None = None, end: str | None = None,
                    group_by: str = "harness") -> Any:
        now = datetime.now(UTC)
        since, until = _when(start, now - timedelta(days=30), "start"), _when(end, now, "end")
        rows = await stores.turns.usage(principal.org_id, start=since, end=until, group_by=group_by)
        return {"object": "list", "start": _time(since), "end": _time(until), "group_by": group_by, "data": rows,
                "has_more": False, "last_id": None}

    @router.get("/v1/audit")
    async def audit(principal: Admin, limit: int = 20, after: int | None = None) -> Any:
        if not 1 <= limit <= 100:
            raise Invalid("limit is 1 to 100", param="limit")
        found = await stores.audit.page(principal.org_id, after=after, limit=limit + 1)
        shown = [{"id": r.id, "at": _time(r.at), "key_id": r.key_id, "action": r.action, "target": r.target,
                  "detail": r.detail} for r in found[:limit]]
        return {"object": "list", "data": shown, "has_more": len(found) > limit,
                "last_id": shown[-1]["id"] if shown else None}

    @router.get("/v1/platform")
    async def about(principal: Anyone) -> Any:
        return {"version": __version__, "contract": CONTRACT_VERSION, "driver": platform.driver.name,
                "isolation": await platform.driver.isolation()}

    @router.get("/v1/organization")
    async def organization(principal: Admin) -> Any:
        org = await stores.orgs.get(principal.org_id)
        return {"id": org.id, "name": org.name, "retention_days": org.retention_days}

    @router.patch("/v1/organization")
    async def change(request: Request, principal: Admin) -> Any:
        body = _object(await read_json(request))
        unknown = sorted(set(body) - {"retention_days"})
        if unknown:
            raise Invalid(f"{unknown[0]} cannot be changed; what can: retention_days")
        org = await stores.orgs.set_retention(principal.org_id, body.get("retention_days"))
        await platform.audit(principal, "organization.update", org.id, {"retention_days": org.retention_days})
        return {"id": org.id, "name": org.name, "retention_days": org.retention_days}

    return router
