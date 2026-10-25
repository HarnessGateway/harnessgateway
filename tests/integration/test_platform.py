"""End-to-end tests against a running stack (api, gateway and a node with gVisor), through the OpenAI SDK.

    HG_TEST_API=http://127.0.0.1:8000 HG_TEST_OPERATOR_KEY=... HG_TEST_DATABASE_URL=postgresql://<app role>@.../hg \\
        pytest tests/integration -v

Each run creates its own organizations, so it is safe against a shared development stack.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import time
import uuid

import httpx
import pytest
from openai import APIStatusError, OpenAI

API = os.environ.get("HG_TEST_API", "")
OPERATOR = os.environ.get("HG_TEST_OPERATOR_KEY", "")
pytestmark = pytest.mark.skipif(not (API and OPERATOR), reason="needs HG_TEST_API and HG_TEST_OPERATOR_KEY")


def admin(method: str, path: str, body: dict | None = None) -> dict:
    r = httpx.request(method, f"{API}/admin/v1{path}", json=body, headers={"Authorization": f"Bearer {OPERATOR}"},
                      timeout=60)
    r.raise_for_status()
    return r.json()


def new_org(limits: dict | None = None) -> tuple[str, str]:
    name = f"it-{uuid.uuid4().hex[:10]}"
    doc = admin("POST", "/organizations", {"name": name, "owner_email": f"{name}@test.local", "limits": limits or {}})
    return doc["organization"]["id"], doc["api_key"]


def client(key: str) -> OpenAI:
    return OpenAI(base_url=f"{API}/v1", api_key=key, timeout=300, max_retries=0)


def http(key: str) -> httpx.Client:
    return httpx.Client(base_url=f"{API}/v1", headers={"Authorization": f"Bearer {key}"}, timeout=300)


def package(name: str, request_sh: str, run: dict | None = None) -> bytes:
    files = {"harness.json": json.dumps({"contract": 1, "name": name, "version": "1", "run": run or {"timeout_s": 120}}),
             "install.sh": "#!/bin/bash\nmkdir -p \"$HG_TOOLS/bin\"\n", "request.sh": "#!/bin/bash\n" + request_sh}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for path, text in files.items():
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(text.encode()), 0o755
            tar.addfile(info, io.BytesIO(text.encode()))
    return raw.getvalue()


def install(key: str, name: str, request_sh: str, run: dict | None = None) -> None:
    r = http(key).post("/harnesses", files={"package": (f"{name}.tgz", package(name, request_sh, run))})
    assert r.status_code == 200, r.text


def wait_final(c: OpenAI, rid: str, timeout: float = 120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.responses.retrieve(rid)
        if r.status not in ("queued", "in_progress"):
            return r
        time.sleep(1)
    raise AssertionError(f"{rid} did not finish")


@pytest.fixture(scope="module")
def org():
    return new_org()


def test_operator_routes_need_the_operator_key():
    assert httpx.get(f"{API}/admin/v1/organizations", timeout=30).status_code == 401
    assert httpx.get(f"{API}/admin/v1/organizations", headers={"Authorization": "Bearer nope"}, timeout=30).status_code == 403


def test_invite_signup():
    invite = admin("POST", "/invites", {"label": "it", "uses": 1})
    name = f"it-{uuid.uuid4().hex[:8]}"
    r = httpx.post(f"{API}/v1/signup", json={"invite_code": invite["code"], "organization": name, "email": "a@b.io"}, timeout=60)
    assert r.status_code == 200 and r.json()["api_key"].startswith("sk-hg-")
    again = httpx.post(f"{API}/v1/signup", json={"invite_code": invite["code"], "organization": name + "x", "email": "a@b.io"},
                       timeout=60)
    assert again.status_code == 403
    harnesses = http(r.json()["api_key"]).get("/harnesses").json()["data"]
    assert "shell" in {h["name"] for h in harnesses}


def test_streaming_events_are_numbered_and_enveloped(org):
    c = client(org[1])
    events = list(c.responses.create(model="shell", input="echo streamed", stream=True))
    assert [e.sequence_number for e in events] == list(range(len(events)))
    assert events[0].type == "response.created" and events[1].type == "response.in_progress"
    assert events[-1].type == "response.completed" and events[-1].response.output_text.strip() == "streamed"


def test_continuation_keeps_the_workspace_and_store_false_ends_it(org):
    c = client(org[1])
    r1 = c.responses.create(model="shell", input="echo kept > note.txt && echo ok")
    r2 = c.responses.create(model="shell", input="cat note.txt", previous_response_id=r1.id)
    assert r2.output_text.strip() == "kept" and r2.metadata["session_id"] == r1.metadata["session_id"]
    eph = c.responses.create(model="shell", input="echo once", store=False)
    with pytest.raises(APIStatusError) as e:
        c.responses.create(model="shell", input="echo again", previous_response_id=eph.id)
    assert e.value.status_code == 400


def test_tenants_are_isolated(org):
    c_a = client(org[1])
    r = c_a.responses.create(model="shell", input="echo private")
    sid = r.metadata["session_id"]
    f = c_a.files.create(file=("secret.txt", io.BytesIO(b"secret")), purpose="user_data")
    _, key_b = new_org()
    c_b = client(key_b)
    for call in (lambda: c_b.responses.retrieve(r.id), lambda: c_b.containers.retrieve(sid),
                 lambda: c_b.files.retrieve(f.id), lambda: c_b.containers.files.list(sid),
                 lambda: c_b.responses.create(model="shell", input="echo x", previous_response_id=r.id)):
        with pytest.raises(APIStatusError) as e:
            call()
        assert e.value.status_code == 404
    assert c_b.containers.list().data == [] and c_b.files.list().data == []


def test_row_level_security_in_the_database(org):
    url = os.environ.get("HG_TEST_DATABASE_URL")
    if not url:
        pytest.skip("needs HG_TEST_DATABASE_URL (the application role)")
    import psycopg
    client(org[1]).responses.create(model="shell", input="echo rls")
    with psycopg.connect(url) as cx:
        assert cx.execute("SELECT count(*) FROM turns").fetchone()[0] == 0          # no scope: nothing
        cx.execute("SELECT set_config('hg.org', %s, false)", (org[0],))
        orgs = {r[0] for r in cx.execute("SELECT DISTINCT org_id FROM turns").fetchall()}
        assert orgs == {org[0]}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute("INSERT INTO providers (id, org_id, name, api, base_url, sealed_key) VALUES "
                       "('prv_x', 'org_other', 'x', 'openai', 'https://x', 'v1:x')")


def test_files_and_container_files(org):
    c = client(org[1])
    f = c.files.create(file=("in.txt", io.BytesIO(b"from files")), purpose="user_data")
    r = c.responses.create(model="shell", input=[{"role": "user", "content": [
        {"type": "input_text", "text": "cat /run/hg/files/*/in.txt"}, {"type": "input_file", "file_id": f.id}]}])
    assert r.output_text == "from files"
    sid = r.metadata["session_id"]
    c.containers.files.create(sid, file=("data.csv", io.BytesIO(b"a,b\n")))
    r2 = c.responses.create(model="shell", input="cat data.csv", previous_response_id=r.id)
    assert r2.output_text == "a,b\n"
    paths = {x.path for x in c.containers.files.list(sid).data}
    assert "/workspace/data.csv" in paths
    target = next(x for x in c.containers.files.list(sid).data if x.path == "/workspace/data.csv")
    assert c.containers.files.content.retrieve(target.id, container_id=sid).read() == b"a,b\n"


def test_background_cancel(org):
    c = client(org[1])
    bg = c.responses.create(model="shell", input="sleep 300", background=True)
    deadline = time.time() + 120
    while c.responses.retrieve(bg.id).status == "queued" and time.time() < deadline:
        time.sleep(1)
    c.responses.cancel(bg.id)
    final = wait_final(c, bg.id, 90)
    assert final.status == "cancelled" and final.metadata["hg_reason"] == "cancelled"


def test_idempotency(org):
    h = http(org[1])
    body = {"model": "shell", "input": "echo idem"}
    key = uuid.uuid4().hex
    a = h.post("/responses", json=body, headers={"Idempotency-Key": key}).json()
    b = h.post("/responses", json=body, headers={"Idempotency-Key": key}).json()
    assert a["id"] == b["id"]
    assert h.post("/responses", json={**body, "input": "echo other"}, headers={"Idempotency-Key": key}).status_code == 409


def test_routing_errors(org):
    h = http(org[1])
    assert h.post("/responses", json={"model": "no-such-harness", "input": "x"}).status_code == 404
    assert h.post("/responses", json={"model": "Bad Model", "input": "x"}).status_code == 400
    assert h.post("/responses", json={"model": "shell", "input": 5}).status_code == 400
    assert h.post("/responses", json={"model": "shell/someprovider", "input": "x"}).status_code == 400


def test_platform_decided_endings(org):
    key = org[1]
    c = client(key)
    install(key, "it-badevent", 'echo \'{"type":"response.output_text.delta","delta":"x"}\'\n')
    install(key, "it-noterminal", "echo just a log line\n")
    install(key, "it-slow", "sleep 600\n", run={"timeout_s": 5, "stop_grace_s": 2})
    bad = c.responses.create(model="it-badevent", input="go")
    assert bad.status == "failed" and bad.metadata["hg_reason"] == "invalid_output" and "delta" in bad.error.message
    none = c.responses.create(model="it-noterminal", input="go")
    assert none.status == "failed" and none.metadata["hg_reason"] == "no_terminal_event"
    slow = c.responses.create(model="it-slow", input="go")
    assert slow.status == "incomplete" and slow.metadata["hg_reason"] == "timeout"


def test_package_upload_is_validated(org):
    h = http(org[1])
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        info = tarfile.TarInfo("harness.json")
        data = b'{"contract": 1, "name": "x", "version": "1", "surprise": 1}'
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    r = h.post("/harnesses", files={"package": ("x.tgz", raw.getvalue())})
    assert r.status_code == 400


def test_rate_limit():
    org_id, key = new_org({"requests_per_minute": 2})
    h = http(key)
    codes = [h.post("/responses", json={"model": "shell", "input": "true", "background": True}).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429


def test_usage_is_metered(org):
    c = client(org[1])
    c.responses.create(model="shell", input="echo meter")
    deadline = time.time() + 90
    while time.time() < deadline:
        lines = {x["meter"]: x for x in http(org[1]).get("/usage").json()["lines"]}
        if "compute.vcpu_seconds" in lines and "network.egress_bytes" in lines:
            assert float(lines["compute.vcpu_seconds"]["charge_usd"]) >= 0
            return
        time.sleep(5)
    raise AssertionError(f"no compute usage recorded: {lines}")
