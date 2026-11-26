"""Changes: every one is told to the extensions inside the transaction that makes it, once, with who made it; and an
extension that cannot keep its record undoes the change."""

import os
from pathlib import Path

import httpx
import pytest

from harnessgateway.api.app import public_app
from harnessgateway.api.responses import accept
from harnessgateway.domain.change import HOST, PLATFORM
from harnessgateway.domain.principal import OWNER, Principal
from harnessgateway.errors import NotFound
from harnessgateway.platform import Platform
from harnessgateway.serve import init
from harnessgateway.store.db import Database
from harnessgateway.testing import FakeDriver, package, platform_on, settings_for
from tests.integration.conftest import LINE, ORG, SAMPLE


@pytest.fixture
async def recorded(db: Database, tmp_path: Path) -> Platform:
    """A platform whose sample edition keeps a row for each change, with the change's own connection."""
    return await platform_on(db, settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path, HG_EXTENSIONS=SAMPLE,
                                              HG_BLOBS="memory://anywhere"))


async def told(platform: Platform) -> list[tuple[str, str, str]]:
    rows = await platform.db.all(None, "SELECT name, target, by FROM sample_audit ORDER BY id")
    return [(row["name"], row["target"], row["by"]) for row in rows]


async def test_every_change_made_through_the_api_is_told_once_with_who_made_it_and_never_a_secret(recorded: Platform):
    platform = recorded
    _, key = await platform.keys.create(ORG, "the tests' key", by=HOST)
    first, used = (await platform.keys.all(ORG))[0].id, OWNER          # the key, and who it acts as
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(platform)), base_url="http://hg", timeout=30,
                                 headers={"Authorization": f"Bearer {key}"}) as http:
        assert (await http.put("/v1/harnesses/echo", content=package())).status_code == 200
        assert (await http.put("/v1/providers/acme", json={"apis": {"anthropic": "https://api.example.com"}, "key": "sk-real"})).status_code == 200
        assert (await http.put("/v1/environments/default", json={"image": "node:22", "harnesses": ["echo"], "secrets": {
            "TOKEN": {"hosts": ["api.example.com"], "value": "env-secret"}}})).status_code == 200
        made = (await http.post("/v1/keys", json={"name": "for the pipeline"})).json()
        uploaded = (await http.post("/v1/files", files={"file": ("notes.txt", b"words")}, data={"purpose": "user_data"})).json()
        queued = (await http.post("/v1/responses", json={"model": LINE, "input": "x", "background": True})).json()
        session = queued["metadata"]["hg_session"]
        assert (await http.post(f"/v1/responses/{queued['id']}/cancel")).json()["status"] == "cancelled"
        assert (await http.post(f"/v1/responses/{queued['id']}/cancel")).status_code == 200           # already ended: no change
        assert (await http.delete(f"/v1/responses/{queued['id']}")).status_code == 200
        assert (await http.delete(f"/v1/sessions/{session}")).status_code == 200
        assert (await http.delete(f"/v1/files/{uploaded['id']}")).status_code == 200
        assert (await http.delete(f"/v1/keys/{made['id']}")).status_code == 200
        assert (await http.delete("/v1/providers/acme")).status_code == 200
        assert (await http.put("/v1/providers/bad", json={"apis": {}})).status_code == 400            # refused: nothing changed
        assert (await http.delete("/v1/providers/acme")).status_code == 404
        assert (await http.get("/v1/providers")).status_code == 200                                   # read: nothing changed
    assert await told(platform) == [
        ("keys.created", first, HOST), ("harnesses.applied", "echo", used), ("providers.applied", "acme", used),
        ("environments.applied", "default", used), ("keys.created", made["id"], used), ("files.created", uploaded["id"], used),
        ("responses.created", queued["id"], used), ("responses.cancelled", queued["id"], used),
        ("responses.deleted", queued["id"], used), ("sessions.deleted", session, used), ("files.deleted", uploaded["id"], used),
        ("keys.revoked", made["id"], used), ("providers.deleted", "acme", used)]
    everything = str(await platform.db.all(None, "SELECT * FROM sample_audit"))
    assert not any(secret in everything for secret in ("sk-real", "env-secret", key, made["key"]))


async def test_an_extension_that_cannot_keep_its_record_undoes_the_change_and_refuses_the_request(recorded: Platform):
    platform = recorded
    _, key = await platform.keys.create(ORG, "the tests' key", by=HOST)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(platform)), base_url="http://hg", timeout=30,
                                 headers={"Authorization": f"Bearer {key}"}) as http:
        refused = await http.put("/v1/providers/unrecorded", json={"apis": {"anthropic": "https://api.example.com"}})
        assert refused.status_code == 403 and "could not record this change" in refused.json()["error"]["message"]
        assert (await http.get("/v1/providers/unrecorded")).status_code == 404                         # neither is kept
        assert (await http.put("/v1/harnesses/unrecorded", content=package())).status_code == 403
        assert (await http.get("/v1/harnesses")).json()["data"] == []
    assert [name for name, _, _ in await told(platform)] == ["keys.created"]


async def test_what_the_platform_does_for_itself_is_told_too_and_a_turn_that_was_not_to_be_stored_is_no_change(db: Database, tmp_path: Path):
    settings = settings_for(os.environ["HG_TEST_DATABASE_URL"], tmp_path, HG_EXTENSIONS=SAMPLE, HG_BLOBS="memory://anywhere",
                            HG_DRIVER="sample")
    assert (await init(settings)) is not None                                            # as `harnessgateway init` does
    platform = await platform_on(db, settings)
    first = (await platform.keys.all(ORG))[0].id
    assert await told(platform) == [
        ("harnesses.applied", "harness-package-builder", HOST), ("harnesses.applied", "shell", HOST),
        ("environments.applied", "default", HOST), ("keys.created", first, HOST)]
    from harnessgateway import defaults
    await platform.resources.add_package(ORG, "shell", package(), by=first)             # as an earlier version left it
    await defaults.packages(platform, ORG, by=PLATFORM)                                  # as every start does
    assert (await told(platform))[-2:] == [("harnesses.applied", "shell", first), ("harnesses.applied", "shell", PLATFORM)]
    before = len(await told(platform))
    assert isinstance(platform.driver, FakeDriver)
    from harnessgateway.runner.turn import TurnRunner
    unkept = await accept(platform, Principal(ORG, first), {"model": "default/shell/none/none", "input": "x", "store": False})
    await TurnRunner(platform).run(await platform.turns.claim("runner-tests"))
    await platform.forget(ORG, unkept.id)                                                # it leaves nothing, and no record:
    with pytest.raises(NotFound):
        await platform.sessions.get(ORG, unkept.session_id)
    assert len(await told(platform)) == before                                           # it was nobody's change
