"""Layer 5, end to end without Docker: the API with an in-process worker on a fake node, over ASGI.
Sign-up, providers, a harness from an archive, a turn whose package reads request.json and prints
Responses stream events, continuation from a snapshot, streaming, background, cancel, a request that
supersedes a running turn, stateless, archive, trace, response.sh shaping, the harness repository,
the deployer's view, organizations apart, and the official OpenAI SDK unchanged."""
import asyncio
import base64
import io
import json
import tarfile

import httpx
import pytest

from fakes import FakeNode, archive, RESPONSE_SH, folder
from harnessgateway.api.app import Service, create_app
from harnessgateway.config import Settings
from harnessgateway.services.worker import Worker

# a package that runs until it is stopped (and says so), unless the input is "quick"
SLOW = """#!/usr/bin/env bash
echo started
trap 'echo stopping; exit 0' TERM
if [ "$(jq -r '.input' "$HG_REQUEST")" = "quick" ]; then
  printf '{"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_q","role":"assistant","status":"completed","content":[{"type":"output_text","text":"quick done","annotations":[]}]}}\\n'
  exit 0
fi
sleep 30 & wait $!
"""


@pytest.fixture
def platform(tmp_path, vault, monkeypatch):
    monkeypatch.setenv("HG_CATALOG_DIR", str(tmp_path / "catalog"))
    folder(tmp_path / "catalog" / "demo", description="shipped")
    settings = Settings(data_dir=tmp_path / "data", public_url="http://api.test", signup="open", join_token="jt",
                        deployer_key="hgd_deployer")
    service = Service(settings, vault)
    app = create_app(service)
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    node = FakeNode()
    worker = Worker("http://api.test", node, tmp_path / "cache", name="w1", slots=2, version="t", join_token="jt", transport=transport)
    return app, transport, worker, node


async def owner_client(transport, email="chase@acme.test", org=None) -> tuple[httpx.AsyncClient, str, str]:
    """An account signs up (open sign-up gives it an organization), mints a key; a client with it."""
    anon = httpx.AsyncClient(transport=transport, base_url="http://api.test")
    me = (await anon.post("/auth/signup", json={"email": email, "password": "password1", "name": "Chase"})).json()
    org = me["current"]["org"]
    key = (await anon.post(f"/v1/members/{me['current']['member']}/keys", json={"label": "test"})).json()["secret"]
    await anon.aclose()
    client = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": f"Bearer {key}"}, timeout=60)
    client.cookies.clear()
    return client, key, org


async def setup(transport, client) -> None:
    await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})
    r = await client.post("/v1/harnesses/echo", json={"archive": base64.b64encode(archive()).decode()})
    assert r.status_code == 200, r.text


async def running(client, rid: str) -> None:
    """Wait until a worker has the turn (the public status says in_progress from the queue on)."""
    for _ in range(200):
        if (await client.get(f"/v1/responses/{rid}/detail")).json()["status_raw"] == "in_progress":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the turn never started")


async def settled(client, rid: str) -> dict:
    for _ in range(300):
        got = (await client.get(f"/v1/responses/{rid}")).json()
        if got["status"] not in ("queued", "in_progress"):
            return got
        await asyncio.sleep(0.1)
    raise AssertionError(f"{rid} is still running")


async def with_worker(worker, coro):
    task = asyncio.create_task(worker.run())
    try:
        return await coro
    finally:
        worker.stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_turns_stream_and_continue(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await setup(transport, client)
        # a plain turn: the package read request.json and printed events; the response is built from them
        r = await client.post("/v1/responses", json={"model": "echo", "input": "hello", "instructions": "be brief", "metadata": {"k": "v"}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed" and body["object"] == "response" and body["model"] == "echo/prov/m1"
        kinds = [i["type"] for i in body["output"]]
        assert kinds == ["shell_call", "message"], body["output"]
        assert body["output"][1]["content"][0]["text"] == "prompt=hello ins=be brief"
        assert body["usage"]["input_tokens"] == 3 and body["usage"]["total_tokens"] == 8
        assert body["metadata"]["k"] == "v" and body["metadata"]["session_id"].startswith("sess_") and body["metadata"]["turn"] == "1"
        detail = (await client.get(f"/v1/responses/{body['id']}/detail")).json()
        assert "turn=1 model=m1 key=sk-" in detail["log"] and "url=http://hg-provider:8080/v1" in detail["log"]
        assert "home=from-home" in detail["log"] and "workspace=from-workspace" in detail["log"]
        assert detail["stderr"].strip() == "stderr line" and detail["exit_code"] == 0
        # the package got the verbatim request
        req = (await client.get(f"/v1/responses/{body['id']}/request")).json()
        assert req == {"model": "echo", "input": "hello", "instructions": "be brief", "metadata": {"k": "v"}}
        # continuation: the same workspace (out.txt from turn 1 is there), state persisted
        sid = body["metadata"]["session_id"]
        r2 = (await client.post("/v1/responses", json={"model": "echo", "input": "again", "previous_response_id": body["id"]})).json()
        assert r2["status"] == "completed" and r2["metadata"]["turn"] == "2" and r2["previous_response_id"] == body["id"]
        files = (await client.get(f"/v1/sessions/{sid}/files")).json()["data"]
        assert {f["path"] for f in files} == {"README.md", "out.txt"}
        assert (await client.get(f"/v1/sessions/{sid}/files/out.txt")).content == b"again\n"
        with tarfile.open(fileobj=io.BytesIO((await client.get(f"/v1/sessions/{sid}/archive")).content)) as tar:
            assert "./out.txt" in tar.getnames() and not any("state" in n for n in tar.getnames())
        detail2 = (await client.get(f"/v1/responses/{r2['id']}/detail")).json()
        assert f"previous={body['id']}" in detail2["log"]
        # streaming: the standard envelope around the package's events
        async with client.stream("POST", "/v1/responses", json={"model": "echo", "input": "streamed", "stream": True, "previous_response_id": r2["id"]}) as s:
            assert s.status_code == 200 and s.headers["content-type"].startswith("text/event-stream")
            events = [json.loads(ln[6:]) for ln in (await s.aread()).decode().split("\n") if ln.startswith("data: ")]
        kinds = [e["type"] for e in events]
        assert kinds[:2] == ["response.created", "response.in_progress"] and kinds[-1] == "response.completed"
        assert "response.output_text.delta" in kinds and "response.output_item.done" in kinds
        assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))
        assert "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta") == "prompt=streamed ins="
        assert events[-1]["response"]["output"][-1]["content"][0]["text"] == "prompt=streamed ins="
        # input files land in HG_FILES
        r3 = (await client.post("/v1/responses", json={"model": "echo", "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "with a file"}, {"type": "input_file", "filename": "a.txt", "file_data": "data:text/plain;base64,aGk="}]}]})).json()
        d3 = (await client.get(f"/v1/responses/{r3['id']}/detail")).json()
        assert "file=a.txt:hi" in d3["log"]
        trace = (await client.get(f"/v1/sessions/{sid}/trace")).json()["data"]
        assert [t["kind"] for t in trace] == ["request", "response"] * 3
        assert trace[0]["body"]["input"] == "hello" and trace[1]["body"]["status"] == "completed"
        # the trace keeps file sizes, not bytes
        t3 = (await client.get(f"/v1/sessions/{r3['metadata']['session_id']}/trace")).json()["data"]
        assert "characters" in t3[0]["body"]["input"][0]["content"][1]["file_data"]
        # stateless
        r4 = (await client.post("/v1/responses", json={"model": "echo", "input": "stateless", "store": False})).json()
        assert r4["status"] == "completed" and r4["store"] is False
        s4 = (await client.get(f"/v1/sessions/{r4['metadata']['session_id']}")).json()
        assert s4["stored"] is False and (await client.get(f"/v1/sessions/{s4['id']}/files")).json()["data"] == []
        assert (await client.post("/v1/responses", json={"model": "echo", "input": "x", "previous_response_id": r4["id"]})).status_code == 400
        # the sessions, as the app lists them
        listed = (await client.get("/v1/sessions")).json()["data"]
        assert {s["id"] for s in listed} >= {sid, s4["id"]} and all(s["status"] == "idle" for s in listed)
        # the official SDK, unchanged
        from openai import AsyncOpenAI
        sdk = AsyncOpenAI(api_key=key, base_url="http://api.test/v1", http_client=httpx.AsyncClient(transport=transport))
        resp = await sdk.responses.create(model="echo", input="via sdk")
        assert resp.status == "completed" and resp.output_text == "prompt=via sdk ins="
        deltas = []
        async for e in await sdk.responses.create(model="echo", input="sdk stream", stream=True):
            if e.type == "response.output_text.delta":
                deltas.append(e.delta)
        assert "".join(deltas) == "prompt=sdk stream ins="
        assert node.installs == 1
    asyncio.run(with_worker(worker, go()))


def test_refusals(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await setup(transport, client)
        for body, code, text in [({"model": "echo", "input": "x", "temperature": "hot"}, 400, "not a valid Responses request"),
                                 ({"model": "nope", "input": "x"}, 404, "no harness"),
                                 ({"model": "echo/other/m", "input": "x"}, 404, "no provider"),
                                 ({"model": "echo", "input": ""}, 400, "input is required"),
                                 ({"model": "echo", "input": "x", "previous_response_id": "resp_" + "0" * 32}, 404, "no response"),
                                 ({"input": "x"}, 400, "model is a harness name")]:
            r = await client.post("/v1/responses", json=body)
            assert r.status_code == code and text in r.text, (body, r.text)
        bad = (await client.post("/v1/harnesses/bad", json={"archive": base64.b64encode(archive(files={"request.sh": "if then"})).decode()})).json()
        assert "not valid bash" in bad["detail"]
        assert (await httpx.AsyncClient(transport=transport, base_url="http://api.test").get("/v1/me")).status_code == 401
        # the organization's rate ceiling: a 429 with Retry-After, and a retried key is never a new turn
        dep = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": "Bearer hgd_deployer"})
        assert (await dep.patch(f"/v1/orgs/{org}", json={"limits": {"requests_per_minute": 2}})).status_code == 200
        first = await client.post("/v1/responses", json={"model": "echo", "input": "one", "background": True}, headers={"Idempotency-Key": "job-1"})
        assert first.status_code == 200
        again = await client.post("/v1/responses", json={"model": "echo", "input": "changed", "background": True}, headers={"Idempotency-Key": "job-1"})
        assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
        assert (await client.post("/v1/responses", json={"model": "echo", "input": "two", "background": True})).status_code == 200
        refused = await client.post("/v1/responses", json={"model": "echo", "input": "three", "background": True})
        assert refused.status_code == 429 and refused.headers["retry-after"] == "60" and "requests per minute" in refused.text
        assert (await client.post("/v1/responses", json={"model": "echo", "input": "x", "background": True}, headers={"Idempotency-Key": "job-1"})).json()["id"] == first.json()["id"]
        assert (await client.post("/v1/responses", json={"model": "echo", "input": "x"}, headers={"Idempotency-Key": "a b"})).status_code == 400
    asyncio.run(with_worker(worker, go()))


def test_background_cancel_and_supersede(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})
        await client.post("/v1/harnesses/slow", json={"archive": base64.b64encode(archive(files={"request.sh": SLOW})).decode()})
        r = (await client.post("/v1/responses", json={"model": "slow", "input": "go", "background": True})).json()
        assert r["status"] in ("queued", "in_progress")
        sid = r["metadata"]["session_id"]
        await running(client, r["id"])
        assert (await client.get(f"/v1/sessions/{sid}")).json()["status"] == "running"
        await asyncio.sleep(0.6)                       # let the trap be installed
        # a new request on the running session: the running turn is stopped, the new one runs after it, on its workspace
        r2 = (await client.post("/v1/responses", json={"model": "slow", "input": "quick", "previous_response_id": r["id"]})).json()
        assert r2["status"] == "completed" and r2["output"][0]["content"][0]["text"] == "quick done" and r2["metadata"]["turn"] == "2", r2
        first = await settled(client, r["id"])
        assert first["status"] == "cancelled" and first["metadata"]["incomplete_reason"] == "superseded"
        assert first["output"][0]["content"][0]["text"].strip().endswith("stopping")       # SIGTERM reached the package: a graceful stop
        detail2 = (await client.get(f"/v1/responses/{r2['id']}/detail")).json()
        assert detail2["started"] > first["completed_at"] - 1                               # the second ran after the first ended
        # cancel a running turn on purpose
        r3 = (await client.post("/v1/responses", json={"model": "slow", "input": "go", "background": True, "previous_response_id": r2["id"]})).json()
        await running(client, r3["id"])
        await asyncio.sleep(0.3)
        cancelled = (await client.post(f"/v1/responses/{r3['id']}/cancel")).json()
        assert cancelled["status"] == "cancelled" and cancelled["metadata"]["incomplete_reason"] == "cancelled"
        trace = [t["kind"] for t in (await client.get(f"/v1/sessions/{sid}/trace")).json()["data"]]
        assert trace == ["request", "supersede", "request", "response", "response", "request", "cancel", "response"]
        assert (await client.get(f"/v1/sessions/{sid}")).json()["status"] == "idle"
        # a running session cannot be deleted; an idle one can
        r4 = (await client.post("/v1/responses", json={"model": "slow", "input": "go", "background": True, "previous_response_id": r3["id"]})).json()
        await running(client, r4["id"])
        assert (await client.delete(f"/v1/sessions/{sid}")).status_code == 409
        await client.post(f"/v1/responses/{r4['id']}/cancel")
        assert (await client.delete(f"/v1/sessions/{sid}")).status_code == 200
    asyncio.run(with_worker(worker, go()))


def test_response_sh_shapes(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})
        await client.post("/v1/harnesses/shaped", json={"archive": base64.b64encode(archive(files={"response.sh": RESPONSE_SH})).decode()})
        r = (await client.post("/v1/responses", json={"model": "shaped", "input": "hi"})).json()
        assert r["status"] == "completed" and r["output"][0]["content"][0]["text"] == "shaped: outcome=exited exit=0"
        assert r["usage"]["input_tokens"] == 1 and r["metadata"]["shaped"] == "yes"
        # a package whose response.sh prints something the schema refuses fails loudly
        await client.post("/v1/harnesses/broken", json={"archive": base64.b64encode(archive(files={"response.sh": "#!/bin/bash\necho '{\"output\":[{\"type\":\"nope\"}]}'\n"})).decode()})
        r2 = (await client.post("/v1/responses", json={"model": "broken", "input": "hi"})).json()
        assert r2["status"] == "failed" and r2["error"]["code"] == "server_error" and r2["metadata"]["error_code"] == "package_output_invalid" and "output[0]" in r2["error"]["message"]
        # a package that prints plain text gets a message of it
        await client.post("/v1/harnesses/plain", json={"archive": base64.b64encode(archive(files={"request.sh": "#!/bin/bash\necho just text\n"})).decode()})
        r3 = (await client.post("/v1/responses", json={"model": "plain", "input": "hi"})).json()
        assert r3["status"] == "completed" and r3["output"][0]["content"][0]["text"] == "just text" and r3["metadata"]["raw_stdout"] == "true"
        # a non-zero exit is a failure
        await client.post("/v1/harnesses/exit3", json={"archive": base64.b64encode(archive(files={"request.sh": "#!/bin/bash\nexit 3\n"})).decode()})
        r4 = (await client.post("/v1/responses", json={"model": "exit3", "input": "hi"})).json()
        assert r4["status"] == "failed" and r4["metadata"]["error_code"] == "exit_status" and r4["metadata"]["exit_code"] == "3"
    asyncio.run(with_worker(worker, go()))


def test_harness_repository_members_and_the_deployer(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})
        # from the catalog
        h = (await client.post("/v1/harnesses/echo", json={"catalog": "demo"})).json()
        assert h["origin"] == "catalog/demo" and h["description"] == "shipped" and h["editable"] is True
        assert {f["path"] for f in h["files"]} >= {"harness.json", "install.sh", "request.sh"}
        assert (await client.get("/v1/harnesses/echo/files/request.sh")).text.startswith("#!/usr/bin/env bash")
        # a commit through the API
        head = h["head"]
        c = await client.post("/v1/harnesses/echo/commits", json={"changes": {"README.md": "# mine", "seeds/home/home.txt": "edited"}, "message": "notes", "parent": head})
        assert c.status_code == 200 and c.json()["head"] != head
        log = (await client.get("/v1/harnesses/echo/commits")).json()["data"]
        assert [x["message"] for x in log] == ["notes", "fork of the catalog package demo"] and log[0]["author"] == "chase"
        stale = await client.post("/v1/harnesses/echo/commits", json={"changes": {"x": "y"}, "parent": head})
        assert stale.status_code == 400 and "changed since" in stale.text
        broken = await client.post("/v1/harnesses/echo/commits", json={"changes": {"harness.json": "{}"}})
        assert broken.status_code == 400 and (await client.get("/v1/harnesses/echo")).json()["head"] == c.json()["head"]
        # a session runs the head it started on, even after a later commit
        r1 = (await client.post("/v1/responses", json={"model": "echo", "input": "one"})).json()
        d1 = (await client.get(f"/v1/responses/{r1['id']}/detail")).json()
        assert "home=edited" in d1["log"]
        await client.post("/v1/harnesses/echo/commits", json={"changes": {"seeds/home/home.txt": "newer"}, "message": "again"})
        r2 = (await client.post("/v1/responses", json={"model": "echo", "input": "two", "previous_response_id": r1["id"]})).json()
        assert r2["metadata"]["version"] == r1["metadata"]["version"]
        r3 = (await client.post("/v1/responses", json={"model": "echo", "input": "three"})).json()
        assert r3["metadata"]["version"] != r1["metadata"]["version"] and "home=newer" in (await client.get(f"/v1/responses/{r3['id']}/detail")).json()["log"]
        # export and push
        tgz = (await client.get("/v1/harnesses/echo/archive")).content
        with tarfile.open(fileobj=io.BytesIO(tgz)) as tar:
            assert "README.md" in tar.getnames()
        pushed = await client.put("/v1/harnesses/echo", content=archive(files={"README.md": None, "NEW.md": "n"}), headers={"x-commit-message": "pushed"})
        assert pushed.status_code == 200 and {f["path"] for f in pushed.json()["files"]} >= {"NEW.md"} and not any(f["path"] == "README.md" for f in pushed.json()["files"])
        f = (await client.post("/v1/harnesses/echo-copy", json={"fork": "echo"})).json()
        assert f["origin"] == f"{org}/echo" and len((await client.get("/v1/harnesses/echo-copy/commits")).json()["data"]) == 4
        # a member runs every harness and sees their own sessions; an owner changes harnesses and sees every session
        await client.post("/v1/members", json={"name": "dev", "role": "member"})
        dev_key = (await client.post("/v1/members/dev/keys", json={})).json()["secret"]
        dev = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": f"Bearer {dev_key}"}, timeout=60)
        names = {h["name"]: h["editable"] for h in (await dev.get("/v1/harnesses")).json()["data"]}
        assert names == {"echo": False, "echo-copy": False}
        assert (await dev.post("/v1/harnesses/echo/commits", json={"changes": {"x": "y"}})).status_code == 403
        assert (await dev.post("/v1/harnesses/mine", json={"catalog": "demo"})).status_code == 403
        assert (await dev.put("/v1/providers/other", json={"url": "https://o.example", "api": "openai-chat", "key": "k"})).status_code == 403
        mine = (await dev.post("/v1/responses", json={"model": "echo-copy", "input": "private"})).json()
        assert mine["status"] == "completed"
        assert (await client.get(f"/v1/responses/{mine['id']}")).status_code == 200                  # the owner sees it
        assert mine["metadata"]["session_id"] in {s["id"] for s in (await client.get("/v1/sessions")).json()["data"]}
        assert (await dev.get(f"/v1/responses/{r1['id']}")).status_code == 404                       # a member sees only their own
        assert [s["member"] for s in (await dev.get("/v1/sessions")).json()["data"]] == ["dev"]
        # the deployer sees names and counts, never content
        dep = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": "Bearer hgd_deployer"})
        orgs = (await dep.get("/v1/orgs")).json()["data"]
        assert orgs[0]["name"] == org and orgs[0]["counts"]["responses"] == 4 and orgs[0]["counts"]["running"] == 0
        one = (await dep.get(f"/v1/orgs/{org}")).json()
        assert one["harnesses"] == ["echo", "echo-copy"] and one["providers"] == ["prov"] and {m["name"] for m in one["members"]} == {"chase", "dev"}
        assert (await dep.get("/v1/responses/" + r1["id"])).status_code == 403
        assert (await dep.get("/v1/fleet")).json()["workers"] == 1
        # another organization sees nothing of this one
        other, _, _ = await owner_client(transport, email="eve@other.test")
        assert (await other.get("/v1/harnesses")).json()["data"] == []
        assert (await other.get(f"/v1/responses/{r1['id']}")).status_code == 404
        # suspended: refused until active again
        assert (await dep.patch(f"/v1/orgs/{org}", json={"status": "suspended"})).json()["status"] == "suspended"
        assert (await client.get("/v1/me")).status_code == 401
        assert (await dep.patch(f"/v1/orgs/{org}", json={"status": "active"})).json()["status"] == "active"
        assert (await client.get("/v1/me")).status_code == 200
    asyncio.run(with_worker(worker, go()))


# accounts: the app's sign-in, over the same routes
def test_accounts_sign_up_invite_switch_and_the_cookie_on_member_routes(platform):
    app, transport, worker, node = platform

    async def scenario():
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            assert (await c.get("/auth/providers")).json() == {"signup": "open", "password": True}
            assert (await c.get("/auth/me")).status_code == 401
            assert (await c.post("/auth/signup", json={"email": "chase@example.com", "password": "short"})).status_code == 400
            r = await c.post("/auth/signup", json={"email": "Chase@Example.com", "password": "correct horse", "name": "Chase"})
            assert r.status_code == 201, r.text
            me = r.json()
            assert me["account"]["email"] == "chase@example.com" and me["account"]["name"] == "Chase"
            assert me["current"] == {**me["current"], "org": "chase", "member": "chase", "role": "owner"}   # an organization of its own
            assert "hg_session" in c.cookies
            assert (await c.post("/auth/signup", json={"email": "chase@example.com", "password": "correct horse"})).status_code == 409
            # the cookie is a member's credential on every member route; the deployer's routes still want the key
            assert (await c.get("/v1/me")).json()["name"] == "chase"
            assert (await c.put("/v1/providers/prov", json={"url": "https://api.example/v1", "api": "openai-chat", "key": "sk-real"})).status_code == 200
            assert (await c.get("/v1/fleet")).status_code == 401
            # an owner invites an email; the account that signs up with it joins as a member, not with an organization of its own
            assert (await c.post("/v1/invites", json={"email": "dev@example.com", "role": "member"})).status_code == 200
            assert [i["email"] for i in (await c.get("/v1/invites")).json()["data"]] == ["dev@example.com"]
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as dev:
                r = await dev.post("/auth/signup", json={"email": "dev@example.com", "password": "another horse"})
                assert r.status_code == 201, r.text
                assert r.json()["current"] == {**r.json()["current"], "org": "chase", "member": "dev", "role": "member"}
                assert (await dev.get("/v1/members")).status_code == 200
                assert (await dev.post("/v1/invites", json={"email": "x@example.com"})).status_code == 403   # a member, not an owner
                # an organization of their own, and switching between the two
                assert (await dev.post("/auth/orgs", json={"name": "devs"})).json()["current"]["org"] == "devs"
                assert sorted(t["org"] for t in (await dev.get("/auth/me")).json()["orgs"]) == ["chase", "devs"]
                assert (await dev.post("/auth/switch", json={"org": "chase"})).json()["current"]["role"] == "member"
                assert (await dev.post("/auth/switch", json={"org": "acme"})).status_code == 404
                assert (await dev.post("/auth/logout")).status_code == 200
                assert (await dev.get("/auth/me")).status_code == 401
            assert (await c.get("/v1/invites")).json()["data"] == []            # spent
            # wrong password, then the right one
            assert (await c.post("/auth/login", json={"email": "chase@example.com", "password": "wrong"})).status_code == 401
            assert (await c.post("/auth/login", json={"email": "chase@example.com", "password": "correct horse"})).status_code == 200
    asyncio.run(scenario())


def test_sign_up_is_closed_but_an_invitation_opens_it(tmp_path, vault):
    settings = Settings(data_dir=tmp_path / "data", public_url="http://api.test", signup="closed", deployer_key="hgd_deployer")
    app = create_app(Service(settings, vault))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as c:
            assert (await c.post("/auth/signup", json={"email": "a@example.com", "password": "correct horse"})).status_code == 403
            r = await c.post("/v1/orgs", json={"name": "acme", "owner": "ops"}, headers={"Authorization": "Bearer hgd_deployer"})
            assert r.status_code == 200, r.text
            key = r.json()["key"]["secret"]
            assert (await c.post("/v1/invites", json={"email": "a@example.com", "role": "owner"}, headers={"Authorization": f"Bearer {key}"})).status_code == 200
            r = await c.post("/auth/signup", json={"email": "a@example.com", "password": "correct horse"})
            assert r.status_code == 201 and r.json()["current"]["org"] == "acme" and r.json()["current"]["role"] == "owner"
    asyncio.run(go())
