"""Drafts: a harness package written in the console, file by file, then installed (spec §14.3)."""

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from harnessgateway.api.management import Role, file_response, harness_shown, session_package
from harnessgateway.api.public import read_json
from harnessgateway.catalog import Catalog
from harnessgateway.domain.archive import MANIFEST_NAME, PackageFile, read_archive, write_archive
from harnessgateway.domain.names import DEFAULT_ENVIRONMENT, check_name
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Invalid, TooLarge
from harnessgateway.platform import Platform
from harnessgateway.store.drafts import FILE_MAX, Draft

TEMPLATE = "shell"                             # the catalog package a new harness starts as (spec §21)
_TEMPLATE_LEFT_OUT = "tests/"                  # its recorded cases are its own, not the new harness's


def _draft(draft: Draft) -> dict[str, Any]:
    return {"name": draft.name, "based_on": draft.based_on,
            "updated_at": draft.updated_at.isoformat().replace("+00:00", "Z"),
            "files": [{"path": file.path, "bytes": file.bytes, "executable": file.executable} for file in draft.files]}


def _renamed(files: list[PackageFile], name: str) -> list[PackageFile]:
    """The template's files, with the manifest naming the new harness."""
    renamed = []
    for file in files:
        if file.path == MANIFEST_NAME:
            manifest = {**json.loads(file.content), "name": name}
            file = PackageFile(file.path, file.executable, (json.dumps(manifest, indent=2) + "\n").encode())
        renamed.append(file)
    return renamed


def drafts_router(platform: Platform, role: Role) -> APIRouter:
    router = APIRouter()
    stores = platform.stores
    drafts = stores.drafts
    catalog = Catalog(Path(stores.settings.catalog))
    Developer = Annotated[Principal, Depends(role("developer"))]

    @router.post("/v1/drafts")
    async def create(request: Request, principal: Developer) -> Any:
        """Start a draft: from the template, from an installed harness, or from a folder of a session's work."""
        body = await read_json(request)
        if not isinstance(body, dict):
            raise Invalid("the request body is a JSON object")
        name, org_id = check_name(body.get("name"), "name"), principal.org_id
        if "harness" in body:
            package = await stores.harnesses.package(org_id, str(body["harness"]))
            files, based_on = list(package.files), f"the installed harness {body['harness']}"
        elif "session" in body:
            files = await session_package(stores, org_id, body["session"], body.get("path"))
            based_on = f"session {body['session']}, {str(body['path']).strip('/')}/"
        else:
            template = read_archive(catalog.archive(TEMPLATE))
            files = _renamed([f for f in template.files if not f.path.startswith(_TEMPLATE_LEFT_OUT)], name)
            based_on = "the template"
        return _draft(await drafts.create(org_id, name, based_on, files))

    @router.get("/v1/drafts")
    async def listing(principal: Developer) -> Any:
        found = await drafts.all(principal.org_id)
        return {"object": "list", "has_more": False, "last_id": found[-1].name if found else None, "data": [
            {**{k: v for k, v in _draft(draft).items() if k != "files"}, "files": len(draft.files)}
            for draft in found]}

    @router.get("/v1/drafts/{name}")
    async def one(name: str, principal: Developer) -> Any:
        return _draft(await drafts.get(principal.org_id, name))

    @router.delete("/v1/drafts/{name}")
    async def delete(name: str, principal: Developer) -> Any:
        await drafts.delete(principal.org_id, name)
        return {"name": name, "deleted": True}

    @router.get("/v1/drafts/{name}/files/{path:path}")
    async def read(name: str, path: str, principal: Developer) -> Response:
        return file_response(await drafts.read(principal.org_id, name, path))

    @router.put("/v1/drafts/{name}/files/{path:path}")
    async def write(name: str, path: str, request: Request, principal: Developer) -> Any:
        content = bytearray()
        async for piece in request.stream():
            content += piece
            if len(content) > FILE_MAX:
                raise TooLarge(f"a file written to a draft is at most {FILE_MAX >> 20} MiB")
        file = await drafts.write(principal.org_id, name, path, bytes(content))
        return {"path": file.path, "bytes": file.bytes, "executable": file.executable}

    @router.delete("/v1/drafts/{name}/files/{path:path}")
    async def remove(name: str, path: str, principal: Developer) -> Any:
        await drafts.remove_file(principal.org_id, name, path)
        return {"path": path, "deleted": True}

    @router.post("/v1/drafts/{name}/install")
    async def install(name: str, principal: Developer) -> Any:
        """Add the draft as a harness, as an upload of the same files would be. The draft is done then."""
        org_id = principal.org_id
        harness = await stores.harnesses.add(org_id, write_archive(await drafts.files(org_id, name)))
        await drafts.delete(org_id, name)
        await platform.audit(principal, "harness.add", harness.name,
                             {"version": harness.manifest.version, "digest": harness.digest, "source": f"draft:{name}"})
        platform.install_in_background(org_id, harness.name, DEFAULT_ENVIRONMENT)
        return await harness_shown(stores, org_id, harness)

    return router
