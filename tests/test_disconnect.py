"""A client that closes its request stops its turn, with no endpoint called: the stream's end or an
abandoned blocking request is a graceful stop (the package's stop signal and grace), and the response
ends `cancelled` saying the client disconnected. A background turn was asked to outlive the request,
so a follower closing its stream changes nothing. Over a real HTTP server, since a disconnect is a
thing the transport does."""
import asyncio
import base64
import json
import socket

import httpx
import uvicorn

from fakes import FakeNode, archive
from harnessgateway.api.app import Service, create_app
from harnessgateway.config import Settings
from harnessgateway.services.worker import Worker
from test_api import SLOW


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def settled(c: httpx.AsyncClient, rid: str, timeout_s: float = 40) -> dict:
    for _ in range(int(timeout_s / 0.25)):
        got = (await c.get(f"/v1/responses/{rid}")).json()
        if got["status"] not in ("queued", "in_progress"):
            return got
        await asyncio.sleep(0.25)
    raise AssertionError(f"{rid} is still running")


async def opened(stream) -> tuple[str, object]:
    """The response id from the stream's first event, and the line iterator, to hold: dropping it
    would close the connection, which is the very thing this test does on purpose later."""
    lines = stream.aiter_lines()
    while True:
        line = await lines.__anext__()
        if line.startswith("data: "):
            return json.loads(line[6:])["response"]["id"], lines


async def running(c: httpx.AsyncClient, rid: str) -> None:
    """Until a worker has the turn and the package has had a moment to install its traps."""
    for _ in range(400):
        if (await c.get(f"/v1/responses/{rid}/detail")).json()["status_raw"] == "in_progress":
            await asyncio.sleep(0.8)
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{rid} never started")


def test_closing_the_request_stops_a_foreground_turn_and_spares_a_background_one(tmp_path, vault, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)                             # the worker and the client reach this server directly
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    settings = Settings(data_dir=tmp_path / "data", host="127.0.0.1", port=port, public_url=url, signup="open", join_token="jt", deployer_key="hgd_x")
    app = create_app(Service(settings, vault))
    worker = Worker(url, FakeNode(), tmp_path / "cache", name="w1", slots=2, version="t", join_token="jt")

    async def go():
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        working = asyncio.create_task(worker.run())
        try:
            async with httpx.AsyncClient(base_url=url, timeout=60) as anon:
                me = (await anon.post("/auth/signup", json={"email": "chase@acme.test", "password": "password1", "name": "Chase"})).json()
                key = (await anon.post(f"/v1/members/{me['current']['member']}/keys", json={})).json()["secret"]
            headers = {"Authorization": f"Bearer {key}"}
            async with httpx.AsyncClient(base_url=url, headers=headers, timeout=60) as c:
                assert (await c.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-responses", "key": "sk-test"})).status_code == 200
                r = await c.post("/v1/harnesses/slow", json={"archive": base64.b64encode(archive(files={"request.sh": SLOW})).decode()})
                assert r.status_code == 200, r.text
                # a streamed foreground turn: the client closes the stream once the turn runs
                async with c.stream("POST", "/v1/responses", json={"model": "slow", "input": "go", "stream": True}) as s:
                    rid, held = await opened(s)
                    await running(c, rid)
                    assert (await c.get(f"/v1/responses/{rid}")).json()["status"] == "in_progress"   # open: it runs
                del held                                                    # the stream is closed: nobody waits
                final = await settled(c, rid)
                assert final["status"] == "cancelled" and final["metadata"]["incomplete_reason"] == "client_disconnected", final
                assert final["output"][0]["content"][0]["text"].strip().endswith("stopping")   # SIGTERM reached the package: a graceful stop
                sid = final["metadata"]["session_id"]
                # a blocking foreground turn the client abandons
                abandoned = httpx.AsyncClient(base_url=url, headers=headers, timeout=60)
                task = asyncio.create_task(abandoned.post("/v1/responses", json={"model": "slow", "input": "go", "previous_response_id": rid}))
                for _ in range(200):
                    s2 = (await c.get(f"/v1/sessions/{sid}")).json()
                    if len(s2["responses"]) == 2:
                        break
                    await asyncio.sleep(0.05)
                await running(c, s2["responses"][-1])
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await abandoned.aclose()
                rid2 = s2["responses"][-1]
                final2 = await settled(c, rid2)
                assert final2["status"] == "cancelled" and final2["metadata"]["incomplete_reason"] == "client_disconnected", final2
                # a background turn: a follower closing its stream is not the turn's end
                bg = (await c.post("/v1/responses", json={"model": "slow", "input": "go", "background": True, "previous_response_id": rid2})).json()
                await running(c, bg["id"])
                async with c.stream("GET", f"/v1/responses/{bg['id']}?stream=true") as s:
                    _, held = await opened(s)
                del held
                await asyncio.sleep(7)                                      # longer than a worker heartbeat
                assert (await c.get(f"/v1/responses/{bg['id']}")).json()["status"] == "in_progress"
                cancelled = (await c.post(f"/v1/responses/{bg['id']}/cancel")).json()
                assert cancelled["status"] == "cancelled" and cancelled["metadata"]["incomplete_reason"] == "cancelled"
                trace = [t["kind"] for t in (await c.get(f"/v1/sessions/{sid}/trace")).json()["data"]]
                assert trace == ["request", "disconnect", "response", "request", "disconnect", "response", "request", "cancel", "response"]
        finally:
            worker.stopping = True
            working.cancel()
            await asyncio.gather(working, return_exceptions=True)
            server.should_exit = True
            await serving
    asyncio.run(go())
