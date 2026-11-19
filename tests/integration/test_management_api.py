"""Spec §14, §16, §22.2: the management API. Every endpoint, who may call it, and what it refuses."""

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harnessgateway import __version__, packaging
from harnessgateway.store.egress import Connection
from harnessgateway.stores import Stores
from tests.helpers import SHELL, SHELL_MODEL, variant
from tests.integration.api import Running, running
from tests.integration.local_driver import LocalDriver

DEEPSEEK = {"name": "deepseek", "apis": {"anthropic": "https://api.deepseek.com/anthropic"},
            "models": ["deepseek-flash"], "api_key": "the-real-key"}


@pytest.fixture
async def api(stores: Stores, org: str, tmp_path: Path) -> AsyncIterator[Running]:
    async with running(stores, org, tmp_path) as api:
        yield api


async def add_shell(api: Running) -> None:
    """Add the shell harness from the catalog, and have the default environment carry it, installed."""
    assert (await api.developer.post("/v1/harnesses", json={"catalog": "shell"})).status_code == 200
    assert (await api.admin.patch("/v1/environments/default", json={"harnesses": ["shell"]})).status_code == 200
    await api.platform.settle()


# ── providers ───────────────────────────────────────────────────────────────────────────────────────

async def test_providers_are_made_read_changed_and_deleted_and_the_key_is_never_shown(api: Running) -> None:
    made = await api.admin.post("/v1/providers", json=DEEPSEEK)
    assert made.status_code == 200
    assert made.json()["has_key"] is True
    assert "the-real-key" not in made.text
    assert (await api.developer.get("/v1/providers/deepseek")).json()["models"] == ["deepseek-flash"]
    listed = (await api.developer.get("/v1/providers")).json()
    assert (listed["object"], [p["name"] for p in listed["data"]], listed["has_more"], listed["last_id"]) == (
        "list", ["deepseek"], False, "deepseek")
    assert "the-real-key" not in str(listed)

    changed = await api.admin.patch("/v1/providers/deepseek", json={"models": ["deepseek-flash", "deepseek-v4-pro"],
                                                                    "api_key": None})
    assert (changed.json()["models"], changed.json()["has_key"]) == (["deepseek-flash", "deepseek-v4-pro"], False)
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
    assert (await api.admin.patch("/v1/providers/absent", json={"models": ["m"]})).status_code == 404
    named_none = await api.admin.post("/v1/providers", json={**DEEPSEEK, "name": "none"})
    assert (named_none.status_code, "none is not a provider's name" in named_none.json()["error"]["message"]) == (
        400, True)
    old = await api.admin.patch("/v1/providers/deepseek", json={"default_model": "m"})
    assert (old.status_code, "default_model cannot be changed" in old.json()["error"]["message"]) == (400, True)


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
    shown = await api.prepared("secure")
    assert (shown["image_id"], shown["build"], shown["installs"], shown["harnesses"]) == (
        "sha256:sandbox:test", None, [], [])
    assert shown["setup"] == {"script": "", "egress": [], "timeout_s": 900}

    assert (await api.admin.delete("/v1/environments/secure")).json() == {"name": "secure", "deleted": True}
    assert (await api.admin.delete("/v1/environments/default")).json() == {"name": "default", "deleted": True}
    assert (await api.developer.get("/v1/environments")).json()["data"] == []
    bad = await api.admin.post("/v1/environments", json={"name": "bad", "cpus": 1000})
    assert (bad.status_code, "cpus" in bad.json()["error"]["message"]) == (400, True)
    unknown = await api.admin.post("/v1/environments", json={"name": "bad", "harnesses": ["absent"]})
    assert (unknown.status_code, unknown.json()["error"]["param"]) == (400, "harnesses")


async def test_a_setup_script_is_built_once_into_the_image_its_environment_s_turns_run_on(api: Running) -> None:
    driver = api.platform.driver
    assert isinstance(driver, LocalDriver)
    await add_shell(api)
    patched = await api.admin.patch("/v1/environments/default", json={"setup": {"script": "echo set up"}})
    assert patched.status_code == 200, patched.text
    default = await api.prepared("default")
    assert (default["build"]["status"], default["build"]["log"]) == ("ready", "set up")
    assert default["build"]["updated_at"]
    image_id = default["image_id"]
    assert image_id.startswith("sha256:harnessgateway-environment:")
    assert [(i["harness"], i["status"]) for i in default["installs"]] == [("shell", "ready")]
    assert (await api.installed("shell"))["environments"] == [{"name": "default", "status": "ready"}]
    [listed] = (await api.developer.get("/v1/environments")).json()["data"]
    assert (listed["image_id"], listed["build"]["status"], "log" in listed["build"]) == (image_id, "ready", False)

    answer = await api.openai.responses.create(model=SHELL_MODEL, input="echo on the built image")
    assert answer.output_text == "on the built image\n"
    assert driver.sandboxes[-1].image == image_id
    assert len(driver.builds) == 1

    await api.admin.post("/v1/environments", json={"name": "twin", "harnesses": ["shell"],
                                                   "setup": {"script": "echo set up"}})
    twin = await api.prepared("twin")                         # the same script on the same image: the same build
    assert (twin["image_id"], twin["build"]["status"], [i["status"] for i in twin["installs"]]) == (
        image_id, "ready", ["ready"])
    assert len(driver.builds) == 1


async def test_a_setup_script_that_fails_fails_its_environment_s_turns_until_it_is_fixed(api: Running) -> None:
    await add_shell(api)
    await api.admin.patch("/v1/environments/default", json={"setup": {"script": "echo trying\nexit 3"}})
    default = await api.prepared("default")
    assert (default["image_id"], default["build"]["status"]) == (None, "failed")
    assert default["build"]["log"].splitlines() == ["trying", "setup exited with code 3"]
    assert [(i["harness"], i["status"]) for i in default["installs"]] == [("shell", None)]
    assert (await api.installed("shell"))["environments"] == [{"name": "default", "status": None}]

    failed = await api.openai.responses.create(model=SHELL_MODEL, input="echo never")
    assert failed.metadata is not None
    assert failed.error is not None
    assert (failed.status, failed.metadata["hg_reason"]) == ("failed", "setup_failed")
    assert failed.error.message == "the environment's setup script failed"
    assert failed.output_text == ""
    assert "setup exited with code 3" in (await api.developer.get(f"/v1/responses/{failed.id}/trace")).json()["log"]

    await api.admin.patch("/v1/environments/default", json={"setup": {"script": "echo fixed"}})
    fixed = await api.prepared("default")
    assert (fixed["build"]["status"], [i["status"] for i in fixed["installs"]]) == ("ready", ["ready"])
    assert (await api.openai.responses.create(model=SHELL_MODEL, input="echo now")).output_text == "now\n"


async def test_a_setup_script_holds_a_placeholder_for_each_secret_and_never_its_value(api: Running) -> None:
    driver = api.platform.driver
    assert isinstance(driver, LocalDriver)
    made = await api.admin.post("/v1/environments", json={
        "name": "tokened", "setup": {"script": 'echo "token is $EXAMPLE_TOKEN"'},
        "secrets": {"EXAMPLE_TOKEN": {"hosts": ["api.example.com"], "value": "s3cret-value-for-test"}}})
    assert made.status_code == 200, made.text
    build = (await api.prepared("tokened"))["build"]
    assert build["status"] == "ready"
    assert build["log"].startswith("token is hgs_")
    assert "s3cret-value-for-test" not in build["log"]
    assert "s3cret-value-for-test" not in driver.builds[-1].stdin


# ── harnesses ───────────────────────────────────────────────────────────────────────────────────────

async def test_the_catalog_offers_what_ships_and_a_package_is_added_from_it(api: Running) -> None:
    offered = (await api.developer.get("/v1/catalog")).json()["data"]
    assert [(p["name"], p["installed_version"]) for p in offered] == [("shell", None)]
    added = await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
    assert added.status_code == 200
    assert (added.json()["name"], added.json()["version"], added.json()["missing"]) == ("shell", "1.0.0", [])
    assert (await api.developer.get("/v1/catalog")).json()["data"][0]["installed_version"] == "1.0.0"
    assert (await api.developer.post("/v1/harnesses", json={"catalog": "absent"})).status_code == 404
    assert (await api.installed("shell"))["environments"] == []          # no environment carries it yet

    await api.admin.patch("/v1/environments/default", json={"harnesses": ["shell"]})
    shown = await api.installed("shell")                     # it is installed for the environment at once
    assert shown["environments"] == [{"name": "default", "status": "ready"}]
    assert {"provider", "model", "installs"}.isdisjoint(shown)
    answer = await api.openai.responses.create(model=SHELL_MODEL, input="echo ready")
    assert answer.output_text == "ready\n"


async def test_a_package_is_uploaded_its_settings_set_and_its_installs_shown(api: Running, tmp_path: Path) -> None:
    archive = variant(tmp_path, "demo", settings={"EFFORT": {"default": "medium"}},
                      secrets={"GITHUB_TOKEN": {"hosts": ["api.github.com"]}})
    added = await api.developer.post("/v1/harnesses", files={"package": ("demo.tar.gz", io.BytesIO(archive))})
    assert added.status_code == 200, added.text
    assert added.json()["settings"] == {"EFFORT": "medium"}

    changed = await api.developer.patch("/v1/harnesses/demo", json={"settings": {"EFFORT": "high"}})
    assert changed.json()["settings"] == {"EFFORT": "high"}
    defaults = await api.developer.patch("/v1/harnesses/demo", json={"model": "m"})
    assert (defaults.status_code, "model cannot be changed" in defaults.json()["error"]["message"]) == (400, True)
    refused = await api.developer.patch("/v1/harnesses/demo", json={"secrets": {"GITHUB_TOKEN": "the-token"}})
    assert (refused.status_code, refused.json()["error"]["type"]) == (403, "insufficient_permissions")
    by_admin = await api.admin.patch("/v1/harnesses/demo", json={"secrets": {"GITHUB_TOKEN": "the-token"}})
    assert by_admin.json()["secrets_set"] == ["GITHUB_TOKEN"]
    assert "the-token" not in by_admin.text

    await api.admin.patch("/v1/environments/default", json={"harnesses": ["demo"]})
    default = await api.prepared("default")
    assert default["image_id"] == "sha256:sandbox:test"
    [install] = default["installs"]
    assert (install["harness"], install["status"], "log" in install) == ("demo", "ready", True)
    assert install["updated_at"]
    listed = (await api.developer.get("/v1/environments")).json()["data"]
    assert [[i["status"] for i in e["installs"]] for e in listed] == [["ready"]]
    assert "log" not in listed[0]["installs"][0]                        # a log is shown for one environment

    await api.admin.post("/v1/environments", json={"name": "other", "image": "other:image", "harnesses": ["demo"]})
    other = await api.prepared("other")
    assert (other["image_id"], [i["status"] for i in other["installs"]]) == ("sha256:other:image", ["ready"])
    shown = await api.installed("demo")
    assert shown["environments"] == [{"name": "default", "status": "ready"}, {"name": "other", "status": "ready"}]
    assert (await api.developer.post("/v1/harnesses/demo/install", json={"environment": "other"})).status_code == 405
    assert (await api.developer.get("/v1/harnesses/demo/installs")).status_code == 404

    assert [h["name"] for h in (await api.developer.get("/v1/harnesses")).json()["data"]] == ["demo"]
    assert (await api.developer.delete("/v1/harnesses/demo")).json() == {"name": "demo", "deleted": True}
    assert (await api.developer.get("/v1/harnesses/demo")).status_code == 404
    assert [e["harnesses"] for e in (await api.developer.get("/v1/environments")).json()["data"]] == [[], []]


async def test_a_package_that_is_not_valid_is_refused_naming_what_is_wrong(api: Running) -> None:
    garbage = await api.developer.post("/v1/harnesses", files={"package": ("x.tar.gz", io.BytesIO(b"not one"))})
    assert (garbage.status_code, "gzip-compressed tar" in garbage.json()["error"]["message"]) == (400, True)
    nothing = await api.developer.post("/v1/harnesses", json={})
    assert (nothing.status_code, "catalog" in nothing.json()["error"]["message"]) == (400, True)
    assert (await api.developer.get("/v1/harnesses")).json()["data"] == []


async def test_a_package_an_agent_wrote_in_a_session_is_added_from_its_work_folder(api: Running) -> None:
    await add_shell(api)
    manifest = '{"contract": 1, "name": "built", "version": "0.1.0", "apis": []}'
    built = await api.openai.responses.create(model=SHELL_MODEL, input=(
        f"mkdir -p pkg && cd pkg && echo '{manifest}' > harness.json && printf '#!/bin/sh\\n' > install && "
        "cp $HG_HARNESS/run run && chmod +x install run"))
    assert built.status == "completed"
    assert built.metadata is not None
    session = built.metadata["hg_session"]

    added = await api.developer.post("/v1/harnesses", json={"session": session, "path": "pkg"})
    assert added.status_code == 200, added.text
    assert (added.json()["name"], added.json()["version"]) == ("built", "0.1.0")
    await api.admin.patch("/v1/environments/default", json={"harnesses": ["built", "shell"]})
    answer = await api.openai.responses.create(model="default/built/none/none", input="echo from the built one")
    assert answer.output_text == "from the built one\n"
    missing = await api.developer.post("/v1/harnesses", json={"session": session, "path": "nowhere"})
    assert (missing.status_code, "no folder 'nowhere'" in missing.json()["error"]["message"]) == (400, True)


# ── sessions, turns, traces ─────────────────────────────────────────────────────────────────────────

async def test_sessions_their_turns_their_files_and_a_turn_s_trace(api: Running) -> None:
    await add_shell(api)
    first = await api.openai.responses.create(model=SHELL_MODEL, input="echo one > a.txt; echo mind > $HOME/.note")
    second = await api.openai.responses.create(model=SHELL_MODEL, input="echo two > b.txt; exit 2",
                                               previous_response_id=first.id)
    other = await api.openai.responses.create(model=SHELL_MODEL, input="echo other")
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
    assert set(trace["used"]) == {"sandbox_seconds", "cpu_seconds", "memory_peak_bytes", "bytes_up", "bytes_down",
                                  "snapshot_bytes"}
    assert trace["used"]["sandbox_seconds"] > 0
    assert trace["used"]["snapshot_bytes"] > 0
    assert "saved as snap_" in trace["log"]
    assert (await api.developer.get("/v1/sessions/sess_00000000000000000000000000")).status_code == 404


async def test_deleting_a_session_removes_its_turns_its_snapshots_and_its_volumes(api: Running) -> None:
    await add_shell(api)
    turn = await api.openai.responses.create(model=SHELL_MODEL, input="echo one > a.txt")
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
    await add_shell(api)
    turn = await api.openai.responses.create(model=SHELL_MODEL, input="sleep 60", background=True)
    assert turn.metadata is not None
    refused = await api.developer.delete(f"/v1/sessions/{turn.metadata['hg_session']}")
    assert (refused.status_code, "cancel it first" in refused.json()["error"]["message"]) == (409, True)
    await api.openai.responses.cancel(turn.id)


async def test_a_turn_not_to_be_stored_takes_its_session_with_it_when_nothing_else_is_in_it(api: Running) -> None:
    await add_shell(api)
    await api.openai.responses.create(model=SHELL_MODEL, input="echo once", store=False)
    assert (await api.developer.get("/v1/sessions")).json()["data"] == []
    kept = await api.openai.responses.create(model=SHELL_MODEL, input="echo kept")
    await api.openai.responses.create(model=SHELL_MODEL, input="echo once", store=False, previous_response_id=kept.id)
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
    ("runner", "GET", "/v1/providers", True),
    ("runner", "GET", "/v1/environments", True),
    ("runner", "GET", "/v1/environments/default", True),
    ("runner", "GET", "/v1/harnesses", True),
    ("runner", "POST", "/v1/providers", False),
    ("runner", "POST", "/v1/environments", False),
    ("runner", "PATCH", "/v1/environments/default", False),
    ("runner", "DELETE", "/v1/environments/default", False),
    ("runner", "POST", "/v1/harnesses", False),
    ("runner", "GET", "/v1/catalog", False),
    ("runner", "GET", "/v1/drafts", False),
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
    answer = await getattr(api, role).request(method, path, json={} if method in ("POST", "PATCH") else None)
    if allowed:
        assert answer.status_code == 200, answer.text
    else:
        assert (answer.status_code, answer.json()["error"]["type"]) == (403, "insufficient_permissions")
        assert f"this key's role is {role}" in answer.json()["error"]["message"]


async def test_a_runner_reads_what_a_model_string_may_name_and_changes_none_of_it(api: Running) -> None:
    await api.admin.post("/v1/providers", json=DEEPSEEK)
    await add_shell(api)
    providers = (await api.runner.get("/v1/providers")).json()["data"]
    assert [(p["name"], p["models"]) for p in providers] == [("deepseek", ["deepseek-flash"])]
    assert "the-real-key" not in str(providers)
    assert (await api.runner.get("/v1/providers/deepseek")).json()["has_key"] is True
    assert [e["harnesses"] for e in (await api.runner.get("/v1/environments")).json()["data"]] == [["shell"]]
    shown = (await api.runner.get("/v1/environments/default")).json()
    assert shown["harnesses"] == ["shell"]
    assert [sorted(install) for install in shown["installs"]] == [["harness", "status", "updated_at"]]   # no log
    assert "log" in (await api.developer.get("/v1/environments/default")).json()["installs"][0]
    assert [h["name"] for h in (await api.runner.get("/v1/harnesses")).json()["data"]] == ["shell"]
    assert [e["name"] for e in (await api.runner.get("/v1/harnesses/shell")).json()["environments"]] == ["default"]

    for method, path in [
        ("POST", "/v1/providers"), ("PATCH", "/v1/providers/deepseek"), ("DELETE", "/v1/providers/deepseek"),
        ("POST", "/v1/environments"), ("PATCH", "/v1/environments/default"), ("DELETE", "/v1/environments/default"),
        ("POST", "/v1/harnesses"), ("PATCH", "/v1/harnesses/shell"), ("DELETE", "/v1/harnesses/shell"),
        ("GET", "/v1/catalog"), ("GET", "/v1/harnesses/shell/files"), ("GET", "/v1/harnesses/shell/files/run"),
    ]:
        answer = await api.runner.request(method, path, json={} if method in ("POST", "PATCH") else None)
        assert (answer.status_code, answer.json()["error"]["type"]) == (403, "insufficient_permissions"), path
    assert (await api.developer.get("/v1/providers/deepseek")).status_code == 200
    assert (await api.developer.get("/v1/harnesses/shell")).status_code == 200
    assert (await api.developer.get("/v1/environments/default")).json()["harnesses"] == ["shell"]


async def test_models_are_not_listed_since_a_model_string_names_its_parts(api: Running) -> None:
    await add_shell(api)
    for path in ("/v1/models", "/v1/models/shell"):
        assert (await api.runner.get(path)).status_code == 404


async def test_a_body_that_holds_a_nul_character_is_refused_and_says_so(api: Running) -> None:
    # The database holds no text with a NUL character. A request with one is refused before anything is stored.
    for path, body in (("/v1/responses", {"model": SHELL_MODEL, "input": "echo a\x00b"}),
                       ("/v1/environments", {"name": "nul", "variables": {"NOTE": "a\x00b"}})):
        refused = await api.admin.post(path, json=body)
        assert refused.status_code == 400, refused.text
        assert "NUL character" in refused.json()["error"]["message"]


async def test_an_environment_says_which_hosts_each_install_was_refused(api: Running) -> None:
    # A package names no host, so an install that needs one its environment does not allow fails with whatever
    # its downloader says. The environment's page says which hosts it asked for and was refused.
    await add_shell(api)
    shown = (await api.admin.get("/v1/environments/default")).json()
    assert shown["installs"][0]["refused"] == []
    image = shown["image_id"]
    [install] = (await api.stores.harnesses.installs_on(api.org, image)).values()
    now = datetime.now(UTC)
    await api.stores.egress.add(api.org, [
        Connection(install.id, host, port, allowed, "said why", 0, 0, now, now)
        for host, port, allowed in (("astral.sh", 443, False), ("astral.sh", 443, False), ("pypi.org", 443, True),
                                    ("mirror.example.com", 8443, False))])
    shown = (await api.admin.get("/v1/environments/default")).json()
    assert shown["installs"][0]["refused"] == ["astral.sh", "mirror.example.com:8443"]
    listed = (await api.admin.get("/v1/environments")).json()["data"]
    assert "refused" not in listed[0]["installs"][0]             # with the logs: for one environment, not a list

    # Another attempt (the environment now allows the host) starts with nothing refused.
    await api.stores.harnesses.begin_install(api.org, install.id, environment="default", step_token_hash="step",
                                             proxy_token_hash="proxy")
    assert (await api.admin.get("/v1/environments/default")).json()["installs"][0]["refused"] == []


# ── usage, audit, platform, organization ────────────────────────────────────────────────────────────

async def test_usage_is_summed_by_what_is_asked(api: Running) -> None:
    await add_shell(api)
    for command in ("echo one", "echo two", "exit 1"):
        await api.openai.responses.create(model=SHELL_MODEL, input=command)
    by_harness = (await api.developer.get("/v1/usage")).json()
    assert by_harness["group_by"] == "harness"
    [row] = by_harness["data"]
    assert (row["group"], row["turns"], row["input_tokens"], row["bytes_up"]) == ("shell", 3, 0, 0)
    assert (row["cpu_seconds"], row["memory_peak_bytes"]) == (0, 0)        # the test driver measures nothing
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
    assert about == {"version": __version__, "contract": 1, "driver": "local", "isolation": "none", "role": "runner"}
    assert (await api.admin.get("/v1/platform")).json()["role"] == "admin"
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


# ── the console ─────────────────────────────────────────────────────────────────────────────────────

async def test_the_console_is_served_at_the_root_without_a_key(api: Running) -> None:
    import httpx
    async with httpx.AsyncClient(base_url=api.url) as anyone:
        page = await anyone.get("/")
        script = await anyone.get("/app.js")
        missing = await anyone.get("/no-such-file.js")
        api_still = await anyone.get("/v1/harnesses")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<title>HarnessGateway</title>" in page.text
    assert (script.status_code, "javascript" in script.headers["content-type"]) == (200, True)
    assert missing.status_code == 404
    assert api_still.status_code == 401                       # the API is not shadowed, and still needs a key


async def test_an_extension_adds_pages_to_the_console(stores: Stores, org: str, tmp_path: Path) -> None:
    import httpx

    from harnessgateway.extensions import Extension, Extensions

    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "extensions.js").write_text("export const pages = [{id: 'billing', title: 'Billing'}];")
    (pages / "billing.js").write_text("export const hello = 1;")

    class WithPages(Extension):
        def console(self) -> list[Path]:
            return [pages]

    async with running(stores, org, tmp_path, Extensions([WithPages()])) as api, httpx.AsyncClient(
            base_url=api.url) as anyone:
        assert "billing" in (await anyone.get("/extensions.js")).text       # the extension's file wins
        assert (await anyone.get("/billing.js")).status_code == 200
        assert (await anyone.get("/app.js")).status_code == 200            # the core's files are still there
