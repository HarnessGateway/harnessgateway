"""Layer 6, end to end without Docker: the API with an in-process worker on a fake node, over ASGI.
Sign-up, providers, a harness from an archive, a turn whose package reads request.json and prints
Responses stream events, continuation from a snapshot, streaming, background, cancel, steer,
stateless, archive, trace, response.sh shaping, teams, the deployer's view, organizations apart,
and the official OpenAI SDK unchanged."""
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

SLOW = """#!/usr/bin/env bash
echo started
trap 'printf "{\\"type\\":\\"response.output_item.done\\",\\"item\\":{\\"type\\":\\"message\\",\\"id\\":\\"msg_s\\",\\"role\\":\\"assistant\\",\\"status\\":\\"completed\\",\\"content\\":[{\\"type\\":\\"output_text\\",\\"text\\":\\"steer=%s\\",\\"annotations\\":[]}]}}\\n" "$(cat "$HG_DIR/interrupt")"; exit 0' USR1
trap 'echo stopping; exit 0' TERM
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
    asyncio.run(go())


def test_background_cancel_steer(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})
        await client.post("/v1/harnesses/slow", json={"archive": base64.b64encode(archive(files={"request.sh": SLOW})).decode()})
        r = (await client.post("/v1/responses", json={"model": "slow", "input": "go", "background": True})).json()
        assert r["status"] in ("queued", "in_progress")
        sid = r["metadata"]["session_id"]
        await running(client, r["id"])
        await asyncio.sleep(0.6)                       # let the trap be installed
        steered = (await client.post(f"/v1/sessions/{sid}/interrupt", json={"input": "turn left"}))
        assert steered.status_code == 200, steered.text
        for _ in range(300):
            got = (await client.get(f"/v1/responses/{r['id']}")).json()
            if got["status"] != "in_progress":
                break
            await asyncio.sleep(0.1)
        detail = (await client.get(f"/v1/responses/{r['id']}/detail")).json()
        assert got["status"] == "completed" and got["output"][0]["content"][0]["text"] == "steer=turn left", detail
        # cancel a running turn
        r2 = (await client.post("/v1/responses", json={"model": "slow", "input": "go", "background": True, "previous_response_id": r["id"]})).json()
        await running(client, r2["id"])
        await asyncio.sleep(0.3)
        cancelled = (await client.post(f"/v1/responses/{r2['id']}/cancel")).json()
        assert cancelled["status"] == "cancelled" and cancelled["metadata"]["incomplete_reason"] == "cancelled"
        trace = [t["kind"] for t in (await client.get(f"/v1/sessions/{sid}/trace")).json()["data"]]
        assert trace == ["request", "interrupt", "response", "request", "cancel", "response"]
        # a busy session refuses a second turn
        r3 = (await client.post("/v1/responses", json={"model": "slow", "input": "go", "background": True, "previous_response_id": r2["id"]})).json()
        busy = await client.post("/v1/responses", json={"model": "slow", "input": "go", "previous_response_id": r2["id"]})
        assert busy.status_code == 409
        await client.post(f"/v1/responses/{r3['id']}/cancel")
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


def test_harness_repository_and_teams(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})
        # from the catalog
        h = (await client.post("/v1/harnesses/echo", json={"catalog": "demo"})).json()
        assert h["origin"] == "catalog/demo" and h["description"] == "shipped" and h["editable"] is True
        assert {f["path"] for f in h["files"]} >= {"harness.json", "install.sh", "request.sh"}
        assert (await client.get("/v1/harnesses/echo/files/request.sh")).text.startswith("#!/usr/bin/env bash")
        # a commit through the editor
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
        # fork, teams and visibility
        await client.post("/v1/members", json={"name": "dev", "role": "member"})
        dev_key = (await client.post("/v1/members/dev/keys", json={})).json()["secret"]
        dev = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": f"Bearer {dev_key}"}, timeout=60)
        await client.put("/v1/teams/platform", json={"members": ["dev"], "title": "Platform"})
        f = (await client.post("/v1/harnesses/echo-platform", json={"fork": "echo", "team": "platform"})).json()
        assert f["team"] == "platform" and f["origin"] == f"{org}/echo" and len((await client.get("/v1/harnesses/echo-platform/commits")).json()["data"]) == 4
        names = {h["name"]: h["editable"] for h in (await dev.get("/v1/harnesses")).json()["data"]}
        assert names == {"echo": False, "echo-platform": True}
        assert (await dev.post("/v1/harnesses/echo/commits", json={"changes": {"x": "y"}})).status_code == 403
        assert (await dev.post("/v1/harnesses/echo-platform/commits", json={"changes": {"x": "y"}})).status_code == 200
        assert (await dev.post("/v1/harnesses/mine", json={"catalog": "demo"})).status_code == 403
        assert (await dev.post("/v1/harnesses/mine", json={"catalog": "demo", "team": "platform"})).status_code == 200
        r = (await dev.post("/v1/responses", json={"model": "echo-platform", "input": "team run"})).json()
        assert r["status"] == "completed"
        # a team's sessions are the team's: a teammate sees them, another member of the organization does not
        for name in ("peer", "outsider"):
            await client.post("/v1/members", json={"name": name, "role": "member"})
        await client.put("/v1/teams/platform", json={"members": ["dev", "peer"], "title": "Platform"})
        peer_key, out_key = [(await client.post(f"/v1/members/{n}/keys", json={})).json()["secret"] for n in ("peer", "outsider")]
        peer = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": f"Bearer {peer_key}"}, timeout=60)
        outsider = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": f"Bearer {out_key}"}, timeout=60)
        team_sid = r["metadata"]["session_id"]
        assert (await peer.get(f"/v1/sessions/{team_sid}")).status_code == 200 and (await peer.get(f"/v1/responses/{r['id']}")).status_code == 200
        assert team_sid in {s["id"] for s in (await peer.get("/v1/sessions")).json()["data"]}
        assert (await outsider.get(f"/v1/sessions/{team_sid}")).status_code == 404 and (await outsider.get(f"/v1/responses/{r['id']}")).status_code == 404
        assert (await outsider.get("/v1/sessions")).json()["data"] == []
        mine = (await dev.post("/v1/responses", json={"model": "echo", "input": "private"})).json()
        assert (await peer.get(f"/v1/responses/{mine['id']}")).status_code == 404                 # a member's own session stays theirs
        await client.put("/v1/teams/platform", json={"members": []})
        assert (await dev.post("/v1/responses", json={"model": "echo-platform", "input": "x"})).status_code == 404
        assert (await client.delete("/v1/teams/platform")).status_code == 409
        # the deployer sees counts, never content
        dep = httpx.AsyncClient(transport=transport, base_url="http://api.test", headers={"Authorization": "Bearer hgd_deployer"})
        orgs = (await dep.get("/v1/orgs")).json()["data"]
        assert orgs[0]["name"] == org and orgs[0]["usage"]["turns"] == 5
        assert (await dep.get("/v1/usage")).json()["totals"]["turns"] == 5
        assert (await dep.get("/v1/responses/" + r["id"])).status_code == 403
        # another organization sees nothing of this one
        other, _, _ = await owner_client(transport, email="eve@other.test")
        assert (await other.get("/v1/harnesses")).json()["data"] == []
        assert (await other.get(f"/v1/responses/{r['id']}")).status_code == 404
    asyncio.run(with_worker(worker, go()))


def test_rodeo(platform):
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})
        for name in ("a", "b"):
            await client.post(f"/v1/harnesses/{name}", json={"archive": base64.b64encode(archive()).decode()})
        ra = (await client.post("/v1/responses", json={"model": "a", "input": "task"})).json()
        rb = (await client.post("/v1/responses", json={"model": "b", "input": "task"})).json()
        rodeo = (await client.post("/v1/rodeos", json={"task": "task", "entries": [{"response": ra["id"]}, {"response": rb["id"]}]})).json()
        assert [e["harness"] for e in rodeo["entries"]] == ["a", "b"] and rodeo["winner"] == ""
        picked = (await client.patch(f"/v1/rodeos/{rodeo['id']}", json={"winner": "b"})).json()
        assert picked["winner"] == "b"
        board = (await client.get("/v1/rodeos")).json()["leaderboard"]
        assert board[0] == {"harness": "b", "runs": 1, "wins": 1}
        assert (await client.post("/v1/rodeos", json={"entries": [{"response": ra["id"]}]})).status_code == 400
    asyncio.run(with_worker(worker, go()))


# accounts: the console's sign-in, over the same routes
def test_accounts_sign_up_invite_switch_and_the_cookie_on_member_routes(platform):
    app, transport, worker, node = platform

    async def scenario():
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            assert (await c.get("/auth/providers")).json() == {"signup": "open", "password": True, "mail": False, "github": False, "google": False}
            assert (await c.get("/auth/me")).status_code == 401
            assert (await c.post("/auth/signup", json={"email": "chase@example.com", "password": "short"})).status_code == 400
            r = await c.post("/auth/signup", json={"email": "Chase@Example.com", "password": "correct horse", "name": "Chase"})
            assert r.status_code == 201, r.text
            me = r.json()
            assert me["account"]["email"] == "chase@example.com" and me["account"]["verified"] and me["account"]["has_password"]
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
            # wrong password, then the right one; a changed password
            assert (await c.post("/auth/login", json={"email": "chase@example.com", "password": "wrong"})).status_code == 401
            assert (await c.post("/auth/login", json={"email": "chase@example.com", "password": "correct horse"})).status_code == 200
            assert (await c.post("/auth/password", json={"current": "wrong", "password": "new horse 2"})).status_code == 403
            assert (await c.post("/auth/password", json={"current": "correct horse", "password": "new horse 2"})).status_code == 200
            assert (await c.post("/auth/login", json={"email": "chase@example.com", "password": "new horse 2"})).status_code == 200
            assert (await c.patch("/auth/account", json={"name": "C. Dovey"})).json()["name"] == "C. Dovey"
            # the identity providers are not configured here
            assert (await c.get("/auth/github")).status_code == 404
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


def test_sign_up_with_mail_verifies_first(tmp_path, vault, monkeypatch):
    sent = []
    from harnessgateway.api import signin as auth_routes
    monkeypatch.setattr(auth_routes.Mail, "send", lambda self, to, subject, text: sent.append((to, subject, text)))
    settings = Settings(data_dir=tmp_path / "data", public_url="http://api.test", signup="open", smtp_url="smtp://mail.test:587", mail_from="hg@test")
    app = create_app(Service(settings, vault))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as c:
            r = await c.post("/auth/signup", json={"email": "a@example.com", "password": "correct horse"})
            assert r.status_code == 200 and r.json() == {"verify": True, "email": "a@example.com"} and "hg_session" not in c.cookies
            assert (await c.post("/auth/login", json={"email": "a@example.com", "password": "correct horse"})).status_code == 403
            assert len(sent) == 1 and sent[0][0] == "a@example.com"
            token = sent[0][2].split("token=")[1].split()[0]
            r = await c.get("/auth/verify", params={"token": token})
            assert r.status_code == 303 and r.headers["location"] == "/app" and "hg_session" in c.cookies
            assert (await c.get("/auth/me")).json()["account"]["verified"]
            assert (await c.get("/auth/verify", params={"token": "nope"})).status_code == 400
            # a reset link, then a new password
            assert (await c.post("/auth/reset", json={"email": "a@example.com"})).status_code == 200
            token = sent[-1][2].split("#reset=")[1].split()[0]
            assert (await c.post("/auth/reset/confirm", json={"token": token, "password": "a new horse"})).status_code == 200
            assert (await c.post("/auth/login", json={"email": "a@example.com", "password": "a new horse"})).status_code == 200
    asyncio.run(go())
