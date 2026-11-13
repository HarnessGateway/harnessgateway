"""Spec §14, §16, §22.2: the management API. Every endpoint, who may call it, and what it refuses."""

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from harnessgateway import packaging
from harnessgateway.stores import Stores
from tests.helpers import SHELL, variant
from tests.integration.api import Running, running

DEEPSEEK = {"name": "deepseek", "apis": {"anthropic": "https://api.deepseek.com/anthropic"},
            "default_model": "deepseek-flash", "api_key": "the-real-key"}


@pytest.fixture
async def api(stores: Stores, org: str, tmp_path: Path) -> AsyncIterator[Running]:
    async with running(stores, org, tmp_path) as api:
        yield api


# ── providers ───────────────────────────────────────────────────────────────────────────────────────

async def test_providers_are_made_read_changed_and_deleted_and_the_key_is_never_shown(api: Running) -> None:
    made = await api.admin.post("/v1/providers", json=DEEPSEEK)
    assert made.status_code == 200
    assert made.json()["has_key"] is True
    assert "the-real-key" not in made.text
    assert (await api.developer.get("/v1/providers/deepseek")).json()["default_model"] == "deepseek-flash"
    listed = (await api.developer.get("/v1/providers")).json()
    assert (listed["object"], [p["name"] for p in listed["data"]], listed["has_more"], listed["last_id"]) == (
        "list", ["deepseek"], False, "deepseek")
    assert "the-real-key" not in str(listed)

    changed = await api.admin.patch("/v1/providers/deepseek", json={"default_model": "deepseek-v4-pro",
                                                                    "api_key": None})
    assert (changed.json()["default_model"], changed.json()["has_key"]) == ("deepseek-v4-pro", False)
    assert (await api.admin.delete("/v1/providers/deepseek")).json() == {"name": "deepseek", "deleted": True}
    assert (await api.developer.get("/v1/providers/deepseek")).status_code == 404


async def test_what_is_wrong_with_a_provider_is_named(api: Running) -> None:
    bad = await api.admin.post("/v1/providers", json={"name": "x", "apis": {}})
    assert bad.status_code == 400
    assert "apis" in bad.json()["error"]["message"]
    await api.admin.post("/v1/providers", json=DEEPSEEK)
    again = await api.admin.post("/v1/providers", json=DEEPSEEK)
    assert (again.status_code, again.json()["error"]["type"]) == (409, "conflict")
    assert (await api.admin.post("/v1/providers", content=b"[1]")).status_code == 400
    assert (await api.admin.patch("/v1/providers/absent", json={"default_model": "m"})).status_code == 404


async def test_lists_are_paged_with_limit_and_after(api: Running) -> None:
    for name in ("a", "b", "c"):
        await api.admin.post("/v1/providers", json={**DEEPSEEK, "name": name})
    first = (await api.developer.get("/v1/providers", params={"limit": 2})).json()
    assert ([p["name"] for p in first["data"]], first["has_more"], first["last_id"]) == (["a", "b"], True, "b")
    rest = (await api.developer.get("/v1/providers", params={"limit": 2, "after": "b"})).json()
    assert ([p["name"] for p in rest["data"]], rest["has_more"]) == (["c"], False)
    assert (await api.developer.get("/v1/providers", params={"limit": 0})).status_code == 400
    assert (await api.developer.get("/v1/providers", params={"limit": 101})).status_code == 400
    assert (await api.developer.get("/v1/providers", params={"after": "zzz"})).status_code == 400


# ── environments ────────────────────────────────────────────────────────────────────────────────────

async def test_environments_are_made_read_changed_and_deleted_and_secret_values_are_never_shown(api: Running) -> None:
    made = await api.admin.post("/v1/environments", json={
        "name": "secure", "memory_mb": 1024, "tmp_mb": 256, "network": {"mode": "limited", "allow": ["api.x.com"]},
        "secrets": {"TOKEN": {"hosts": ["api.x.com"], "value": "the-secret"}}})
    assert made.status_code == 200
    assert made.json()["secrets"] == {"TOKEN": {"hosts": ["api.x.com"], "header": None, "prefix": ""}}
    assert "the-secret" not in made.text
    names = [e["name"] for e in (await api.developer.get("/v1/environments")).json()["data"]]
    assert names == ["default", "secure"]
    changed = await api.admin.patch("/v1/environments/secure", json={"cpus": 4})
    assert (changed.json()["cpus"], changed.json()["memory_mb"]) == (4, 1024)
    assert "the-secret" not in (await api.developer.get("/v1/environments/secure")).text

    assert (await api.admin.delete("/v1/environments/default")).status_code == 400
    assert (await api.admin.delete("/v1/environments/secure")).json() == {"name": "secure", "deleted": True}
    bad = await api.admin.post("/v1/environments", json={"name": "bad", "cpus": 1000})
    assert (bad.status_code, "cpus" in bad.json()["error"]["message"]) == (400, True)


# ── harnesses ───────────────────────────────────────────────────────────────────────────────────────

async def test_the_catalog_offers_what_ships_and_a_package_is_added_from_it(api: Running) -> None:
    offered = (await api.developer.get("/v1/catalog")).json()["data"]
    assert [(p["name"], p["installed_version"]) for p in offered] == [("shell", None)]
    added = await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    assert added.status_code == 200
    assert (added.json()["name"], added.json()["version"], added.json()["missing"]) == ("shell", "1.0.0", [])
    assert (await api.developer.get("/v1/catalog")).json()["data"][0]["installed_version"] == "1.0.0"
    assert (await api.developer.post("/v1/harnesses", json={"catalog": "absent"})).status_code == 404

    shown = await api.installed("shell")                      # it is installed for the default environment at once
    assert [install["status"] for install in shown["installs"]] == ["ready"]
    answer = await api.openai.responses.create(model="shell", input="echo ready")
    assert answer.output_text == "ready\n"


async def test_a_package_is_uploaded_its_settings_set_and_its_installs_shown(api: Running, tmp_path: Path) -> None:
    archive = variant(tmp_path, "demo", settings={"EFFORT": {"default": "medium"}},
                      secrets={"GITHUB_TOKEN": {"hosts": ["api.github.com"]}})
    added = await api.developer.post("/v1/harnesses", files={"package": ("demo.tar.gz", io.BytesIO(archive))})
    assert added.status_code == 200, added.text
    assert added.json()["settings"] == {"EFFORT": "medium"}

    changed = await api.developer.patch("/v1/harnesses/demo", json={"settings": {"EFFORT": "high"}, "model": "m"})
    assert (changed.json()["settings"], changed.json()["model"]) == ({"EFFORT": "high"}, "m")
    refused = await api.developer.patch("/v1/harnesses/demo", json={"secrets": {"GITHUB_TOKEN": "the-token"}})
    assert (refused.status_code, refused.json()["error"]["type"]) == (403, "insufficient_permissions")
    by_admin = await api.admin.patch("/v1/harnesses/demo", json={"secrets": {"GITHUB_TOKEN": "the-token"}})
    assert by_admin.json()["secrets_set"] == ["GITHUB_TOKEN"]
    assert "the-token" not in by_admin.text

    await api.platform.settle()
    installs = (await api.developer.get("/v1/harnesses/demo/installs")).json()["data"]
    assert [(i["status"], i["image_id"]) for i in installs] == [("ready", "sha256:sandbox:test")]
    assert "log" in installs[0]
    await api.admin.post("/v1/environments", json={"name": "other", "image": "other:image"})
    asked = await api.developer.post("/v1/harnesses/demo/install", json={"environment": "other"})
    assert asked.json() == {"name": "demo", "environment": "other", "status": "pending"}
    shown = await api.installed("demo")
    assert sorted(i["image_id"] for i in shown["installs"]) == ["sha256:other:image", "sha256:sandbox:test"]

    assert [h["name"] for h in (await api.developer.get("/v1/harnesses")).json()["data"]] == ["demo"]
    assert (await api.developer.delete("/v1/harnesses/demo")).json() == {"name": "demo", "deleted": True}
    assert (await api.developer.get("/v1/harnesses/demo")).status_code == 404


async def test_a_package_that_is_not_valid_is_refused_naming_what_is_wrong(api: Running) -> None:
    garbage = await api.developer.post("/v1/harnesses", files={"package": ("x.tar.gz", io.BytesIO(b"not one"))})
    assert (garbage.status_code, "gzip-compressed tar" in garbage.json()["error"]["message"]) == (400, True)
    nothing = await api.developer.post("/v1/harnesses", json={})
    assert (nothing.status_code, "catalog" in nothing.json()["error"]["message"]) == (400, True)
    assert (await api.developer.get("/v1/harnesses")).json()["data"] == []


async def test_a_package_an_agent_wrote_in_a_session_is_added_from_its_work_folder(api: Running) -> None:
    await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    manifest = '{"contract": 1, "name": "built", "version": "0.1.0", "apis": []}'
    built = await api.openai.responses.create(model="shell", input=(
        f"mkdir -p pkg && cd pkg && echo '{manifest}' > harness.json && printf '#!/bin/sh\\n' > install && "
        "cp $HG_HARNESS/run run && chmod +x install run"))
    assert built.status == "completed"
    assert built.metadata is not None
    session = built.metadata["hg_session"]

    added = await api.developer.post("/v1/harnesses", json={"session": session, "path": "pkg"})
    assert added.status_code == 200, added.text
    assert (added.json()["name"], added.json()["version"]) == ("built", "0.1.0")
    assert (await api.openai.responses.create(model="built", input="echo from the built one")).output_text == (
        "from the built one\n")
    missing = await api.developer.post("/v1/harnesses", json={"session": session, "path": "nowhere"})
    assert (missing.status_code, "no folder 'nowhere'" in missing.json()["error"]["message"]) == (400, True)


# ── sessions, turns, traces ─────────────────────────────────────────────────────────────────────────

async def test_sessions_their_turns_their_files_and_a_turn_s_trace(api: Running) -> None:
    await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    first = await api.openai.responses.create(model="shell", input="echo one > a.txt; echo mind > $HOME/.note")
    second = await api.openai.responses.create(model="shell", input="echo two > b.txt; exit 2",
                                               previous_response_id=first.id)
    other = await api.openai.responses.create(model="shell", input="echo other")
    assert first.metadata is not None
    assert other.metadata is not None
    session = first.metadata["hg_session"]

    listed = (await api.developer.get("/v1/sessions")).json()
    assert [s["id"] for s in listed["data"]] == [other.metadata["hg_session"], session]     # most recent first
    one = (await api.developer.get(f"/v1/sessions/{session}")).json()
    assert (one["head_response_id"], one["turns"], one["forked_from_response_id"]) == (second.id, 2, None)
    assert one["bytes"] > 0

    turns = (await api.developer.get(f"/v1/sessions/{session}/turns")).json()["data"]
    assert [(t["id"], t["number"], t["status"], t["reason"], t["harness"]) for t in turns] == [
        (first.id, 1, "completed", None, "shell"), (second.id, 2, "failed", "harness_failed", "shell")]

    files = (await api.developer.get(f"/v1/sessions/{session}/files")).json()["data"]
    assert [f["path"] for f in files] == ["mind/.note", "work/a.txt", "work/b.txt"]
    earlier = (await api.developer.get(f"/v1/sessions/{session}/files", params={"response_id": first.id})).json()
    assert [f["path"] for f in earlier["data"]] == ["mind/.note", "work/a.txt"]
    content = await api.developer.get(f"/v1/sessions/{session}/files/content", params={"path": "work/a.txt"})
    assert content.content == b"one\n"
    assert (await api.developer.get(f"/v1/sessions/{session}/files/content",
                                    params={"path": "work/absent"})).status_code == 404

    trace = (await api.developer.get(f"/v1/responses/{second.id}/trace")).json()
    assert (trace["status"], trace["reason"], trace["code"], trace["session_id"]) == (
        "failed", "harness_failed", "command_failed", session)
    assert [step["name"] for step in trace["steps"]] == ["install and restore", "run", "save"]
    assert all(step["started_at"] and step["ended_at"] for step in trace["steps"])
    assert trace["command"].startswith("local hg-sess_")
    assert trace["connections"] == []
    assert "saved as snap_" in trace["log"]
    assert (await api.developer.get("/v1/sessions/sess_00000000000000000000000000")).status_code == 404


async def test_deleting_a_session_removes_its_turns_its_snapshots_and_its_volumes(api: Running) -> None:
    await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    turn = await api.openai.responses.create(model="shell", input="echo one > a.txt")
    assert turn.metadata is not None
    session = turn.metadata["hg_session"]
    volume = api.platform.driver.volume(f"hg-work-{session}")  # type: ignore[attr-defined]
    assert volume.exists()

    assert (await api.developer.delete(f"/v1/sessions/{session}")).json() == {"id": session, "deleted": True}
    assert (await api.developer.get(f"/v1/sessions/{session}")).status_code == 404
    assert (await api.runner.get(f"/v1/responses/{turn.id}")).status_code == 404
    assert not volume.exists()
    async with api.stores.db.org(api.org) as conn:
        left = await (await conn.execute("SELECT count(*) AS n FROM snapshots WHERE session_id = %s",
                                         [session])).fetchone()
        unused = await (await conn.execute("SELECT count(*) AS n FROM blobs WHERE org_id = %s AND refs > 0",
                                           [api.org])).fetchone()
    assert left == {"n": 0}
    assert unused == {"n": 0}
    assert (await api.developer.delete(f"/v1/sessions/{session}")).status_code == 404


async def test_a_session_with_a_turn_in_progress_is_not_deleted(api: Running) -> None:
    await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    turn = await api.openai.responses.create(model="shell", input="sleep 60", background=True)
    assert turn.metadata is not None
    refused = await api.developer.delete(f"/v1/sessions/{turn.metadata['hg_session']}")
    assert (refused.status_code, "cancel it first" in refused.json()["error"]["message"]) == (409, True)
    await api.openai.responses.cancel(turn.id)


async def test_a_turn_not_to_be_stored_takes_its_session_with_it_when_nothing_else_is_in_it(api: Running) -> None:
    await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    await api.openai.responses.create(model="shell", input="echo once", store=False)
    assert (await api.developer.get("/v1/sessions")).json()["data"] == []
    kept = await api.openai.responses.create(model="shell", input="echo kept")
    await api.openai.responses.create(model="shell", input="echo once", store=False, previous_response_id=kept.id)
    sessions = (await api.developer.get("/v1/sessions")).json()["data"]
    assert [s["turns"] for s in sessions] == [2]                       # the stored turn keeps the session


# ── keys, roles ─────────────────────────────────────────────────────────────────────────────────────

async def test_a_key_is_made_shown_once_listed_and_revoked(api: Running) -> None:
    made = (await api.admin.post("/v1/keys", json={"name": "ci", "role": "developer"})).json()
    assert made["key"].startswith("hg_")
    listed = (await api.admin.get("/v1/keys")).json()["data"]
    assert [k["name"] for k in listed] == ["the admin", "the developer", "the runner", "ci"]
    assert "key" not in listed[-1]
    assert made["key"] not in str(listed)

    ci = api.http("admin")
    ci.headers["Authorization"] = f"Bearer {made['key']}"
    assert (await ci.get("/v1/harnesses")).status_code == 200
    assert (await api.admin.delete(f"/v1/keys/{made['id']}")).json() == {"id": made["id"], "revoked": True}
    assert (await ci.get("/v1/harnesses")).status_code == 401
    await ci.aclose()
    bad = await api.admin.post("/v1/keys", json={"name": "x", "role": "owner"})
    assert (bad.status_code, "role is one of" in bad.json()["error"]["message"]) == (400, True)


@pytest.mark.parametrize(("role", "method", "path", "allowed"), [
    ("runner", "GET", "/v1/platform", True),
    ("runner", "GET", "/v1/models", True),
    ("runner", "GET", "/v1/harnesses", False),
    ("runner", "GET", "/v1/sessions", False),
    ("runner", "GET", "/v1/usage", False),
    ("developer", "GET", "/v1/harnesses", True),
    ("developer", "GET", "/v1/providers", True),
    ("developer", "GET", "/v1/environments", True),
    ("developer", "GET", "/v1/usage", True),
    ("developer", "POST", "/v1/providers", False),
    ("developer", "POST", "/v1/environments", False),
    ("developer", "DELETE", "/v1/environments/default", False),
    ("developer", "GET", "/v1/keys", False),
    ("developer", "POST", "/v1/keys", False),
    ("developer", "GET", "/v1/audit", False),
    ("developer", "GET", "/v1/organization", False),
    ("admin", "GET", "/v1/audit", True),
    ("admin", "GET", "/v1/organization", True),
])
async def test_each_role_may_do_what_the_specification_says_and_no_more(
    api: Running, role: str, method: str, path: str, allowed: bool
) -> None:
    answer = await getattr(api, role).request(method, path, json={} if method == "POST" else None)
    if allowed:
        assert answer.status_code == 200, answer.text
    else:
        assert (answer.status_code, answer.json()["error"]["type"]) == (403, "insufficient_permissions")
        assert f"this key's role is {role}" in answer.json()["error"]["message"]


# ── usage, audit, platform, organization ────────────────────────────────────────────────────────────

async def test_usage_is_summed_by_what_is_asked(api: Running) -> None:
    await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    for command in ("echo one", "echo two", "exit 1"):
        await api.openai.responses.create(model="shell", input=command)
    by_harness = (await api.developer.get("/v1/usage")).json()
    assert by_harness["group_by"] == "harness"
    [row] = by_harness["data"]
    assert (row["group"], row["turns"], row["input_tokens"], row["bytes_up"]) == ("shell", 3, 0, 0)
    assert row["snapshot_bytes"] >= 0
    by_day = (await api.developer.get("/v1/usage", params={"group_by": "day"})).json()["data"]
    assert len(by_day) == 1
    assert len(by_day[0]["group"]) == 10
    none = (await api.developer.get("/v1/usage", params={"start": "2020-01-01T00:00:00Z",
                                                         "end": "2020-02-01T00:00:00Z"})).json()
    assert none["data"] == []
    assert (await api.developer.get("/v1/usage", params={"group_by": "user"})).status_code == 400
    assert (await api.developer.get("/v1/usage", params={"start": "yesterday"})).status_code == 400


async def test_every_change_is_in_the_audit_record_without_any_secret(api: Running) -> None:
    await api.admin.post("/v1/providers", json=DEEPSEEK)
    await api.admin.patch("/v1/providers/deepseek", json={"api_key": "another-key"})
    await api.admin.post("/v1/environments", json={"name": "e", "secrets": {"T": {"hosts": ["x.com"],
                                                                                 "value": "the-secret"}}})
    await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    made = (await api.admin.post("/v1/keys", json={"name": "ci", "role": "runner"})).json()
    await api.admin.delete(f"/v1/keys/{made['id']}")
    await api.admin.patch("/v1/organization", json={"retention_days": 30})

    audit = (await api.admin.get("/v1/audit", params={"limit": 100})).json()
    records = audit["data"][::-1]                                  # oldest first
    assert [(r["action"], r["target"]) for r in records] == [
        ("provider.create", "deepseek"), ("provider.update", "deepseek"), ("environment.create", "e"),
        ("harness.add", "shell"), ("key.create", made["id"]), ("key.revoke", made["id"]),
        ("organization.update", api.org)]
    assert records[1]["detail"] == {"fields": ["api_key"]}
    assert records[3]["detail"]["source"] == "catalog:shell"
    assert all(record["key_id"] for record in records)
    for secret in ("the-real-key", "another-key", "the-secret", made["key"]):
        assert secret not in str(audit)
    page = (await api.admin.get("/v1/audit", params={"limit": 2})).json()
    assert (len(page["data"]), page["has_more"]) == (2, True)
    older = (await api.admin.get("/v1/audit", params={"limit": 100, "after": page["last_id"]})).json()
    assert len(older["data"]) == 5


async def test_the_platform_says_what_it_is_and_the_organization_keeps_its_retention(api: Running) -> None:
    about = (await api.runner.get("/v1/platform")).json()
    assert about == {"version": "4.0.0", "contract": 1, "driver": "local", "isolation": "none"}
    assert (await api.admin.get("/v1/organization")).json() == {"id": api.org, "name": "Test", "retention_days": 0}
    changed = await api.admin.patch("/v1/organization", json={"retention_days": 7})
    assert changed.json()["retention_days"] == 7
    assert (await api.admin.patch("/v1/organization", json={"retention_days": -1})).status_code == 400
    assert (await api.admin.patch("/v1/organization", json={"name": "Other"})).status_code == 400
    ready = await api.runner.get("/readyz")
    assert (ready.status_code, ready.json()) == (200, {"status": "ready"})


async def test_what_packaging_made_is_what_the_api_reports(api: Running) -> None:
    added = (await api.developer.post("/v1/harnesses", files={
        "package": ("shell.tar.gz", io.BytesIO(packaging.pack(SHELL)))})).json()
    from harnessgateway.domain.archive import read_archive
    assert added["digest"] == read_archive(packaging.pack(SHELL)).digest
    assert added["manifest"]["name"] == "shell"
