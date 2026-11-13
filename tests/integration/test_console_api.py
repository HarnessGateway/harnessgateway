"""Spec §14.2a, §14.2b, §13.11, §20: what the console needs of the API beyond the rest of it: a package's files
as installed, harness drafts, and links to a session's files that a browser can open without a key."""

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from harnessgateway.api.views import sign_view
from harnessgateway.domain.names import container_id
from harnessgateway.stores import Stores
from tests.integration.api import Running, running


@pytest.fixture
async def api(stores: Stores, org: str, tmp_path: Path) -> AsyncIterator[Running]:
    async with running(stores, org, tmp_path) as api:
        await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
        yield api


def _paths(listing: httpx.Response) -> list[str]:
    return [file["path"] for file in listing.json()["files"]]


# ── a package's files, as installed ─────────────────────────────────────────────────────────────────

async def test_an_installed_package_s_files_are_listed_and_read(api: Running) -> None:
    listed = (await api.developer.get("/v1/harnesses/shell/files")).json()
    by_path = {file["path"]: file for file in listed["data"]}
    assert {"harness.json", "install", "run"} <= set(by_path)
    assert (by_path["run"]["executable"], by_path["harness.json"]["executable"]) == (True, False)
    assert by_path["harness.json"]["bytes"] > 0

    manifest = await api.developer.get("/v1/harnesses/shell/files/harness.json")
    assert manifest.headers["content-type"].startswith("text/plain")
    assert json.loads(manifest.text)["name"] == "shell"
    assert (await api.developer.get("/v1/harnesses/shell/files/no/such")).status_code == 404
    assert (await api.developer.get("/v1/harnesses/absent/files")).status_code == 404
    assert (await api.runner.get("/v1/harnesses/shell/files")).status_code == 403


# ── drafts ──────────────────────────────────────────────────────────────────────────────────────────

async def test_a_draft_starts_from_the_template_under_the_name_it_is_given(api: Running) -> None:
    made = await api.developer.post("/v1/drafts", json={"name": "mine"})
    assert made.status_code == 200, made.text
    assert (made.json()["name"], made.json()["based_on"]) == ("mine", "the template")
    draft = await api.developer.get("/v1/drafts/mine")
    by_path = {file["path"]: file for file in draft.json()["files"]}
    assert {"harness.json", "install", "run"} <= set(by_path)
    assert not any(path.startswith("tests/") for path in by_path)        # the template's own tests stay behind
    assert (by_path["install"]["executable"], by_path["run"]["executable"]) == (True, True)
    manifest = await api.developer.get("/v1/drafts/mine/files/harness.json")
    assert json.loads(manifest.text)["name"] == "mine"

    listed = (await api.developer.get("/v1/drafts")).json()["data"]
    assert [(d["name"], d["based_on"], d["files"]) for d in listed] == [("mine", "the template", len(by_path))]
    again = await api.developer.post("/v1/drafts", json={"name": "mine"})
    assert (again.status_code, "already" in again.json()["error"]["message"]) == (409, True)
    assert (await api.developer.post("/v1/drafts", json={"name": "Not A Name"})).status_code == 400
    assert (await api.runner.get("/v1/drafts")).status_code == 403


async def test_a_draft_s_files_are_written_read_and_deleted(api: Running) -> None:
    await api.developer.post("/v1/drafts", json={"name": "mine"})
    put = await api.developer.put("/v1/drafts/mine/files/seeds/mind/NOTES.md", content=b"# notes\n")
    assert (put.status_code, put.json()) == (200, {"path": "seeds/mind/NOTES.md", "bytes": 8, "executable": False})
    assert (await api.developer.get("/v1/drafts/mine/files/seeds/mind/NOTES.md")).content == b"# notes\n"
    rewritten = await api.developer.put("/v1/drafts/mine/files/run", content=b"#!/bin/sh\necho hi\n")
    assert rewritten.json()["executable"] is True                         # the two executables stay executable
    binary = await api.developer.put("/v1/drafts/mine/files/logo.png", content=b"\x89PNG\x00\xff")
    assert binary.status_code == 200
    assert (await api.developer.get("/v1/drafts/mine/files/logo.png")).headers["content-type"] == (
        "application/octet-stream")

    for bad in ("..%2Foutside", "a//b", "kit/hgkit.py"):
        refused = await api.developer.put(f"/v1/drafts/mine/files/{bad}", content=b"x")
        assert refused.status_code == 400, bad
    too_big = await api.developer.put("/v1/drafts/mine/files/big", content=b"x" * (4 * 1024 * 1024 + 1))
    assert too_big.status_code == 413

    gone = await api.developer.delete("/v1/drafts/mine/files/seeds/mind/NOTES.md")
    assert gone.json() == {"path": "seeds/mind/NOTES.md", "deleted": True}
    assert "seeds/mind/NOTES.md" not in _paths(await api.developer.get("/v1/drafts/mine"))
    assert (await api.developer.get("/v1/drafts/mine/files/seeds/mind/NOTES.md")).status_code == 404
    assert (await api.developer.get("/v1/drafts/absent")).status_code == 404

    assert (await api.developer.delete("/v1/drafts/mine")).json() == {"name": "mine", "deleted": True}
    assert (await api.developer.get("/v1/drafts")).json()["data"] == []


async def test_installing_a_draft_makes_it_a_harness_and_the_draft_is_done(api: Running) -> None:
    await api.developer.post("/v1/drafts", json={"name": "mine"})
    installed = await api.developer.post("/v1/drafts/mine/install")
    assert installed.status_code == 200, installed.text
    assert installed.json()["name"] == "mine"
    assert (await api.developer.get("/v1/drafts")).json()["data"] == []
    assert (await api.openai.responses.create(model="mine", input="echo from the draft")).output_text == (
        "from the draft\n")
    audit = (await api.admin.get("/v1/audit")).json()["data"]
    assert ("harness.add", "mine", "draft:mine") in [(a["action"], a["target"], a["detail"].get("source"))
                                                    for a in audit]


async def test_a_draft_that_is_not_a_valid_package_is_refused_and_kept(api: Running) -> None:
    await api.developer.post("/v1/drafts", json={"name": "mine"})
    await api.developer.delete("/v1/drafts/mine/files/run")
    refused = await api.developer.post("/v1/drafts/mine/install")
    assert (refused.status_code, "no run" in refused.json()["error"]["message"]) == (400, True)
    assert [d["name"] for d in (await api.developer.get("/v1/drafts")).json()["data"]] == ["mine"]


async def test_an_installed_harness_is_opened_as_a_draft_and_installing_it_upgrades_the_harness(api: Running) -> None:
    made = await api.developer.post("/v1/drafts", json={"name": "shell", "harness": "shell"})
    assert (made.status_code, made.json()["based_on"]) == (200, "the installed harness shell")
    package = [f["path"] for f in (await api.developer.get("/v1/harnesses/shell/files")).json()["data"]]
    assert _paths(await api.developer.get("/v1/drafts/shell")) == package

    manifest = json.loads((await api.developer.get("/v1/drafts/shell/files/harness.json")).text)
    await api.developer.put("/v1/drafts/shell/files/harness.json",
                            content=json.dumps({**manifest, "version": "9.9.9"}).encode())
    upgraded = await api.developer.post("/v1/drafts/shell/install")
    assert (upgraded.json()["name"], upgraded.json()["version"]) == ("shell", "9.9.9")
    assert (await api.developer.post("/v1/drafts", json={"name": "x", "harness": "absent"})).status_code == 404


async def test_what_an_agent_wrote_in_a_session_becomes_a_draft(api: Running) -> None:
    manifest = '{"contract": 1, "name": "built", "version": "0.1.0", "apis": []}'
    built = await api.openai.responses.create(model="shell", input=(
        f"mkdir -p pkg/__pycache__ && cd pkg && echo '{manifest}' > harness.json && printf '#!/bin/sh\\n' > install "
        "&& cp $HG_HARNESS/run run && chmod +x install run && echo junk > __pycache__/x.pyc"))
    assert built.metadata is not None
    session = built.metadata["hg_session"]

    made = await api.developer.post("/v1/drafts", json={"name": "built", "session": session, "path": "pkg"})
    assert made.status_code == 200, made.text
    assert made.json()["based_on"] == f"session {session}, pkg/"
    assert _paths(await api.developer.get("/v1/drafts/built")) == ["harness.json", "install", "run"]
    assert (await api.developer.post("/v1/drafts/built/install")).json()["name"] == "built"

    nowhere = await api.developer.post("/v1/drafts", json={"name": "other", "session": session, "path": "nowhere"})
    assert (nowhere.status_code, "no folder 'nowhere'" in nowhere.json()["error"]["message"]) == (400, True)
    assert (await api.developer.get("/v1/drafts")).json()["data"] == []        # a draft that failed is not left behind


# ── links to a session's files ──────────────────────────────────────────────────────────────────────

async def _made(api: Running, command: str, **more: str) -> tuple[str, str]:
    """Run a command with the shell harness; the session's container id and the response's id."""
    response = await api.openai.responses.create(model="shell", input=command, **more)      # type: ignore[call-overload]
    assert response.status == "completed", response
    return container_id(response.metadata["hg_session"]), response.id


async def test_a_link_opens_a_session_s_files_in_a_browser_without_a_key(api: Running) -> None:
    container, first = await _made(api, "mkdir -p 'my site' && echo '<h1>hi</h1>' > 'my site/index.html' && "
                                        "printf 0123456789 > data.bin && echo 'a,b' > t.csv")
    link = await api.runner.post(f"/v1/containers/{container}/view")
    assert link.status_code == 200, link.text
    url = link.json()["url"]
    assert url.startswith("/view/")
    assert url.endswith("/")
    assert link.json()["expires_at"] > time.time() + 600

    async with httpx.AsyncClient(base_url=api.url) as anyone:              # no key: the link is the permission
        page = await anyone.get(f"{url}my%20site/index.html")
        assert (page.status_code, page.text) == (200, "<h1>hi</h1>\n")
        assert page.headers["content-type"] == "text/html; charset=utf-8"
        assert page.headers["content-security-policy"].startswith("sandbox ")   # what an agent made runs apart
        assert (page.headers["x-content-type-options"], page.headers["referrer-policy"]) == ("nosniff", "no-referrer")
        assert (await anyone.get(f"{url}t.csv")).headers["content-type"] == "text/csv; charset=utf-8"

        part = await anyone.get(f"{url}data.bin", headers={"Range": "bytes=2-5"})
        assert (part.status_code, part.content, part.headers["content-range"]) == (206, b"2345", "bytes 2-5/10")
        tail = await anyone.get(f"{url}data.bin", headers={"Range": "bytes=-3"})
        assert (tail.status_code, tail.content) == (206, b"789")
        beyond = await anyone.get(f"{url}data.bin", headers={"Range": "bytes=50-"})
        assert (beyond.status_code, beyond.headers["content-range"]) == (416, "bytes */10")

        saved = await anyone.get(f"{url}data.bin", params={"download": "1"})
        assert saved.headers["content-disposition"] == 'attachment; filename="data.bin"'
        assert (await anyone.get(f"{url}absent.txt")).status_code == 404
        assert (await anyone.get(f"{url}../mind/.note")).status_code == 404

        await _made(api, "printf changed > data.bin", previous_response_id=first)
        assert (await anyone.get(f"{url}data.bin")).content == b"changed"   # the link follows the session


async def test_a_link_that_was_changed_or_is_past_its_time_opens_nothing(api: Running) -> None:
    container, _ = await _made(api, "echo x > a.txt")
    url = (await api.runner.post(f"/v1/containers/{container}/view")).json()["url"]
    token = url.split("/")[2]
    session = f"sess_{container.partition('_')[2]}"
    old = sign_view(api.stores.settings.secret_key, api.org, session, expires_at=int(time.time()) - 1)
    async with httpx.AsyncClient(base_url=api.url) as anyone:
        assert (await anyone.get(f"/view/{token}/a.txt")).status_code == 200
        assert (await anyone.get(f"/view/{token[:-2]}zz/a.txt")).status_code == 404
        assert (await anyone.get(f"/view/{old}/a.txt")).status_code == 404
        assert (await anyone.get("/view/not-a-token/a.txt")).status_code == 404
    assert (await api.runner.post("/v1/containers/cntr_00000000000000000000000000/view")).status_code == 404
    async with httpx.AsyncClient(base_url=api.url) as anyone:
        assert (await anyone.post(f"/v1/containers/{container}/view")).status_code == 401


# ── what the session list shows ─────────────────────────────────────────────────────────────────────

async def test_a_session_says_what_its_latest_turn_ran_with_and_how_it_ended(api: Running) -> None:
    container, response = await _made(api, "echo one")
    [listed] = (await api.developer.get("/v1/sessions")).json()["data"]
    assert (listed["harness"], listed["model"], listed["status"], listed["container_id"]) == (
        "shell", None, "completed", container)
    assert listed["head_response_id"] == response


# ── the console's pages ─────────────────────────────────────────────────────────────────────────────

async def test_the_console_s_pages_say_what_may_run_in_them(api: Running) -> None:
    async with httpx.AsyncClient(base_url=api.url) as anyone:
        page = await anyone.get("/")
        script = await anyone.get("/app.js")
        chat = await anyone.get("/chat/index.html")
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert "content-security-policy" not in script.headers
    assert (chat.status_code, "script-src 'self'" in chat.headers["content-security-policy"]) == (200, True)
