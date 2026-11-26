"""The seams: an edition loaded the way an installation loads one, using every one of them; and a second extension
loaded after it, which adds its own and takes the place of nothing."""

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from harnessgateway.api.app import public_app
from harnessgateway.platform import Platform
from harnessgateway.runner.housekeeping import housekeep
from harnessgateway.runner.runner import Runner
from harnessgateway.settings import SettingsError
from harnessgateway.store.db import Database
from harnessgateway.testing import FakeDriver, package, platform_on, settings_for
from tests.integration.conftest import LINE, ORG
from tests.integration.edition import DENIED, Edition, MemoryBlobs
from tests.integration.second import DENIED as DENIED_TOO
from tests.integration.second import Second

SAMPLE, SECOND = "tests.integration.edition:Edition", "tests.integration.second:Second"


async def loaded(db: Database, data: Path, *extensions: str, **more: str) -> Platform:
    return await platform_on(db, settings_for(os.environ["HG_TEST_DATABASE_URL"], data, HG_EXTENSIONS=",".join(extensions), **more))


async def test_an_edition_replaces_parts_is_asked_and_told_and_adds_its_own_without_touching_the_core(db: Database, tmp_path: Path):
    platform = await loaded(db, tmp_path, SAMPLE, HG_DRIVER="sample", HG_BLOBS="memory://anywhere", HG_SECRET_KEY="")
    edition = platform.extensions._all[0]
    assert isinstance(edition, Edition) and isinstance(platform.driver, FakeDriver)      # its driver runs the sandboxes
    assert edition.platform is platform                                                  # it was handed the platform
    assert await platform.db.all(None, "SELECT owner, name FROM migrations ORDER BY owner, name") == [
        {"owner": "core", "name": "0001_core.sql"}, {"owner": "core", "name": "0002_keys.sql"},
        {"owner": "core", "name": "0003_files.sql"}, {"owner": "sample", "name": "0001_edition.sql"}]
    running = asyncio.create_task(Runner(platform, heartbeat_s=0.2).run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(platform)), base_url="http://hg",
                                     timeout=30) as http:
            admin, reader = {"X-Sample-User": f"{ORG}/admin"}, {"X-Sample-User": f"{ORG}/reader"}
            assert (await http.get("/v1/platform")).status_code == 401                   # nobody is still nobody
            about = (await http.get("/v1/platform", headers=admin)).json()               # its way of signing in
            assert (about["driver"], about["extensions"]) == ("fake", [{"name": "sample", "version": "0.1.0"}])
            assert (await http.put("/v1/harnesses/echo", headers=admin, content=package())).status_code == 200
            assert (await http.put("/v1/providers/p", headers=admin, json={
                "apis": {"anthropic": "https://api.example.com"}, "key": "sk-real"})).status_code == 200
            sealed = (await platform.resources.get(ORG, "providers", "p")).sealed["key"]
            assert sealed == f"sample:{ORG}/providers/p/key:sk-real"                     # its sealer sealed it, with no HG_SECRET_KEY
            refused = await http.put("/v1/environments/default", headers=reader, json={"image": "node:22"})
            assert refused.status_code == 403 and "may read" in refused.json()["error"]["message"]    # its say on who may
            assert (await http.get("/v1/environments", headers=reader)).status_code == 200
            await http.put("/v1/environments/default", headers=admin, json={"image": "node:22", "harnesses": ["echo"]})
            over = await http.post("/v1/responses", headers={"X-Sample-User": f"{ORG}/over-quota"}, json={"model": LINE, "input": "x"})
            assert over.status_code == 400 and "quota" in over.json()["error"]["message"]    # its say before a turn
            done = (await http.post("/v1/responses", headers=admin, json={"model": LINE, "input": "x"})).json()
            assert done["status"] == "completed"
            assert isinstance(edition.blobs, MemoryBlobs) and any("/chunks/" in key or "/snapshots/" in key for key in edition.blobs.kept)
            assert not (tmp_path / "blobs").exists()                                     # its store keeps the bytes
            plan = platform.driver.plans[-1]                                              # its narrowing of the network
            token = plan.env["HTTPS_PROXY"].split("@")[0].rsplit(":", 1)[1]
            assert await platform.grants.policy(done["id"], token) is None               # (the turn is over: its grant is gone)
            name, finished = edition.heard[-1]                                           # it hears what happened by itself
            assert name == "turn.finished" and finished["target"] == done["id"] and finished["by"] == "admin"
            assert finished["status"] == "completed" and finished["usage"]["total_tokens"] == 5
            assert finished["cpus"] == 2 and finished["memory_mb"] == 4096 and finished["seconds"] >= 0 and "cpu_seconds" in finished
            audit = (await http.get("/v1/audit", headers=reader)).json()["data"]         # its own route, on its own table:
            assert [(row["name"], row["target"], row["by"]) for row in audit] == [       # every change, with who made it
                ("harnesses.applied", "echo", "admin"), ("providers.applied", "p", "admin"),
                ("environments.applied", "default", "admin"), ("responses.created", done["id"], "admin")]
            assert "sk-real" not in str(audit) and "sk-real" not in str(edition.heard)
            assert (await http.get("/console/extensions.json")).json() == ["sample"]     # its pages in the console,
            assert 'id: "audit"' in (await http.get("/console/x/sample/edition.js")).text    # under its own name
            assert "import { editions }" in (await http.get("/console/app.js")).text     # beside the core's own
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    await housekeep(platform, elapsed_s=7.0)                                             # its turn in every round
    assert edition.rounds == [7.0]


async def test_a_second_extension_adds_its_own_beside_the_firsts_and_one_that_fails_fails_nothing_else(db: Database, tmp_path: Path):
    platform = await loaded(db, tmp_path, SAMPLE, SECOND, HG_BLOBS="memory://anywhere")
    first, second = platform.extensions._all
    assert isinstance(first, Edition) and isinstance(second, Second) and second.started_after == ["sample", "second"]
    assert await platform.db.all(None, "SELECT owner, name FROM migrations ORDER BY owner, name") == [   # two files of one name
        {"owner": "core", "name": "0001_core.sql"}, {"owner": "core", "name": "0002_keys.sql"},
        {"owner": "core", "name": "0003_files.sql"}, {"owner": "sample", "name": "0001_edition.sql"},
        {"owner": "second", "name": "0001_edition.sql"}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(platform)), base_url="http://hg", timeout=30) as http:
        admin = {"X-Sample-User": f"{ORG}/admin"}
        assert [each["name"] for each in (await http.get("/v1/platform", headers=admin)).json()["extensions"]] == ["sample", "second"]
        assert (await http.put("/v1/harnesses/echo", headers=admin, content=package())).status_code == 200
        assert (await http.get("/v1/audit", headers=admin)).json()["data"][0]["name"] == "harnesses.applied"     # the first's route,
        assert (await http.get("/v1/notes", headers=admin)).json()["data"] == ["harnesses.applied"]              # and the second's
        assert (await http.get("/v1/notes", headers={"X-Sample-User": f"{ORG}/stranger"})).status_code == 403    # both are asked who may:
        assert (await http.put("/v1/harnesses/more", headers={"X-Sample-User": f"{ORG}/reader"}, content=package())).status_code == 403
        assert (await http.get("/console/extensions.json")).json() == ["sample", "second"]                       # both add to the console,
        pages = [(await http.get(f"/console/x/{name}/edition.js")).text for name in ("sample", "second")]
        assert "sample edition" in pages[0] and 'id: "notes"' in pages[1]                                       # each under its own name
        assert (await http.get("/console/x/nobody/edition.js")).status_code == 404
        assert (await http.get("/console/x/second/../../cli.py")).status_code == 404
        assert (await http.get("/console/edition.js")).status_code == 404        # and no file of theirs is the console's
    first.failing = {"on_event", "housekeep"}                                    # the first fails: the second still hears,
    await platform.announce("turn.finished", ORG, target="resp_x")
    await housekeep(platform, elapsed_s=3.0)                                     # and still has its round
    assert first.heard == [] and second.heard == ["turn.finished"] and (first.rounds, second.rounds) == ([], [3.0])


async def test_each_extension_narrows_what_a_sandbox_may_reach_and_none_may_widen_it(db: Database, tmp_path: Path):
    from harnessgateway.domain.egress import EgressPolicy
    platform = await loaded(db, tmp_path, SAMPLE, SECOND, HG_BLOBS="memory://anywhere")
    environment = EgressPolicy("limited", ("api.example.com",), ("bad.example.com",))
    narrowed = await platform.extensions.egress(ORG, environment)
    assert narrowed == EgressPolicy("limited", ("api.example.com",), ("bad.example.com", DENIED, DENIED_TOO))
    assert narrowed.decide(DENIED, 443) and narrowed.decide(DENIED_TOO, 443) and not narrowed.decide("api.example.com", 443)
    platform.extensions._all[0].failing = {"egress"}
    with pytest.raises(ValueError, match="sample widened what a sandbox may reach"):
        await platform.extensions.egress(ORG, environment)


async def test_an_extension_that_would_take_the_place_of_a_route_or_a_name_is_refused_at_start(db: Database, tmp_path: Path):
    platform = await loaded(db, tmp_path, "tests.integration.second:Clashing")
    with pytest.raises(SettingsError, match="two routes answer GET /v1/keys"):
        public_app(platform)
    for names, why in (((SAMPLE, SAMPLE), "the name 'sample' is taken"), (("tests.integration.edition:MemoryBlobs",), "is not an Extension"),
                       (("tests.integration.nothing:Here",), "could not be loaded"), (("tests.integration.edition:Nothing",), "could not be loaded")):
        with pytest.raises(SettingsError, match=why):
            await loaded(db, tmp_path, *names, HG_BLOBS="memory://anywhere")
    with pytest.raises(SettingsError, match="HG_SECRET_KEY is 32 random bytes"):          # no sealer of an extension's, and no key
        await loaded(db, tmp_path, SECOND, HG_SECRET_KEY="")
