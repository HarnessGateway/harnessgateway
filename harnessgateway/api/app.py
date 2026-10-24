"""Layer 6: the HTTP routes an organization uses. A member's key, or the app's session, names a member of
an organization; owners register providers, manage members and teams and every harness; members read
them, edit the harnesses of their teams, and make requests. `POST /v1/responses` is the OpenAI
Responses API: the body goes to the harness package whole. Sessions are read, followed, steered,
archived, and their workspaces browsed; harness packages are git repositories, read file by file and
committed to; rodeos run several harnesses on one task and keep the pick. The web surface is served
too: the home page at `/`, the app at `/app`, the documentation at `/docs`. Each route validates,
calls the layer below and shapes the reply; nothing else. A refusal is 400, 401, 403, 404 or 409 with
`{"detail": …}`; a malformed body 422."""
from __future__ import annotations

import asyncio
import base64
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import wire
from ..accounts import Accounts
from ..blobs import open_blobs
from ..catalog import Catalog
from ..errors import Conflict, Invalid, NotFound
from ..records import open_records
from ..secrets import Vault
from ..sessions import Record, Sessions
from ..store import Member, Store
from . import accounts as auth_routes
from . import deployer, internal
from .auth import Principals
from .config import Settings

STATUS = {Invalid: 400, NotFound: 404, Conflict: 409}
WEB = Path(__file__).resolve().parents[1] / "web"              # inside the wheel; a checkout has it at the repository root
if not WEB.is_dir():
    WEB = Path(__file__).resolve().parents[2] / "web"
DOCS = Path(__file__).resolve().parents[1] / "docs"            # the built documentation site (mkdocs build), when present
VERSION = version("harnessgateway")


class InviteBody(BaseModel):
    email: str
    role: str = "member"


class RodeoBody(BaseModel):
    task: str = ""
    entries: list[dict[str, Any]]


class WinnerBody(BaseModel):
    winner: str = ""


class MemberBody(BaseModel):
    name: str
    role: str = "member"


class RoleBody(BaseModel):
    role: str


class TeamBody(BaseModel):
    members: list[str] = []
    title: str = ""


class KeyBody(BaseModel):
    label: str = ""


class ProviderBody(BaseModel):
    url: str
    api: str
    key: str = ""


class NewHarnessBody(BaseModel):
    """A harness is born from the catalog, from another harness of the organization, or from an
    archive (base64 of a tar) in this body."""
    catalog: str = ""
    fork: str = ""
    archive: str = ""
    team: str = ""
    message: str = ""


class CommitBody(BaseModel):
    """File changes as one commit: a path to text (or base64 with `encoding`) writes it, a path to
    null removes it. `parent` is the head the editor read; a stale parent is refused."""
    changes: dict[str, Optional[str]]
    message: str = ""
    parent: Optional[str] = None
    encoding: str = "text"


class MoveBody(BaseModel):
    team: str = ""


class SteerBody(BaseModel):
    input: str


class Service:
    """The platform's parts, built from its settings; shared by every role in a process."""

    def __init__(self, settings: Settings, vault: Optional[Vault] = None):
        self.settings = settings
        self.vault = vault or Vault.from_env()
        self.records = open_records(settings.records_url)
        self.blobs = open_blobs(settings.blobs_url, base_url=settings.public_url, secret=self.vault.derive("blobs"))
        self.store = Store(self.records, self.blobs, self.vault, settings.data_dir / "git", settings.allow_private)
        self.sessions = Sessions(self.store, settings.public_url, settings.gateway_url)
        self.catalog = Catalog()
        self.accounts = Accounts(self.store)
        self.join_token = settings.join_token or secrets.token_urlsafe(32)   # an in-process worker joins with it
        self.principals = Principals(self.store, settings.deployer_key, self.join_token, self.accounts)


def author_of(who: Member, request: Request) -> str:
    """Who a commit is by: the account's name and email when the app is signed in, else the member."""
    acct = getattr(request.state, "account", None)
    return f"{acct.name} <{acct.email}>" if acct else who.name


def create_app(service: Service) -> FastAPI:
    settings, store, sessions, principals = service.settings, service.store, service.sessions, service.principals

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def sweeps() -> None:
            while True:
                await asyncio.sleep(15)
                try:
                    sessions.sweep_lost()
                except Exception:
                    pass
        task = asyncio.create_task(sweeps())
        yield
        task.cancel()

    app = FastAPI(title="HarnessGateway", version=VERSION, lifespan=lifespan, docs_url="/reference", redoc_url=None)

    for error, code in STATUS.items():
        @app.exception_handler(error)
        async def refused(_: Request, e: Exception, code: int = code) -> JSONResponse:
            return JSONResponse({"detail": str(e)}, status_code=code)

    member, owner = Depends(principals.member), Depends(principals.owner)

    def editor(who: Member, name: str) -> Member:
        """A member who may change the harness: an owner, or a member of its team; an organization-wide
        harness is an owner's to change."""
        h = store.harness(who.org, name)
        if not store.in_team(who, h.team):
            raise NotFound(f"no harness {name}")
        if not who.owner and not h.team:
            raise HTTPException(403, "an organization-wide harness is changed by an owner; team harnesses by their team")
        return who

    def viewer(who: Member, name: str) -> None:
        if not store.in_team(who, store.harness(who.org, name).team):
            raise NotFound(f"no harness {name}")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": VERSION, "contract": 7, "roles": list(settings.roles), "signup": settings.signup}

    @app.get("/v1/me")
    def me(who: Member = member) -> dict:
        o = store.org(who.org)
        return {**asdict(who), "limits": o.limits, "org_status": o.status, "org_title": o.title,
                "teams": [t.name for t in store.teams(who.org) if who.name in t.members]}

    # the organization: members, invitations, teams, keys
    @app.post("/v1/members")
    def create_member(body: MemberBody, who: Member = owner) -> dict:
        return asdict(store.create_member(who.org, body.name, body.role))

    @app.get("/v1/members")
    def list_members(who: Member = member) -> dict:
        return {"object": "list", "data": [asdict(m) for m in store.members(who.org)]}

    @app.patch("/v1/members/{name}")
    def set_role(name: str, body: RoleBody, who: Member = owner) -> dict:
        return asdict(store.set_role(who.org, name, body.role))

    @app.delete("/v1/members/{name}")
    def remove_member(name: str, who: Member = owner) -> dict:
        if name == who.name:
            raise Invalid("you cannot remove yourself; another owner does that")
        store.remove_member(who.org, name)
        return {"deleted": name}

    @app.post("/v1/invites")
    def invite(body: InviteBody, who: Member = owner) -> dict:
        return service.accounts.invite(who.org, body.email, body.role)

    @app.get("/v1/invites")
    def list_invites(who: Member = owner) -> dict:
        return {"object": "list", "data": service.accounts.invites(who.org)}

    @app.delete("/v1/invites/{email}")
    def revoke_invite(email: str, who: Member = owner) -> dict:
        service.accounts.revoke_invite(who.org, email)
        return {"deleted": email}

    @app.put("/v1/teams/{name}")
    def put_team(name: str, body: TeamBody, who: Member = owner) -> dict:
        return asdict(store.put_team(who.org, name, body.members, body.title))

    @app.get("/v1/teams")
    def list_teams(who: Member = member) -> dict:
        return {"object": "list", "data": [asdict(t) for t in store.teams(who.org)]}

    @app.get("/v1/teams/{name}")
    def get_team(name: str, who: Member = member) -> dict:
        return asdict(store.team(who.org, name))

    @app.delete("/v1/teams/{name}")
    def delete_team(name: str, who: Member = owner) -> dict:
        store.delete_team(who.org, name)
        return {"deleted": name}

    @app.post("/v1/members/{name}/keys")
    def mint_key(name: str, body: KeyBody, who: Member = member) -> dict:
        if not (who.owner or who.name == name):
            raise HTTPException(403, "a member mints keys for themselves; an owner for anyone")
        key, secret = store.mint_key(who.org, name, body.label)
        return {**asdict(key), "secret": secret}

    @app.get("/v1/keys")
    def list_keys(who: Member = member) -> dict:
        return {"object": "list", "data": [asdict(k) for k in store.keys(who.org) if who.owner or k.member == who.name]}

    @app.delete("/v1/keys/{key_id}")
    def revoke_key(key_id: str, who: Member = member) -> dict:
        keys = {k.id: k for k in store.keys(who.org)}
        if key_id not in keys or not (who.owner or keys[key_id].member == who.name):
            raise NotFound(f"no key {key_id}")
        return asdict(store.revoke_key(who.org, key_id))

    # providers
    @app.put("/v1/providers/{name}")
    def put_provider(name: str, body: ProviderBody, who: Member = owner) -> dict:
        return store.put_provider(who.org, name, body.url, body.api, body.key).public()

    @app.get("/v1/providers")
    def list_providers(who: Member = member) -> dict:
        return {"object": "list", "data": [p.public() for p in store.providers(who.org)]}

    @app.get("/v1/providers/{name}")
    def get_provider(name: str, who: Member = member) -> dict:
        return store.provider(who.org, name).public()

    @app.delete("/v1/providers/{name}")
    def delete_provider(name: str, who: Member = owner) -> dict:
        store.delete_provider(who.org, name)
        return {"deleted": name}

    # the catalog: shipped packages, read-only
    @app.get("/v1/catalog")
    def list_catalog(who: Member = member) -> dict:
        return {"object": "list", "data": [service.catalog.info(n) for n in service.catalog.names()]}

    @app.get("/v1/catalog/{name}")
    def get_catalog(name: str, who: Member = member) -> dict:
        return {**service.catalog.info(name), "files": service.catalog.files(name)}

    @app.get("/v1/catalog/{name}/files/{path:path}")
    def get_catalog_file(name: str, path: str, who: Member = member) -> Response:
        return Response(service.catalog.file(name, path), media_type="text/plain; charset=utf-8")

    # harnesses: git repositories
    def harness_view(name: str, who: Member) -> dict[str, Any]:
        h = store.harness(who.org, name)
        return {**h.public(), "editable": who.owner or (bool(h.team) and store.in_team(who, h.team)),
                "files": [asdict(e) for e in store.harness_files(who.org, name)]}

    @app.post("/v1/harnesses/{name}")
    async def create_harness(name: str, body: NewHarnessBody, request: Request, who: Member = member) -> dict:
        """A new harness: from the catalog (`catalog`), from a harness of this organization (`fork`), or
        from an archive (`archive`, base64 of a tar). A member may create a harness for a team they are
        in; an owner for the whole organization."""
        if not who.owner and (not body.team or not store.in_team(who, body.team)):
            raise HTTPException(403, "a member creates harnesses for their teams (name the team); an owner for the organization")
        author = author_of(who, request)
        if sum(bool(x) for x in (body.catalog, body.fork, body.archive)) != 1:
            raise Invalid("name one source: catalog, fork or archive")
        if body.catalog:
            await asyncio.to_thread(service.catalog.fork, store, who.org, body.catalog, name, author, body.team)
        elif body.fork:
            viewer(who, body.fork)
            await asyncio.to_thread(store.fork_harness, who.org, body.fork, name, author, body.team)
        else:
            try:
                archive = base64.b64decode(body.archive, validate=True)
            except ValueError:
                raise Invalid("archive is base64 of a tar") from None
            await asyncio.to_thread(store.create_harness_from_archive, who.org, name, archive, author, body.team, "", body.message or "import")
        return harness_view(name, who)

    @app.put("/v1/harnesses/{name}")
    async def replace_harness(name: str, request: Request, who: Member = member) -> dict:
        """The body is the whole package as a tar archive: the next commit, files not in it removed.
        A harness that does not exist yet is created (an owner's, or for `?team=`)."""
        team = request.query_params.get("team", "")
        archive = await request.body()
        if not archive:
            raise Invalid("send the package as a tar archive in the request body")
        author = author_of(who, request)
        message = request.headers.get("x-commit-message", "") or "push"
        try:
            store.harness(who.org, name)
        except NotFound:
            if not who.owner and (not team or not store.in_team(who, team)):
                raise HTTPException(403, "a member creates harnesses for their teams (?team=); an owner for the organization") from None
            await asyncio.to_thread(store.create_harness_from_archive, who.org, name, archive, author, team, "", message)
            return harness_view(name, who)
        editor(who, name)
        await asyncio.to_thread(store.replace_harness, who.org, name, archive, message, author)
        return harness_view(name, who)

    @app.get("/v1/harnesses")
    def list_harnesses(who: Member = member) -> dict:
        return {"object": "list", "data": [{**h.public(), "editable": who.owner or (bool(h.team) and store.in_team(who, h.team))}
                                           for h in store.visible_harnesses(who)]}

    @app.get("/v1/harnesses/{name}")
    def get_harness(name: str, who: Member = member) -> dict:
        viewer(who, name)
        return harness_view(name, who)

    @app.patch("/v1/harnesses/{name}")
    def move_harness(name: str, body: MoveBody, who: Member = owner) -> dict:
        store.move_harness(who.org, name, body.team)
        return harness_view(name, who)

    @app.delete("/v1/harnesses/{name}")
    def delete_harness(name: str, who: Member = member) -> dict:
        editor(who, name)
        store.delete_harness(who.org, name)
        return {"deleted": name}

    @app.get("/v1/harnesses/{name}/files/{path:path}")
    def get_harness_file(name: str, path: str, at: str = "", who: Member = member) -> Response:
        viewer(who, name)
        return Response(store.harness_file(who.org, name, path, at or None), media_type="text/plain; charset=utf-8")

    @app.post("/v1/harnesses/{name}/commits")
    def commit(name: str, body: CommitBody, request: Request, who: Member = member) -> dict:
        editor(who, name)
        if body.encoding not in ("text", "base64"):
            raise Invalid("encoding is text or base64")
        changes: dict[str, bytes | None] = {}
        for path, content in body.changes.items():
            if content is None:
                changes[path] = None
            elif body.encoding == "base64":
                try:
                    changes[path] = base64.b64decode(content, validate=True)
                except ValueError:
                    raise Invalid(f"{path}: not base64") from None
            else:
                changes[path] = content.encode()
        store.commit_harness(who.org, name, changes, body.message or "edit", author_of(who, request), body.parent)
        return harness_view(name, who)

    @app.get("/v1/harnesses/{name}/commits")
    def commits(name: str, who: Member = member) -> dict:
        viewer(who, name)
        return {"object": "list", "data": [asdict(c) for c in store.harness_log(who.org, name)]}

    @app.get("/v1/harnesses/{name}/archive")
    def harness_archive(name: str, at: str = "", who: Member = member) -> Response:
        viewer(who, name)
        return Response(store.harness_export(who.org, name, at or None), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.tar.gz"'})

    # responses: the OpenAI Responses API
    def streamed(who: Member, r: Record, *, owns: bool = False) -> StreamingResponse:
        """The turn's event stream. When the request `owns` the turn (it made it, in the foreground),
        the client closing the stream stops the turn gracefully: a request that is gone has no one
        waiting for it. A follower of a background turn closing its stream changes nothing."""
        async def ended() -> dict[str, Any]:
            return sessions.response_object(await sessions.wait(r.id))

        async def events() -> Any:
            try:
                async for chunk in wire.stream(sessions.response_object(r), sessions.follow(who, r.id), ended):
                    yield chunk
            finally:
                if owns:
                    sessions.disconnected(who, r.id)   # nothing when the turn has ended or runs in the background
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Session-Id": r.session})

    async def awaited(request: Request, who: Member, r: Record) -> Record:
        """The turn's end, unless the client stops waiting first: then the turn is stopped gracefully."""
        while not (r := sessions.response(who, r.id)).final:
            if await request.is_disconnected():
                return sessions.disconnected(who, r.id)
            await asyncio.sleep(0.25)
        return r

    @app.post("/v1/responses")
    async def create_response(request: Request, who: Member = member) -> Any:
        try:
            body = await request.json()
        except ValueError:
            raise Invalid("the request body is JSON") from None
        req = wire.Request.parse(body)
        r = await asyncio.to_thread(sessions.create, who, req)
        if req.stream:
            return streamed(who, r, owns=not req.background)
        if not req.background:
            r = await awaited(request, who, r)
        return JSONResponse(sessions.response_object(r), headers={"X-Session-Id": r.session})

    @app.get("/v1/responses/{rid}")
    def get_response(rid: str, stream: bool = False, who: Member = member) -> Any:
        r = sessions.response(who, rid)
        return streamed(who, r) if stream else sessions.response_object(r)

    @app.get("/v1/responses/{rid}/detail")
    def response_detail(rid: str, who: Member = member) -> dict:
        """The response plus what the package printed: its log lines, stderr, exit code, the items so far."""
        r = sessions.response(who, rid)
        log, stderr = sessions.live_log(r)
        return {**sessions.response_object(r), "output": sessions.live_items(r) if not r.final else r.output, "log": log, "stderr": stderr,
                "exit_code": r.exit_code, "worker": r.worker, "started": r.started, "ended": r.ended, "text": r.text, "status_raw": r.status}

    @app.get("/v1/responses/{rid}/request")
    def response_request(rid: str, who: Member = member) -> dict:
        return sessions.request_body(who, rid)

    @app.post("/v1/responses/{rid}/cancel")
    async def cancel_response(rid: str, who: Member = member) -> dict:
        r = sessions.cancel(who, rid)
        if not r.final:
            r = await sessions.wait(rid)
        return sessions.response_object(r)

    @app.get("/v1/responses/{rid}/captures")
    def list_captures(rid: str, who: Member = member) -> dict:
        return {"object": "list", "data": sessions.captures(who, rid)}

    @app.get("/v1/responses/{rid}/captures/{name:path}")
    def get_capture(rid: str, name: str, who: Member = member) -> Response:
        return Response(sessions.capture(who, rid, name), media_type="application/octet-stream")

    # sessions
    def session_view(s) -> dict[str, Any]:
        return {"id": s.id, "object": "session", "harness": s.harness, "version": s.version, "team": s.team, "member": s.member,
                "title": s.title, "created_at": int(s.created), "stored": s.stored, "status": "running" if s.running else "idle",
                "responses": list(s.responses), "workspace": bool(s.snapshot_key)}

    @app.get("/v1/sessions")
    def list_sessions(who: Member = member) -> dict:
        return {"object": "list", "data": [session_view(s) for s in sessions.sessions(who)]}

    @app.get("/v1/sessions/{sid}")
    def get_session(sid: str, who: Member = member) -> dict:
        s = sessions.session(who, sid)
        return {**session_view(s), "turns": [{**sessions.response_object(r), "text": r.text} for r in sessions.records_of(who, sid)]}

    @app.post("/v1/sessions/{sid}/interrupt")
    def interrupt(sid: str, body: SteerBody, who: Member = member) -> dict:
        return sessions.response_object(sessions.interrupt(who, sid, body.input))

    @app.get("/v1/sessions/{sid}/trace")
    def trace(sid: str, who: Member = member) -> dict:
        return {"object": "list", "data": sessions.trace(who, sid)}

    @app.get("/v1/sessions/{sid}/files")
    def list_files(sid: str, who: Member = member) -> dict:
        return {"object": "list", "data": sessions.files(who, sid)}

    @app.get("/v1/sessions/{sid}/files/{path:path}")
    def get_file(sid: str, path: str, who: Member = member) -> Response:
        return Response(sessions.file(who, sid, path), media_type="application/octet-stream")

    @app.get("/v1/sessions/{sid}/archive")
    def archive(sid: str, who: Member = member) -> Response:
        return Response(sessions.archive(who, sid), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{sid}.tar.gz"'})

    @app.delete("/v1/sessions/{sid}")
    def delete_session(sid: str, who: Member = member) -> dict:
        sessions.delete(who, sid)
        return {"deleted": sid}

    # rodeos
    @app.post("/v1/rodeos")
    def create_rodeo(body: RodeoBody, who: Member = member) -> dict:
        return sessions.create_rodeo(who, body.task, body.entries)

    @app.get("/v1/rodeos")
    def list_rodeos(who: Member = member) -> dict:
        return {"object": "list", "data": sessions.rodeos(who), "leaderboard": sessions.leaderboard(who)}

    @app.get("/v1/rodeos/{rid}")
    def get_rodeo(rid: str, who: Member = member) -> dict:
        return sessions.rodeo(who, rid)

    @app.patch("/v1/rodeos/{rid}")
    def pick_winner(rid: str, body: WinnerBody, who: Member = member) -> dict:
        return sessions.pick_winner(who, rid, body.winner)

    @app.delete("/v1/rodeos/{rid}")
    def delete_rodeo(rid: str, who: Member = member) -> dict:
        sessions.delete_rodeo(who, rid)
        return {"deleted": rid}

    app.include_router(auth_routes.router(store, service.accounts, principals, settings))
    app.include_router(deployer.router(store, sessions, principals))
    app.include_router(internal.router(store, sessions, principals, VERSION))

    if (WEB / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        def home() -> FileResponse:
            return FileResponse(WEB / "index.html")

        @app.get("/app", include_in_schema=False)
        def application() -> FileResponse:
            return FileResponse(WEB / "app.html")

        @app.get("/web/{path:path}", include_in_schema=False)
        def web_file(path: str) -> FileResponse:
            target = (WEB / path).resolve()
            if not target.is_file() or WEB not in target.parents:
                raise HTTPException(404)
            return FileResponse(target)

    if (DOCS / "index.html").is_file():
        app.mount("/docs", StaticFiles(directory=DOCS, html=True), name="docs")

    return app
