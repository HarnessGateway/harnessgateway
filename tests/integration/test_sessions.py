"""Whose a session is: whoever started it. Its owner reaches it; anyone else only when reaching everyone's is
allowed. The core alone has one person, its owner, and an edition with people decides who else may."""

import os
from pathlib import Path

import httpx

from harnessgateway.domain.principal import OWNER
from harnessgateway.platform import Platform
from harnessgateway.store.db import Database
from harnessgateway.testing import platform_on, public, run_next, settings_for
from tests.integration.conftest import BY, LINE, ORG, SAMPLE, person, ready

SUBJECT = "hg-subject"


async def turn(platform: Platform, who: httpx.AsyncClient, **request) -> dict:
    sent = await who.post("/v1/responses", json={"model": LINE, "input": "hi", "background": True, **request})
    assert sent.status_code == 200, sent.text
    assert await run_next(platform) is not None
    return (await who.get(f"/v1/responses/{sent.json()['id']}")).json()


async def test_with_the_core_alone_every_session_is_the_owners_and_each_answer_names_its_caller(platform: Platform):
    await ready(platform)
    (_, first), (_, second) = [await platform.keys.create(ORG, name, by=BY) for name in ("one", "two")]
    async with public(platform, first) as a, public(platform, second) as b:
        made = await turn(platform, a)
        session = made["metadata"]["hg_session"]
        assert (await a.get("/v1/platform")).headers[SUBJECT] == (await b.get("/v1/sessions")).headers[SUBJECT] == OWNER
        assert (await a.get(f"/v1/responses/{made['id']}?stream=true")).headers[SUBJECT] == OWNER    # a stream names them too
        assert SUBJECT not in (await public(platform, "hg_nobody").get("/v1/platform")).headers      # nobody is named nobody
        listed = (await b.get("/v1/sessions")).json()["data"]                                        # another key: the same person
        assert [(each["id"], each["owner"]) for each in listed if each["id"] == session] == [(session, OWNER)]
        again = await turn(platform, b, previous_response_id=made["id"])                             # who continues what is theirs
        assert again["metadata"]["hg_session"] == session and (await a.get(f"/v1/sessions/{session}")).json()["owner"] == OWNER


async def test_a_session_is_its_owners_and_anyone_else_reaches_it_only_when_everyones_are_theirs_to_reach(db: Database, tmp_path: Path):
    platform = await platform_on(db, settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path, HG_EXTENSIONS=SAMPLE))
    await ready(platform)
    async with person(platform, "member-a") as a, person(platform, "member-b") as b, person(platform, "lead") as lead:
        first = await turn(platform, a)
        second = await turn(platform, a, previous_response_id=first["id"])
        session = second["metadata"]["hg_session"]
        container = "cntr_" + session.removeprefix("sess_")
        waiting = (await a.post("/v1/responses", json={"model": LINE, "input": "x", "background": True, "previous_response_id": second["id"]})).json()

        # Its owner reaches all of it; the list shows them their own.
        assert [(each["id"], each["owner"]) for each in (await a.get("/v1/sessions")).json()["data"]] == [(session, "member-a")]
        for path in (f"/v1/sessions/{session}", f"/v1/sessions/{session}/turns", f"/v1/responses/{first['id']}",
                     f"/v1/responses/{first['id']}/input_items", f"/v1/containers/{container}", f"/v1/containers/{container}/files"):
            assert (await a.get(path)).status_code == 200, path
        assert (await a.post(f"/v1/containers/{container}/view")).status_code == 200

        # Someone else does not: not to read it, and not to change it. Their own list has none of it.
        assert (await b.get("/v1/sessions")).json()["data"] == []
        refusals = [await b.get(f"/v1/sessions/{session}"), await b.get(f"/v1/sessions/{session}/turns"),
                    await b.get(f"/v1/responses/{first['id']}"), await b.get(f"/v1/responses/{first['id']}?stream=true"),
                    await b.get(f"/v1/responses/{first['id']}/input_items"),
                    await b.get(f"/v1/containers/{container}"), await b.get(f"/v1/containers/{container}/files"),
                    await b.post(f"/v1/containers/{container}/view"),
                    await b.put("/v1/harnesses/taken", json={"session": session, "path": "package"}),
                    await b.post("/v1/responses", json={"model": LINE, "input": "x", "previous_response_id": second["id"]}),      # to continue it,
                    await b.post("/v1/responses", json={"model": LINE, "input": "x", "previous_response_id": first["id"]}),       # or to fork it
                    await b.post(f"/v1/responses/{waiting['id']}/cancel"), await b.delete(f"/v1/responses/{first['id']}"),
                    await b.delete(f"/v1/sessions/{session}")]
        for refused in refusals:
            assert refused.status_code == 403 and "member-b reaches what is their own" in refused.json()["error"]["message"], refused.text
        assert (await a.put("/v1/harnesses/taken", json={"session": session, "path": "package"})).status_code == 400      # the owner: only no such folder
        assert (await a.get(f"/v1/sessions/{session}")).json()["turns"] == 3 and (await b.get("/v1/sessions/sess_nothing")).status_code == 404
        assert await run_next(platform) is not None

        # Whoever may reach everyone's sees every session, each with whose it is; what they continue stays its
        # owner's, and what they fork is their own.
        mine = (await turn(platform, b))["metadata"]["hg_session"]
        assert {(each["id"], each["owner"]) for each in (await lead.get("/v1/sessions")).json()["data"]} == {(session, "member-a"), (mine, "member-b")}
        continued = await turn(platform, lead, previous_response_id=waiting["id"])
        assert continued["metadata"]["hg_session"] == session and (await lead.get(f"/v1/sessions/{session}")).json()["owner"] == "member-a"
        forked = (await turn(platform, lead, previous_response_id=first["id"]))["metadata"]["hg_session"]
        assert forked != session and (await lead.get(f"/v1/sessions/{forked}")).json()["owner"] == "lead"
        assert (await a.get(f"/v1/sessions/{forked}")).status_code == 403                    # and is not the first owner's to reach
        assert (await a.get(f"/v1/responses/{continued['id']}")).status_code == 200          # a turn another sent into theirs is theirs

        # A page of the list is a page of what the caller may read.
        page = (await lead.get("/v1/sessions?limit=2")).json()
        assert len(page["data"]) == 2 and page["has_more"] and len((await lead.get(f"/v1/sessions?after={page['last_id']}")).json()["data"]) == 1
        assert (await b.get("/v1/sessions?limit=1")).json()["has_more"] is False
        assert (await lead.delete(f"/v1/sessions/{mine}")).json()["deleted"] and (await b.get("/v1/sessions")).json()["data"] == []
