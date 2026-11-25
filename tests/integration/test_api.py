"""The public API, over HTTP, with a runner running beside it."""

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from harnessgateway.api.app import public_app
from harnessgateway.domain.package import read_archive
from harnessgateway.platform import Platform
from harnessgateway.runner.runner import Runner
from harnessgateway.testing import extensions_under_test, package
from tests.integration.conftest import BY, LINE, ORG


@pytest.fixture
async def http(platform: Platform) -> AsyncIterator[httpx.AsyncClient]:
    running = asyncio.create_task(Runner(platform, heartbeat_s=0.2).run())
    _, key = await platform.keys.create(ORG, "tests", by=BY)
    transport = httpx.ASGITransport(app=public_app(platform))
    async with httpx.AsyncClient(transport=transport, base_url="http://hg", timeout=30,
                                 headers={"Authorization": f"Bearer {key}"}) as client:
        yield client
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


async def setup(http: httpx.AsyncClient) -> None:
    assert (await http.put("/v1/harnesses/echo", content=package())).status_code == 200
    assert (await http.put("/v1/environments/default", json={"image": "node:22", "harnesses": ["echo"]})).status_code == 200


async def test_every_route_needs_a_key_and_a_refusal_is_the_openai_error_envelope(http: httpx.AsyncClient):
    for headers in ({"Authorization": "Bearer hg_wrong"}, {"Authorization": ""}):
        refused = await http.get("/v1/platform", headers=headers)
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "invalid_api_key"
    assert (await http.get("/healthz", headers={"Authorization": ""})).json() == {"status": "ok"}
    assert (await http.get("/readyz", headers={"Authorization": ""})).json() == {"status": "ready"}
    about = (await http.get("/v1/platform")).json()
    assert about["isolation"] == "none" and len(about["extensions"]) == len(extensions_under_test())      # none, for the core alone
    wrong = (await http.post("/v1/responses", json={"model": LINE, "input": "x", "temperatur": 1})).json()["error"]
    assert (wrong["type"], wrong["param"]) == ("invalid_request_error", "temperatur")
    assert (await http.post("/v1/responses", content=b"not json")).status_code == 400
    assert (await http.get("/v1/widgets")).status_code == 404 and (await http.get("/v1/responses/nope")).status_code == 404


async def test_definitions_are_made_read_replaced_and_deleted(http: httpx.AsyncClient):
    added = await http.put("/v1/harnesses/echo", content=package(**{"README.md": b"mine"}))
    assert added.status_code == 200 and set(added.json()) == {"name", "digest", "secrets_set", "created_at", "updated_at"}
    refused = await http.put("/v1/harnesses/echo", json={"settings": {"MODE": "fast"}})        # a harness is its package:
    assert refused.status_code == 400 and "a harness is its package" in refused.json()["error"]["message"]      # nothing is set on it
    exported = await http.get("/v1/harnesses/echo/package")
    assert read_archive(exported.content).digest == added.json()["digest"] and "echo.tar.gz" in exported.headers["content-disposition"]
    assert (await http.put("/v1/harnesses/echo", content=b"\x1f\x8bnot an archive")).status_code == 400
    made = await http.put("/v1/environments/default", json={"image": "node:22", "harnesses": ["echo"]})
    assert made.json()["network"] == {"mode": "limited", "allow": [], "deny": [], "package_managers": True}
    assert (await http.put("/v1/environments/default", json={"image": "node:22", "cpu": 1})).json()["error"]["param"] == "cpu"
    provider = await http.put("/v1/providers/deepseek", json={"apis": {"openai-responses": "https://api.deepseek.com/v1"}, "key": "sk-real"})
    assert provider.json()["secrets_set"] == ["key"] and "sk-real" not in provider.text
    assert [found["name"] for found in (await http.get("/v1/environments")).json()["data"]] == ["default"]
    assert (await http.get("/v1/providers/deepseek")).json()["apis"] == {"openai-responses": "https://api.deepseek.com/v1"}
    assert (await http.delete("/v1/harnesses/echo")).json() == {"name": "echo", "deleted": True}
    assert (await http.get("/v1/environments/default")).json()["harnesses"] == []
    assert (await http.delete("/v1/harnesses/echo")).status_code == 404
    for kept in (await http.put("/v1/harnesses/shell", content=package()), await http.delete("/v1/harnesses/harness-package-builder")):
        assert kept.status_code == 400 and "ships with the platform" in kept.json()["error"]["message"]      # the platform's, not changed in place


async def test_responses_files_and_containers_as_the_openai_api_has_them(http: httpx.AsyncClient, platform: Platform):
    await setup(http)
    uploaded = (await http.post("/v1/files", files={"file": ("notes.txt", b"from the caller")}, data={"purpose": "user_data"})).json()
    assert (uploaded["object"], uploaded["bytes"], uploaded["filename"]) == ("file", 15, "notes.txt")
    assert (await http.get(f"/v1/files/{uploaded['id']}/content")).content == b"from the caller"
    assert [file["id"] for file in (await http.get("/v1/files")).json()["data"]] == [uploaded["id"]]
    platform.driver.writes = {"made.txt": b"by the agent"}
    body = {"model": LINE, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "read it"}, {"type": "input_file", "file_id": uploaded["id"]}]}]}
    done = (await http.post("/v1/responses", json=body)).json()
    assert done["status"] == "completed" and done["output"][0]["content"][0]["text"] == "hello"
    assert platform.driver.found[-1][f"files/{uploaded['id']}/notes.txt"] == "from the caller"
    assert (await http.get(f"/v1/responses/{done['id']}")).json() == done
    assert (await http.get(f"/v1/responses/{done['id']}/input_items")).json()["data"][0]["content"][0]["text"] == "read it"
    container = "cntr_" + done["metadata"]["hg_session"][5:]
    assert (await http.get(f"/v1/containers/{container}")).json()["object"] == "container"
    listed = (await http.get(f"/v1/containers/{container}/files")).json()["data"]
    assert [file["path"] for file in listed] == ["/work/made.txt"]
    assert (await http.get(f"/v1/containers/{container}/files/{listed[0]['id']}/content")).content == b"by the agent"
    assert (await http.get("/v1/containers/cntr_nothing/files")).status_code == 404
    async with http.stream("POST", "/v1/responses", json={"model": LINE, "input": "more", "stream": True,
                                                          "previous_response_id": done["id"]}) as answer:
        kinds = [line.removeprefix("event: ") async for line in answer.aiter_lines() if line.startswith("event: ")]
    assert kinds[0] == "response.created" and kinds[-1] == "response.completed" and "response.output_text.delta" in kinds
    platform.driver.hold = True
    started = (await http.post("/v1/responses", json={"model": LINE, "input": "long", "background": True})).json()
    assert started["status"] == "queued"
    async for event in platform.turns.follow(ORG, started["id"]):
        if event["type"] == "response.output_item.done":
            assert (await http.post(f"/v1/responses/{started['id']}/cancel")).status_code == 200
    assert (await http.get(f"/v1/responses/{started['id']}")).json()["status"] == "cancelled"
    platform.driver.hold = False
    platform.driver.stopped.clear()
    unkept = (await http.post("/v1/responses", json={"model": LINE, "input": "x", "store": False})).json()
    assert unkept["status"] == "completed" and (await http.get(f"/v1/responses/{unkept['id']}")).status_code == 404
    assert (await http.delete(f"/v1/files/{uploaded['id']}")).json()["deleted"]
    assert (await http.post("/v1/responses", json=body)).json()["error"]["param"] == "input"       # the file is gone


async def test_sessions_are_their_requests_and_responses_and_usage_is_the_organizations(http: httpx.AsyncClient, platform: Platform):
    await setup(http)
    first = (await http.post("/v1/responses", json={"model": LINE, "input": "one"})).json()
    second = (await http.post("/v1/responses", json={"model": LINE, "input": "two", "previous_response_id": first["id"]})).json()
    session = first["metadata"]["hg_session"]
    listed = (await http.get("/v1/sessions")).json()["data"]
    assert [(found["id"], found["turns"], found["model"], found["container_id"]) for found in listed] == [
        (session, 2, LINE, "cntr_" + session[5:])]
    turns = (await http.get(f"/v1/sessions/{session}/turns")).json()["data"]
    assert [turn["number"] for turn in turns] == [1, 2]
    assert turns[0]["request"] == {"model": LINE, "input": "one"} and turns[0]["response"] == first
    used = turns[0]["used"]                                    # what its sandbox used: how long, what it was allowed, what was measured
    assert set(used) == {"seconds", "cpus", "memory_mb", "cpu_seconds", "memory_byte_seconds"} and (used["cpus"], used["memory_mb"]) == (2, 4096)
    assert turns[1]["request"]["previous_response_id"] == first["id"] and turns[1]["response"] == second
    made = (await http.put("/v1/harnesses/made", json={"session": session, "path": "made"})).json()
    assert "no folder" in made["error"]["message"]
    used = (await http.get("/v1/usage")).json()
    assert used["object"] == "usage" and used["storage_bytes"] > 0 and (used["start"], used["end"]) == (None, None)
    assert set(used) >= {"cpu_seconds", "memory_byte_seconds", "network_in_bytes", "network_out_bytes", "storage_byte_seconds"}
    assert (await http.get("/v1/usage?start=2999-01-01")).json()["cpu_seconds"] == 0
    assert (await http.get("/v1/usage?start=soon")).json()["error"]["param"] == "start"
    assert (await http.delete(f"/v1/responses/{first['id']}")).json()["deleted"]
    assert (await http.get(f"/v1/sessions/{session}/turns")).json()["data"][0]["response"] is None
    assert (await http.delete(f"/v1/sessions/{session}")).json()["deleted"]
    assert (await http.get(f"/v1/sessions/{session}")).status_code == 404
    another = (await http.post("/v1/keys", json={"name": "my app"})).json()
    theirs = {"Authorization": f"Bearer {another['key']}"}
    assert len((await http.get("/v1/keys")).json()["data"]) == 2 and (await http.get("/v1/platform", headers=theirs)).status_code == 200
    assert (await http.delete(f"/v1/keys/{another['id']}")).json()["revoked"]
    assert (await http.get("/v1/platform", headers=theirs)).status_code == 401


async def test_a_link_opens_a_sessions_files_to_a_browser_sandboxed_and_for_a_time(http: httpx.AsyncClient, platform: Platform, monkeypatch: pytest.MonkeyPatch):
    await setup(http)
    platform.driver.writes = {"site/index.html": b"<h1>made</h1>", "data.bin": 9 * 1024 * 1024, "empty.txt": b""}
    done = (await http.post("/v1/responses", json={"model": LINE, "input": "build"})).json()
    container, nobody = "cntr_" + done["metadata"]["hg_session"][5:], {"Authorization": ""}
    assert (await http.post(f"/v1/containers/{container}/view", headers=nobody)).status_code == 401       # a link is asked for with a key
    assert (await http.post("/v1/containers/cntr_" + "0" * 26 + "/view")).status_code == 404
    link = (await http.post(f"/v1/containers/{container}/view")).json()
    assert link["url"].startswith("/view/") and link["expires_at"] > time.time() + 3000
    page = await http.get(link["url"] + "site/index.html", headers=nobody)                                # and opened without one
    assert page.status_code == 200 and page.text == "<h1>made</h1>" and page.headers["content-type"] == "text/html; charset=utf-8"
    assert page.headers["content-security-policy"].startswith("sandbox ") and page.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in (await http.get(link["url"] + "site/index.html?download", headers=nobody)).headers["content-disposition"]
    part = await http.get(link["url"] + "data.bin", headers={**nobody, "Range": "bytes=4194300-4194309"})     # across two chunks
    assert part.status_code == 206 and len(part.content) == 10 and part.headers["content-range"] == "bytes 4194300-4194309/9437184"
    assert (await http.get(link["url"] + "data.bin", headers={**nobody, "Range": "bytes=99999999-"})).status_code == 416
    empty = await http.get(link["url"] + "empty.txt", headers=nobody)
    assert empty.status_code == 200 and empty.content == b""
    for wrong in (link["url"] + "nope.txt", link["url"] + "../mind/x", "/view/not-a-token/site/index.html", link["url"][:-3] + "0a/site/index.html"):
        assert (await http.get(wrong, headers=nobody)).status_code == 404, wrong
    monkeypatch.setattr(time, "time", lambda: link["expires_at"] + 1)                                     # past its time
    assert (await http.get(link["url"] + "site/index.html", headers=nobody)).status_code == 404
