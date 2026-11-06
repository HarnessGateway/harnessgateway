"""The HTTP surface over the services (SPEC §14): credentials and the error envelope, the Responses routes, uploads,
the box routes as the runner calls them, the door's, the platform scope's, and the body guard."""
import json

import httpx
import pytest
import pytest_asyncio

from harnessgateway.api.app import create_app

from .conftest import SHELL, pack, said, snapshot

BODY = {"model": "shell", "input": "echo hi", "background": True}


@pytest_asyncio.fixture
async def api(plat):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(plat)), base_url="http://api.test") as c:
        yield c


@pytest_asyncio.fixture
async def key(plat, org):
    return (await plat.keys.create(org.admin, "test", "admin", False, None))["key"]


def auth(secret):
    return {"Authorization": f"Bearer {secret}"}


async def test_credentials_and_the_error_envelope(api, key, org, shell):
    r = await api.get("/v1/models")
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    r = await api.get("/v1/models", headers=auth("hg_nonsense"))
    assert r.status_code == 401 and "unknown" in r.json()["error"]["message"]
    r = await api.get("/v1/models", headers=auth(key))
    assert r.status_code == 200 and [m["id"] for m in r.json()["data"]] == ["shell"]
    r = await api.get("/v1/models/nope", headers=auth(key))
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    r = await api.post("/v1/responses", headers=auth(key), content=b"not json")
    assert r.status_code == 400 and r.json()["error"]["message"] == "the request body is JSON"
    r = await api.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"]
    r = await api.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ok": True, "database": True, "redis": True}
    r = await api.get("/v1/platform", headers=auth(key))
    assert r.json()["object"] == "platform"


async def test_a_background_turn_through_the_api(api, key, org, shell):
    r = await api.post("/v1/responses", headers={**auth(key), "Idempotency-Key": "api-" + org.id}, json=BODY)
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["object"] == "response"
    rid, sid = r.json()["id"], r.json()["metadata"]["session_id"]
    again = await api.post("/v1/responses", headers={**auth(key), "Idempotency-Key": "api-" + org.id}, json=BODY)
    assert again.json()["id"] == rid
    r = await api.get(f"/v1/responses/{rid}/input_items", headers=auth(key))
    assert r.json()["data"][0]["content"][0]["text"] == "echo hi"
    r = await api.get(f"/v1/containers/{sid}", headers=auth(key))
    assert r.json()["metadata"]["state"] == "queued"
    r = await api.get("/v1/containers", headers=auth(key))
    assert [c["id"] for c in r.json()["data"]] == [sid]
    r = await api.post(f"/v1/responses/{rid}/cancel", headers=auth(key))
    assert r.json()["status"] == "cancelled"
    r = await api.get(f"/v1/responses/{rid}?stream=true", headers=auth(key))
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[0]["type"] == "response.created" and events[-1]["type"] == "response.failed"
    r = await api.delete(f"/v1/responses/{rid}", headers=auth(key))
    assert r.json() == {"id": rid, "object": "response", "deleted": True}
    r = await api.delete(f"/v1/containers/{sid}", headers=auth(key))
    assert r.json()["deleted"] is True


async def test_install_configure_and_read_a_package_over_http(api, key, org):
    r = await api.post("/v1/harnesses", headers=auth(key), files={"package": ("shell.tar.gz", pack(SHELL))})
    assert r.status_code == 200 and r.json()["name"] == "shell" and r.json()["install"]["status"] == "pending"
    r = await api.get("/v1/harnesses/shell/install", headers=auth(key))
    assert r.json()["object"] == "install" and r.json()["status"] == "pending"
    r = await api.get("/v1/harnesses/shell/files/harness.json", headers=auth(key))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    r = await api.patch("/v1/harnesses/shell", headers=auth(key), json={"default_model": "x", "nope": 1})
    assert r.status_code == 400 and r.json()["error"]["param"] == "nope"
    r = await api.put("/v1/egress", headers=auth(key), json={"egress": ["pypi.org"]})
    assert r.json()["egress"] == ["pypi.org"]
    r = await api.post("/v1/keys", headers=auth(key), json={"label": "app", "run_only": True})
    assert r.json()["key"].startswith("hg_") and r.json()["run_only"] is True
    r = await api.get("/v1/organization", headers=auth(r.json()["key"]))
    assert r.json()["principal"]["run_only"] is True and r.json()["limits"]["standby_s"] == 1
    r = await api.get("/v1/audit", headers=auth(key))
    assert [a["action"] for a in r.json()["data"]][:2] == ["key.create", "egress.update"]


async def test_a_turn_runs_through_the_box_routes(api, key, plat, org, ready):
    r = await api.post("/v1/responses", headers=auth(key), json=BODY)
    rid, sid = r.json()["id"], r.json()["metadata"]["session_id"]
    async with plat.db.platform() as tx:
        box_id = await tx.value("SELECT id FROM boxes WHERE session_id = %s", sid)
    spec = await plat.boxes.to_launch(box_id)                                     # the worker's part
    await plat.boxes.launched(box_id, "container")
    h = auth(spec["token"])
    r = await api.get(f"/internal/boxes/{box_id}", headers=auth("hgb_wrong"))
    assert r.status_code == 401
    r = await api.get(f"/internal/boxes/{box_id}", headers=h)
    assert r.status_code == 200 and r.json()["kind"] == "session"
    archive = await api.get(f"/internal/boxes/{box_id}/objects/{r.json()['package']['key']}", headers=h)
    assert archive.status_code == 200 and len(archive.content) > 100
    r = await api.get(f"/internal/boxes/{box_id}/objects/orgs/other/x", headers=h)
    assert r.status_code == 403
    assert (await api.post(f"/internal/boxes/{box_id}/ready", headers=h)).json() == {"ok": True}
    r = await api.get(f"/internal/boxes/{box_id}/next?wait=0", headers=h)
    assert r.status_code == 200 and r.json()["turn_id"] == rid
    r = await api.post(f"/internal/boxes/{box_id}/turns/{rid}/heartbeat", headers=h)
    assert r.json() == {"cancel": False, "stop": False}
    r = await api.post(f"/internal/boxes/{box_id}/turns/{rid}/events", headers=h, json={"batch": 1, "lines": said("hey")})
    assert r.json() == {"concluded": True, "stop": False}
    r = await api.post(f"/internal/boxes/{box_id}/turns/{rid}/finish", headers=h,
                       json={"outcome": "exited", "exit_code": 0, "cpu_seconds": 1})
    assert r.json() == {"persist": True}
    manifest, blobs = snapshot({"workspace/a.txt": b"A"})
    digest = next(iter(blobs))
    r = await api.post(f"/internal/boxes/{box_id}/blobs", headers=h, json={"hashes": [digest]})
    assert r.json() == {"missing": [digest]}
    r = await api.put(f"/internal/boxes/{box_id}/blobs/{digest}", headers=h, content=b"B")
    assert r.status_code == 400 and "do not match" in r.json()["error"]["message"]
    r = await api.put(f"/internal/boxes/{box_id}/blobs/{digest}", headers=h, content=b"A")
    assert r.json() == {"hash": digest, "bytes": 1}
    r = await api.put(f"/internal/boxes/{box_id}/turns/{rid}/manifest", headers=h, content=manifest.dumps().encode())
    assert r.json()["files"] == 1 and r.json()["bytes"] == 1
    r = await api.get(f"/internal/boxes/{box_id}/blobs/{digest}", headers=h)
    assert r.content == b"A"
    r = await api.put(f"/internal/boxes/{box_id}/turns/{rid}/logs/stdout", headers=h, content=b"the whole log")
    assert r.json() == {"ok": True}
    r = await api.get(f"/internal/boxes/{box_id}/next?wait=0", headers=h)
    assert r.status_code == 204
    assert (await api.post(f"/internal/boxes/{box_id}/bye", headers=h)).json() == {"ok": True}
    r = await api.get(f"/v1/responses/{rid}", headers=auth(key))
    assert r.json()["status"] == "completed" and r.json()["output"][-1]["content"][0]["text"] == "hey"
    r = await api.get(f"/v1/containers/{sid}/files", headers=auth(key))
    fid = r.json()["data"][0]["id"]
    r = await api.get(f"/v1/containers/{sid}/files/{fid}/content", headers=auth(key))
    assert r.content == b"A" and "a.txt" in r.headers["content-disposition"]
    r = await api.post(f"/v1/containers/{sid}/files", headers=auth(key), files={"file": ("b.txt", b"BB")},
                       data={"path": "sub/b.txt"})
    assert r.json()["path"] == "/workspace/sub/b.txt" and r.json()["source"] == "user"
    r = await api.get(f"/v1/traces/{rid}", headers=auth(key))
    assert {t["kind"] for t in r.json()["records"]} == {"harness"}
    r = await api.get(f"/v1/containers/{sid}/traces", headers=auth(key))
    assert r.status_code == 200
    r = await api.get("/v1/usage?group_by=session", headers=auth(key))
    assert r.json()["data"][sid]["agent_seconds"] > 0


async def test_the_door_and_the_operator(api, key, plat, org, ready):
    r = await api.post("/v1/responses", headers=auth(key), json=BODY)
    sid = r.json()["metadata"]["session_id"]
    async with plat.db.platform() as tx:
        box_id = await tx.value("SELECT id FROM boxes WHERE session_id = %s", sid)
    spec = await plat.boxes.to_launch(box_id)
    r = await api.post("/internal/door/policy", headers=auth("hgd_wrong"), json={"token": spec["token"]})
    assert r.status_code == 401
    r = await api.post("/internal/door/policy", headers=auth("hgd_test"), json={"token": spec["token"]})
    assert r.status_code == 200 and r.json()["platform"] == ["api.test:8000"]
    r = await api.post("/internal/door/records", headers=auth("hgd_test"), json={"token": spec["token"], "records": []})
    assert r.json() == {"ok": True}
    r = await api.get("/internal/health", headers=auth(key))
    assert r.status_code == 403                                                  # an organization's admin is not the operator
    r = await api.get("/internal/health", headers=auth("hgo_test"))
    assert r.json()["database"]["ok"] and r.json()["boxes"]["session.starting"] >= 1
    r = await api.get("/internal/metrics", headers=auth("hgo_test"))
    assert r.text.startswith("hg_up")
    r = await api.get("/internal/boxes", headers=auth("hgo_test"))
    assert box_id in {b["id"] for b in r.json()["data"]}
    r = await api.patch(f"/internal/orgs/{org.id}", headers=auth("hgo_test"), json={"limits": {"standby_s": 7}})
    assert r.json()["limits"]["standby_s"] == 7
    r = await api.patch(f"/internal/orgs/{org.id}", headers=auth("hgo_test"), json={"capacity": {}})
    assert r.status_code == 400
    r = await api.post(f"/internal/boxes/{box_id}/stop", headers=auth("hgo_test"))
    assert r.json()["status"] == "gone"
    r = await api.get("/internal/audit", headers=auth("hgo_test"))
    assert [a["action"] for a in r.json()["data"]][:2] == ["box.stop", "org.configure"]
    assert (await api.get("/internal/config", headers=auth("hgo_test"))).json()["settings"]["door"] == "hg-door:3128"
    assert (await api.get("/internal/versions", headers=auth("hgo_test"))).json()["platform"].startswith("3.")
    assert (await api.get("/internal/usage?group_by=org", headers=auth("hgo_test"))).status_code == 200


async def test_sign_in_sso_status_and_the_body_guard(api, key, plat, org):
    r = await api.get("/v1/auth/sso")
    assert r.status_code == 200 and r.json()["enabled"] is False
    invited = await plat.accounts.create(org.id, org.admin.actor, f"api-{org.id[-6:]}@example.test", "A", "admin")
    token = invited["invitation"].split("token=")[1]
    r = await api.post("/v1/auth/accept", json={"token": token, "password": "a-long-enough-password"})
    assert r.status_code == 200 and r.json()["token"].startswith("hgl_")
    signed = r.json()["token"]
    r = await api.get("/v1/auth/me", headers=auth(signed))
    assert r.json()["role"] == "admin" and r.json()["organization"]["id"] == org.id
    r = await api.get("/v1/sso", headers=auth(signed))
    assert r.status_code == 404
    r = await api.get("/v1/auth/me", headers=auth(key))
    assert r.status_code == 401 and "sign-in token" in r.json()["error"]["message"]
    r = await api.post("/v1/auth/logout", headers=auth(signed))
    assert r.json()["deleted"] is True
    r = await api.post("/v1/keys", headers={**auth(key), "Content-Type": "application/json"},
                       content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413


@pytest.mark.parametrize("path", ["/v1/responses/resp_" + "0" * 32, "/v1/containers/sess_" + "0" * 32])
async def test_unknown_ids_are_not_found(api, key, shell, path):
    r = await api.get(path, headers=auth(key))
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"
