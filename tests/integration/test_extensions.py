"""The seams: an edition loaded the way an installation loads one, using every one of them."""

import asyncio
import os
from pathlib import Path

import httpx

from harnessgateway.api.app import public_app
from harnessgateway.extensions import Extensions
from harnessgateway.platform import Platform
from harnessgateway.runner.runner import Runner
from harnessgateway.store.db import SCHEMA, Database
from tests.helpers import FakeDriver, package
from tests.integration.conftest import LINE, ORG, settings_for
from tests.integration.edition import Edition, MemoryBlobs


async def test_an_edition_replaces_parts_is_asked_and_told_and_adds_its_own_without_touching_the_core(db: Database, tmp_path: Path):
    settings = settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path, HG_EXTENSIONS="tests.integration.edition:Edition",
                            HG_DRIVER="edition", HG_BLOBS="memory://anywhere")
    extensions = Extensions.load(settings.extensions)
    assert await db.migrate([SCHEMA, *extensions.migrations()]) == ["0001_core.sql", "0001_edition.sql"]
    platform = Platform.of(settings, db, extensions)
    await platform.orgs.ensure(ORG, "Tests")
    edition = extensions._all[0]
    assert isinstance(edition, Edition) and isinstance(platform.driver, FakeDriver)      # its driver runs the sandboxes
    running = asyncio.create_task(Runner(platform, heartbeat_s=0.2).run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(platform)), base_url="http://hg",
                                     timeout=30) as http:
            admin, reader = {"X-Edition-User": "admin"}, {"X-Edition-User": "reader"}
            assert (await http.get("/v1/platform")).status_code == 401                   # nobody is still nobody
            assert (await http.get("/v1/platform", headers=admin)).json()["driver"] == "fake"     # its way of signing in
            assert (await http.put("/v1/harnesses/echo", headers=admin, content=package())).status_code == 200
            assert (await http.put("/v1/providers/p", headers=admin, json={
                "apis": {"anthropic": "https://api.example.com"}, "key": "sk-real"})).status_code == 200
            sealed = (await platform.resources.get(ORG, "providers", "p")).sealed["key"]
            assert sealed == f"edition:{ORG}/providers/p/key:sk-real"                    # its sealer sealed it
            refused = await http.put("/v1/environments/default", headers=reader, json={"image": "node:22"})
            assert refused.status_code == 403 and "may read" in refused.json()["error"]["message"]    # its say on who may
            assert (await http.get("/v1/environments", headers=reader)).status_code == 200
            await http.put("/v1/environments/default", headers=admin, json={"image": "node:22", "harnesses": ["echo"]})
            over = await http.post("/v1/responses", headers={"X-Edition-User": "over-quota"}, json={"model": LINE, "input": "x"})
            assert over.status_code == 400 and "quota" in over.json()["error"]["message"]    # its say before a turn
            done = (await http.post("/v1/responses", headers=admin, json={"model": LINE, "input": "x"})).json()
            assert done["status"] == "completed"
            assert isinstance(edition.blobs, MemoryBlobs) and any("/chunks/" in key or "/snapshots/" in key for key in edition.blobs.kept)
            assert not (tmp_path / "blobs").exists()                                     # its store keeps the bytes
            names = [name for name, _ in edition.heard]
            assert names == ["harnesses.applied", "providers.applied", "environments.applied", "turn.finished"]
            finished = edition.heard[-1][1]                                              # it hears what happened
            assert finished["target"] == done["id"] and finished["status"] == "completed" and "cpu_seconds" in finished
            assert edition.heard[0][1]["by"] == "admin" and "sk-real" not in str(edition.heard)
            audit = (await http.get("/v1/audit", headers=reader)).json()["data"]         # its own route, on its own table
            assert [row["name"] for row in audit] == names
            assert 'id: "audit"' in (await http.get("/console/edition.js")).text         # its page in the console
            assert (await http.get("/console/app.js")).status_code == 200                # beside the core's own
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
