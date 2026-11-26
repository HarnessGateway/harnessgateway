"""API keys: a key acts as whoever made it and never does more than they may. It may be narrowed, and given a time
from which it is refused; one that is limited makes no key. A caller is told exactly what they may do."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harnessgateway.domain.change import HOST
from harnessgateway.domain.principal import OWNER
from harnessgateway.platform import Platform
from harnessgateway.store.db import Database
from harnessgateway.testing import platform_on, public, run_next, settings_for
from tests.integration.conftest import LINE, ORG, SAMPLE, person, ready

SUBJECT = "hg-subject"
SOON = (datetime.now(UTC) + timedelta(days=30)).isoformat()


async def test_in_the_core_every_key_is_the_owners_so_what_one_started_is_theirs_with_any_other(platform: Platform):
    await ready(platform)
    made, first = await platform.keys.create(ORG, "first key", by=HOST)             # as `harnessgateway init` makes one
    assert (made.subject, made.scopes, made.expires_at) == (OWNER, [], None)
    async with public(platform, first) as a:
        second = (await a.post("/v1/keys", json={"name": "my app"})).json()
        assert second["subject"] == OWNER and second["key"].startswith("hg_") and second["object"] == "key"
        assert set(second) == {"id", "object", "name", "subject", "scopes", "created_at", "expires_at", "last_used_at", "revoked_at", "key"}
        async with public(platform, second["key"]) as b:
            assert (await a.get("/v1/platform")).headers[SUBJECT] == (await b.get("/v1/platform")).headers[SUBJECT] == OWNER
            sent = await a.post("/v1/responses", json={"model": LINE, "input": "hi", "background": True})
            assert await run_next(platform) is not None
            session = sent.json()["metadata"]["hg_session"]
            listed = (await b.get("/v1/sessions")).json()["data"]                     # the other key: the same person
            assert [(each["id"], each["owner"]) for each in listed] == [(session, OWNER)]
            assert [key["name"] for key in (await b.get("/v1/keys")).json()["data"]] == ["first key", "my app"]
            assert (await b.delete(f"/v1/keys/{made.id}")).json()["revoked"]          # theirs to revoke, with either
        assert (await a.get("/v1/platform")).status_code == 401
    told = await platform.db.all(None, "SELECT principal FROM turns")
    assert told == [{"principal": OWNER}]


async def test_a_key_is_refused_from_its_time_and_does_only_what_its_scopes_match(platform: Platform):
    await ready(platform)
    _, key = await platform.keys.create(ORG, "first key", by=HOST)
    async with public(platform, key) as owner:
        narrow = (await owner.post("/v1/keys", json={"name": "only responses", "scopes": ["responses.*", "sessions.read"], "expires_at": SOON})).json()
        assert narrow["scopes"] == ["responses.*", "sessions.read"] and narrow["expires_at"].startswith(SOON[:19])
        async with public(platform, narrow["key"]) as app:
            assert (await app.post("/v1/responses", json={"model": LINE, "input": "x", "background": True})).status_code == 200
            assert (await app.get("/v1/sessions")).status_code == 200
            refused = await app.put("/v1/providers/p", json={"apis": {"anthropic": "https://api.example.com"}})
            assert refused.status_code == 403 and refused.json()["error"]["message"] == (
                "this API key is limited to responses.*, sessions.read, which does not allow providers.write")
            assert (await app.get("/v1/providers")).status_code == 403 and (await app.delete(f"/v1/keys/{narrow['id']}")).status_code == 403
            said = (await app.get("/v1/permissions")).json()                         # asking what it may do is never limited
            assert (said["subject"], said["key"]) == (OWNER, narrow["id"])
            assert said["allows"] == ["me.read", "responses.read", "responses.write", "sessions.read"]
        await platform.db.run(None, "UPDATE keys SET expires_at = now() - interval '1 second' WHERE id = %s", [narrow["id"]])
        async with public(platform, narrow["key"]) as late:
            ended = await late.get("/v1/sessions")
            assert ended.status_code == 401 and ended.json()["error"]["code"] == "invalid_api_key"
        everything = (await owner.get("/v1/permissions")).json()["allows"]
        assert {"keys.write", "keys.read_all", "keys.write_all", "sessions.read_all", "sessions.write_all", "providers.write", "usage.read"} <= set(everything)
        assert everything == sorted(everything)
        for body, param in (({"name": "x", "scopes": ["everything"]}, "scopes"), ({"name": "x", "scopes": "responses.*"}, "scopes"),
                            ({"name": "x", "expires_at": "soon"}, "expires_at"), ({"name": "x", "expires_at": "2001-01-01T00:00:00Z"}, "expires_at"),
                            ({"name": "x", "role": "admin"}, "role"), ({"name": ""}, "name"), ({"scopes": []}, "name")):
            wrong = await owner.post("/v1/keys", json=body)
            assert wrong.status_code == 400 and wrong.json()["error"]["param"] == param, wrong.text


async def test_a_key_that_is_limited_makes_no_key(platform: Platform):
    _, key = await platform.keys.create(ORG, "first key", by=HOST)
    async with public(platform, key) as owner:
        scoped = (await owner.post("/v1/keys", json={"name": "keys only", "scopes": ["keys.*"]})).json()
        timed = (await owner.post("/v1/keys", json={"name": "for a month", "expires_at": SOON})).json()
        for limited in (scoped, timed):
            async with public(platform, limited["key"]) as caller:
                refused = await caller.post("/v1/keys", json={"name": "another"})
                assert refused.status_code == 403 and "a key that is limited makes no key" in refused.json()["error"]["message"], refused.text
                assert (await caller.get("/v1/keys")).status_code == 200
        assert len((await owner.get("/v1/keys")).json()["data"]) == 3
    assert not any("another" in str(row) for row in await platform.db.all(None, "SELECT name FROM keys"))


async def test_a_key_acts_as_whoever_made_it_and_theirs_are_anothers_only_where_everyones_are(db: Database, tmp_path: Path):
    platform = await platform_on(db, settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path, HG_EXTENSIONS=SAMPLE))
    await ready(platform)
    async with person(platform, "member-a") as a, person(platform, "member-b") as b, person(platform, "lead") as lead:
        hers = (await a.post("/v1/keys", json={"name": "a's"})).json()
        his = (await b.post("/v1/keys", json={"name": "b's"})).json()
        assert (hers["subject"], his["subject"]) == ("member-a", "member-b")
        async with public(platform, hers["key"]) as as_a:                             # her key is her: what it starts is hers,
            assert (await as_a.get("/v1/platform")).headers[SUBJECT] == "member-a"
            sent = await as_a.post("/v1/responses", json={"model": LINE, "input": "hi", "background": True})
            assert await run_next(platform) is not None
            session = sent.json()["metadata"]["hg_session"]
            assert (await a.get(f"/v1/sessions/{session}")).json()["owner"] == "member-a"
            assert (await b.get(f"/v1/sessions/{session}")).status_code == 403        # and her limits are its limits
            assert (await as_a.get("/v1/permissions")).json()["allows"] == (await a.get("/v1/permissions")).json()["allows"]
            assert "sessions.read_all" not in (await a.get("/v1/permissions")).json()["allows"]
        assert [key["id"] for key in (await a.get("/v1/keys")).json()["data"]] == [hers["id"]]       # each lists their own,
        assert [key["id"] for key in (await b.get("/v1/keys")).json()["data"]] == [his["id"]]
        refused = await b.delete(f"/v1/keys/{hers['id']}")                             # and revokes their own
        assert refused.status_code == 403 and "member-b reaches what is their own" in refused.json()["error"]["message"]
        assert {key["id"] for key in (await lead.get("/v1/keys")).json()["data"]} == {hers["id"], his["id"]}     # whoever may reach everyone's
        assert (await lead.delete(f"/v1/keys/{his['id']}")).json()["revoked"] and (await b.get("/v1/keys")).json()["data"][0]["revoked_at"]
        assert (await a.delete("/v1/keys/key_nothing")).status_code == 404
    audit = await platform.db.all(None, "SELECT name, target, by FROM sample_audit WHERE name LIKE 'keys.%%' ORDER BY id")
    assert [(row["name"], row["target"], row["by"]) for row in audit] == [
        ("keys.created", hers["id"], "member-a"), ("keys.created", his["id"], "member-b"), ("keys.revoked", his["id"], "lead")]
    async with platform.db.tx(ORG) as conn:                                           # what an edition does as it removes someone
        assert await platform.keys.revoke_of(conn, ORG, "member-a", by="lead") == 1 and await platform.keys.revoke_of(conn, ORG, "member-a", by="lead") == 0
    assert (await public(platform, hers["key"]).get("/v1/platform")).status_code == 401
