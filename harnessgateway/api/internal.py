"""The boxes' routes, the doors' and the platform scope's (SPEC §14.3). A box authenticates every call with the token
its launch gave it; a door with the door key; the platform scope is the operator key or a platform admin's sign-in,
and what it changes is audited."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from ..errors import Invalid
from .http import box, door, integer, only, operator, platform, read_json, strings
from .manage import period

router = APIRouter(prefix="/internal")


# ── a box's calls ─────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/boxes/{box_id}")
async def box_info(request: Request, b: dict[str, Any] = Depends(box)) -> Any:
    return await platform(request).boxes.info(b)


@router.post("/boxes/{box_id}/ready")
async def box_ready(request: Request, b: dict[str, Any] = Depends(box)) -> Any:
    await platform(request).boxes.ready(b)
    return {"ok": True}


@router.get("/boxes/{box_id}/next")
async def box_next(request: Request, wait: float = 0, b: dict[str, Any] = Depends(box)) -> Any:
    job = await platform(request).boxes.next(b, wait)
    return Response(status_code=204) if job is None else job


@router.post("/boxes/{box_id}/turns/{rid}/events")
async def box_events(request: Request, rid: str, b: dict[str, Any] = Depends(box)) -> Any:
    body = await read_json(request)
    return await platform(request).boxes.events(b, rid, integer(body, "batch", -1), strings(body, "lines"))


@router.post("/boxes/{box_id}/turns/{rid}/heartbeat")
async def box_heartbeat(request: Request, rid: str, b: dict[str, Any] = Depends(box)) -> Any:
    return await platform(request).boxes.heartbeat(b, rid)


@router.post("/boxes/{box_id}/turns/{rid}/finish")
async def box_finish(request: Request, rid: str, b: dict[str, Any] = Depends(box)) -> Any:
    return await platform(request).boxes.finish(b, rid, await read_json(request))


@router.put("/boxes/{box_id}/turns/{rid}/manifest")
async def box_manifest(request: Request, rid: str, b: dict[str, Any] = Depends(box)) -> Any:
    """The manifest of the state after the turn, as JSON Lines; its blobs were put first."""
    return await platform(request).boxes.put_snapshot(b, rid, (await request.body()).decode("utf-8", "replace"))


@router.post("/boxes/{box_id}/turns/{rid}/saved")
async def box_saved(request: Request, rid: str, b: dict[str, Any] = Depends(box)) -> Any:
    await platform(request).boxes.saved(b, rid, await read_json(request))
    return {"ok": True}


@router.put("/boxes/{box_id}/turns/{rid}/logs/{name}")
async def box_log(request: Request, rid: str, name: str, b: dict[str, Any] = Depends(box)) -> Any:
    await platform(request).boxes.put_log(b, rid, name, request.stream())
    return {"ok": True}


@router.get("/boxes/{box_id}/objects/{key:path}")
async def box_object(request: Request, key: str, b: dict[str, Any] = Depends(box)) -> Any:
    stream = await platform(request).boxes.object_stream(b, key)
    return StreamingResponse(stream, media_type="application/octet-stream")


@router.post("/boxes/{box_id}/blobs")
async def box_blobs_missing(request: Request, b: dict[str, Any] = Depends(box)) -> Any:
    body = await read_json(request)
    only(body, "hashes")
    return {"missing": await platform(request).boxes.blobs_missing(b, body.get("hashes"))}


@router.get("/boxes/{box_id}/blobs/{digest}")
async def box_blob_get(request: Request, digest: str, b: dict[str, Any] = Depends(box)) -> Any:
    return StreamingResponse(platform(request).boxes.blob_stream(b, digest), media_type="application/octet-stream")


@router.put("/boxes/{box_id}/blobs/{digest}")
async def box_blob_put(request: Request, digest: str, b: dict[str, Any] = Depends(box)) -> Any:
    return {"hash": digest, "bytes": await platform(request).boxes.put_blob(b, digest, request.stream())}


@router.put("/boxes/{box_id}/archive")
async def box_archive(request: Request, b: dict[str, Any] = Depends(box)) -> Any:
    if b["kind"] != "install":
        raise Invalid("only an install box uploads an archive")
    return {"bytes": await platform(request).boxes.put_archive(b, request.stream())}


@router.post("/boxes/{box_id}/installed")
async def box_installed(request: Request, b: dict[str, Any] = Depends(box)) -> Any:
    if b["kind"] != "install":
        raise Invalid("only an install box reports an install")
    await platform(request).boxes.installed(b, await read_json(request))
    return {"ok": True}


@router.post("/boxes/{box_id}/bye")
async def box_bye(request: Request, b: dict[str, Any] = Depends(box)) -> Any:
    await platform(request).boxes.bye(b)
    return {"ok": True}


# ── a door's questions ────────────────────────────────────────────────────────────────────────────────────────────
@router.post("/door/policy", dependencies=[Depends(door)])
async def door_policy(request: Request) -> Any:
    body = await read_json(request)
    only(body, "token")
    return await platform(request).boxes.policy(str(body.get("token", "")))


@router.post("/door/records", dependencies=[Depends(door)])
async def door_records(request: Request) -> Any:
    body = await read_json(request)
    only(body, "token", "records")
    records = body.get("records", [])
    if not isinstance(records, list):
        raise Invalid("records is a list", param="records")
    await platform(request).boxes.door_records(str(body.get("token", "")), records)
    return {"ok": True}


# ── the platform scope's view ─────────────────────────────────────────────────────────────────────────────────────
@router.get("/health", dependencies=[Depends(operator)])
async def health(request: Request) -> Any:
    return await platform(request).operations.health()


@router.get("/metrics", dependencies=[Depends(operator)])
async def metrics(request: Request) -> Any:
    return PlainTextResponse(await platform(request).operations.metrics(), media_type="text/plain; version=0.0.4")


@router.get("/boxes", dependencies=[Depends(operator)])
async def boxes(request: Request) -> Any:
    return {"object": "list", "data": await platform(request).boxes.listing()}


@router.post("/boxes/{box_id}/stop")
async def stop_box(request: Request, box_id: str, actor: str = Depends(operator)) -> Any:
    plat = platform(request)
    out = await plat.boxes.stop_requested(box_id, actor)
    await plat.audit.write(None, actor, "box.stop", box_id)
    return out


@router.get("/config", dependencies=[Depends(operator)])
async def config(request: Request) -> Any:
    return platform(request).operations.config()


@router.get("/versions", dependencies=[Depends(operator)])
async def versions(request: Request) -> Any:
    return await platform(request).operations.versions()


@router.get("/audit", dependencies=[Depends(operator)])
async def audit_all(request: Request, limit: int = 50, after: str | None = None) -> Any:
    return await platform(request).audit.list(None, limit, after)


@router.get("/usage", dependencies=[Depends(operator)])
async def usage_all(request: Request, start: int | None = None, end: int | None = None, group_by: str | None = None) -> Any:
    return await platform(request).usage.platform_summary(*period(start, end), group_by)


@router.get("/orgs", dependencies=[Depends(operator)])
async def orgs(request: Request) -> Any:
    return await platform(request).operations.list_orgs()


@router.get("/orgs/{org_id}", dependencies=[Depends(operator)])
async def org(request: Request, org_id: str) -> Any:
    return await platform(request).operations.org(org_id)


@router.patch("/orgs/{org_id}")
async def configure_org(request: Request, org_id: str, actor: str = Depends(operator)) -> Any:
    """{"limits": {...}, "status": "active|suspended", "egress": [...]}: each given part replaces the stored one."""
    plat, body = platform(request), await read_json(request)
    only(body, "limits", "status", "egress")
    if "limits" in body and not isinstance(body["limits"], dict):
        raise Invalid("limits is an object of limit fields", param="limits")
    out = await plat.operations.patch_org(org_id, body)
    await plat.audit.write(org_id, actor, "org.configure", org_id, body)
    return out


@router.post("/orgs/{org_id}/accounts")
async def invite_admin(request: Request, org_id: str, actor: str = Depends(operator)) -> Any:
    """{email, name}: an admin for the organization, invited (the link once; mailed when mail is set). How the
    deployer hands an organization to its people."""
    body = await read_json(request)
    only(body, "email", "name")
    return await platform(request).operations.invite_admin(org_id, actor, body.get("email"), body.get("name"))
