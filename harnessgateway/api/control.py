"""The organization's routes: who you are, members, invitations, keys, providers, the catalog and the
harnesses. Owners register providers, manage members and change harnesses; members read them and run
them. Each route validates, calls the domain and shapes the reply; nothing else."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..domain.members import Member
from ..errors import Invalid, NotFound
from .auth import author_of


class InviteBody(BaseModel):
    email: str
    role: str = "member"


class MemberBody(BaseModel):
    name: str
    role: str = "member"


class RoleBody(BaseModel):
    role: str


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
    message: str = ""


class CommitBody(BaseModel):
    """File changes as one commit: a path to text (or base64 with `encoding`) writes it, a path to
    null removes it. `parent` is the head the editor read; a stale parent is refused."""
    changes: dict[str, Optional[str]]
    message: str = ""
    parent: Optional[str] = None
    encoding: str = "text"


class OrgSettingsBody(BaseModel):
    title: Optional[str] = None


def router(service) -> APIRouter:
    principals, orgs, members, keys = service.principals, service.orgs, service.members, service.keys
    providers, harnesses, catalog, accounts = service.providers, service.harnesses, service.catalog, service.accounts
    r = APIRouter(prefix="/v1")
    member, owner = Depends(principals.member), Depends(principals.owner)

    @r.get("/me")
    def me(who: Member = member) -> dict:
        o = orgs.get(who.org)
        return {**asdict(who), "limits": o.limits, "org_status": o.status, "org_title": o.title}

    @r.get("/org")
    def get_org(who: Member = member) -> dict:
        return orgs.get(who.org).public()

    @r.patch("/org")
    def patch_org(body: OrgSettingsBody, who: Member = owner) -> dict:
        return orgs.update(who.org, title=body.title).public()

    # members and invitations
    @r.post("/members")
    def create_member(body: MemberBody, who: Member = owner) -> dict:
        return asdict(members.create(who.org, body.name, body.role))

    @r.get("/members")
    def list_members(who: Member = member) -> dict:
        return {"object": "list", "data": [asdict(m) for m in members.list(who.org)]}

    @r.patch("/members/{name}")
    def set_role(name: str, body: RoleBody, who: Member = owner) -> dict:
        return asdict(members.set_role(who.org, name, body.role))

    @r.delete("/members/{name}")
    def remove_member(name: str, who: Member = owner) -> dict:
        if name == who.name:
            raise Invalid("you cannot remove yourself; another owner does that")
        members.remove(who.org, name)
        keys.revoke_all(who.org, name)
        return {"deleted": name}

    @r.post("/invites")
    def invite(body: InviteBody, who: Member = owner) -> dict:
        return accounts.invite(who.org, body.email, body.role)

    @r.get("/invites")
    def list_invites(who: Member = owner) -> dict:
        return {"object": "list", "data": accounts.invites(who.org)}

    @r.delete("/invites/{email}")
    def revoke_invite(email: str, who: Member = owner) -> dict:
        accounts.revoke_invite(who.org, email)
        return {"deleted": email}

    # keys
    @r.post("/members/{name}/keys")
    def mint_key(name: str, body: KeyBody, who: Member = member) -> dict:
        if not (who.owner or who.name == name):
            raise HTTPException(403, "a member mints keys for themselves; an owner for anyone")
        key, secret = keys.mint(who.org, name, body.label)
        return {**asdict(key), "secret": secret}

    @r.get("/keys")
    def list_keys(who: Member = member) -> dict:
        return {"object": "list", "data": [asdict(k) for k in keys.list(who.org) if who.owner or k.member == who.name]}

    @r.delete("/keys/{key_id}")
    def revoke_key(key_id: str, who: Member = member) -> dict:
        mine = {k.id: k for k in keys.list(who.org)}
        if key_id not in mine or not (who.owner or mine[key_id].member == who.name):
            raise NotFound(f"no key {key_id}")
        return asdict(keys.revoke(who.org, key_id))

    # providers
    @r.put("/providers/{name}")
    def put_provider(name: str, body: ProviderBody, who: Member = owner) -> dict:
        return providers.put(who.org, name, body.url, body.api, body.key).public()

    @r.get("/providers")
    def list_providers(who: Member = member) -> dict:
        return {"object": "list", "data": [p.public() for p in providers.list(who.org)]}

    @r.get("/providers/{name}")
    def get_provider(name: str, who: Member = member) -> dict:
        return providers.get(who.org, name).public()

    @r.delete("/providers/{name}")
    def delete_provider(name: str, who: Member = owner) -> dict:
        providers.delete(who.org, name)
        return {"deleted": name}

    # the catalog: shipped packages, read-only
    @r.get("/catalog")
    def list_catalog(who: Member = member) -> dict:
        return {"object": "list", "data": [catalog.info(n) for n in catalog.names()]}

    @r.get("/catalog/{name}")
    def get_catalog(name: str, who: Member = member) -> dict:
        return {**catalog.info(name), "files": catalog.files(name)}

    @r.get("/catalog/{name}/files/{path:path}")
    def get_catalog_file(name: str, path: str, who: Member = member) -> Response:
        return Response(catalog.file(name, path), media_type="text/plain; charset=utf-8")

    # harnesses: repositories; every member runs them, owners change them
    def view(name: str, who: Member) -> dict[str, Any]:
        h = harnesses.get(who.org, name)
        return {**h.public(), "editable": who.owner, "files": [asdict(e) for e in harnesses.files(who.org, name)]}

    @r.post("/harnesses/{name}")
    async def create_harness(name: str, body: NewHarnessBody, request: Request, who: Member = owner) -> dict:
        """A new harness: from the catalog (`catalog`), from a harness of this organization (`fork`), or
        from an archive (`archive`, base64 of a tar)."""
        author = author_of(who, request)
        if sum(bool(x) for x in (body.catalog, body.fork, body.archive)) != 1:
            raise Invalid("name one source: catalog, fork or archive")
        if body.catalog:
            await asyncio.to_thread(catalog.fork, harnesses, who.org, body.catalog, name, author)
        elif body.fork:
            await asyncio.to_thread(harnesses.fork, who.org, body.fork, name, author)
        else:
            try:
                archive = base64.b64decode(body.archive, validate=True)
            except ValueError:
                raise Invalid("archive is base64 of a tar") from None
            await asyncio.to_thread(harnesses.create_from_archive, who.org, name, archive, author, "", body.message or "import")
        return view(name, who)

    @r.put("/harnesses/{name}")
    async def replace_harness(name: str, request: Request, who: Member = owner) -> dict:
        """The body is the whole package as a tar archive: the next commit, files not in it removed.
        A harness that does not exist yet is created."""
        archive = await request.body()
        if not archive:
            raise Invalid("send the package as a tar archive in the request body")
        author = author_of(who, request)
        message = request.headers.get("x-commit-message", "") or "push"
        try:
            harnesses.get(who.org, name)
        except NotFound:
            await asyncio.to_thread(harnesses.create_from_archive, who.org, name, archive, author, "", message)
            return view(name, who)
        await asyncio.to_thread(harnesses.replace, who.org, name, archive, message, author)
        return view(name, who)

    @r.get("/harnesses")
    def list_harnesses(who: Member = member) -> dict:
        return {"object": "list", "data": [{**h.public(), "editable": who.owner} for h in harnesses.list(who.org)]}

    @r.get("/harnesses/{name}")
    def get_harness(name: str, who: Member = member) -> dict:
        return view(name, who)

    @r.delete("/harnesses/{name}")
    def delete_harness(name: str, who: Member = owner) -> dict:
        harnesses.delete(who.org, name)
        return {"deleted": name}

    @r.get("/harnesses/{name}/files/{path:path}")
    def get_harness_file(name: str, path: str, at: str = "", who: Member = member) -> Response:
        return Response(harnesses.file(who.org, name, path, at or None), media_type="text/plain; charset=utf-8")

    @r.post("/harnesses/{name}/commits")
    def commit(name: str, body: CommitBody, request: Request, who: Member = owner) -> dict:
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
        harnesses.commit(who.org, name, changes, body.message or "edit", author_of(who, request), body.parent)
        return view(name, who)

    @r.get("/harnesses/{name}/commits")
    def commits(name: str, who: Member = member) -> dict:
        return {"object": "list", "data": [asdict(c) for c in harnesses.log(who.org, name)]}

    @r.get("/harnesses/{name}/archive")
    def harness_archive(name: str, at: str = "", who: Member = member) -> Response:
        return Response(harnesses.export(who.org, name, at or None), media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.tar.gz"'})

    return r
