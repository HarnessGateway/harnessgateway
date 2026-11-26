"""Whose a file is: whoever uploaded it. They reach it; anyone else only when reaching everyone's is allowed. The core
alone has one person, its owner, and an edition with people decides who else may."""

import os
from pathlib import Path

import httpx

from harnessgateway.platform import Platform
from harnessgateway.store.db import Database
from harnessgateway.testing import platform_on, public, settings_for
from tests.integration.conftest import BY, LINE, ORG, SAMPLE, person, ready


async def upload(who: httpx.AsyncClient, name: str, content: bytes) -> str:
    made = await who.post("/v1/files", files={"file": (name, content)}, data={"purpose": "user_data"})
    assert made.status_code == 200, made.text
    return str(made.json()["id"])


def naming(file_id: str) -> dict:
    """A request whose input names an uploaded file."""
    return {"model": LINE, "background": True, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "read it"}, {"type": "input_file", "file_id": file_id}]}]}


async def test_with_the_core_alone_every_file_is_the_owners_whichever_key_uploaded_it(platform: Platform):
    await ready(platform)
    (_, first), (_, second) = [await platform.keys.create(ORG, name, by=BY) for name in ("one", "two")]
    async with public(platform, first) as a, public(platform, second) as b:
        file_id = await upload(a, "notes.txt", b"from one key")
        assert [file["id"] for file in (await b.get("/v1/files")).json()["data"]] == [file_id]      # another key: the same person
        assert (await b.get(f"/v1/files/{file_id}/content")).content == b"from one key"
        assert (await b.post("/v1/responses", json=naming(file_id))).status_code == 200
        assert (await b.delete(f"/v1/files/{file_id}")).json()["deleted"] is True
    assert await platform.db.all(None, "SELECT owner FROM files") == []


async def test_a_file_is_its_uploaders_and_anyone_else_reaches_it_only_when_everyones_are_theirs_to_reach(db: Database, tmp_path: Path):
    platform = await platform_on(db, settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path, HG_EXTENSIONS=SAMPLE))
    await ready(platform)
    async with person(platform, "member-a") as a, person(platform, "member-b") as b, person(platform, "lead") as lead:
        hers, his = await upload(a, "hers.txt", b"hers"), await upload(b, "his.txt", b"his")
        assert await platform.db.all(None, "SELECT id, owner FROM files ORDER BY id") == [
            {"id": hers, "owner": "member-a"}, {"id": his, "owner": "member-b"}]

        # Its uploader reaches it, and their list has their own and nobody else's.
        assert [file["id"] for file in (await a.get("/v1/files")).json()["data"]] == [hers]
        assert (await a.get(f"/v1/files/{hers}")).json()["filename"] == "hers.txt"
        assert (await a.get(f"/v1/files/{hers}/content")).content == b"hers"
        assert (await a.post("/v1/responses", json=naming(hers))).status_code == 200

        # Someone else does not: not to read it, not to give it to an agent, and not to delete it.
        refusals = [await b.get(f"/v1/files/{hers}"), await b.get(f"/v1/files/{hers}/content"),
                    await b.post("/v1/responses", json=naming(hers)), await b.delete(f"/v1/files/{hers}")]
        for refused in refusals:
            assert refused.status_code == 403 and "member-b reaches what is their own" in refused.json()["error"]["message"], refused.text
        assert (await b.get("/v1/files/file_nothing")).status_code == 404
        assert (await b.post("/v1/responses", json=naming("file_nothing"))).json()["error"]["param"] == "input"
        assert len(await platform.db.all(None, "SELECT id FROM turns")) == 1                # nothing of the refused requests was queued

        # Whoever may reach everyone's lists every file, reads any, and deletes any; a page is a page of what they may read.
        assert [file["id"] for file in (await lead.get("/v1/files")).json()["data"]] == [his, hers]      # newest first
        page = (await lead.get("/v1/files?limit=1")).json()
        assert [file["id"] for file in page["data"]] == [his] and page["has_more"]
        assert (await a.get("/v1/files?limit=1")).json()["has_more"] is False
        assert (await lead.get(f"/v1/files/{hers}/content")).content == b"hers"
        assert (await lead.post("/v1/responses", json=naming(hers))).status_code == 200
        assert (await lead.delete(f"/v1/files/{his}")).json()["deleted"] is True
        assert (await b.get("/v1/files")).json()["data"] == []
    audit = await platform.db.all(None, "SELECT name, target, by FROM sample_audit WHERE name LIKE 'files.%%' ORDER BY id")
    assert [(row["name"], row["target"], row["by"]) for row in audit] == [
        ("files.created", hers, "member-a"), ("files.created", his, "member-b"), ("files.deleted", his, "lead")]
