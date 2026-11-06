"""The caller's side of a turn (SPEC §4, §6.3): routing, queueing, idempotency, reads, cancel and delete."""
import pytest

from harnessgateway.errors import Conflict, Invalid, NotFound

from .conftest import SHELL, pack

BODY = {"model": "shell", "input": "echo hi"}


async def test_a_turn_is_queued_with_its_session_and_its_envelope(plat, org, shell):
    turn = await plat.turns.create(org.admin, {**BODY, "user": "alice", "metadata": {"k": "v"}})
    assert turn["status"] == "queued" and turn["seq"] == 1 and turn["actor"] == org.admin.actor and turn["end_user"] == "alice"
    doc = await plat.turns.response(org.admin, turn["id"])
    assert doc["status"] == "queued" and doc["metadata"] == {"k": "v", "session_id": turn["session_id"]}
    assert doc["model"] == "shell" and doc["output"] == []
    items = await plat.turns.input_items(org.admin, turn["id"])
    assert items[0]["content"][0]["text"] == "echo hi"
    events = [e async for e in follow_until(plat, org, turn["id"], 1)]
    assert events[0]["type"] == "response.created" and events[0]["sequence_number"] == 0
    container = await plat.containers.get(org.admin, turn["session_id"])
    assert container["metadata"]["created_by"] == org.admin.actor and container["metadata"]["user"] == "alice"
    assert container["metadata"]["state"] == "idle" and container["metadata"]["turns"] == "1"


async def follow_until(plat, org, rid, count):
    seen = 0
    async for event in plat.turns.follow(org.admin, rid):
        if event is None:
            continue
        yield event
        seen += 1
        if seen >= count:
            return


async def test_routing_refusals_name_what_is_wrong(plat, org, shell):
    with pytest.raises(NotFound) as e:
        await plat.turns.create(org.admin, {"model": "nope", "input": "x"})
    assert e.value.code == "model_not_found"
    with pytest.raises(Invalid, match="without a model provider"):
        await plat.turns.create(org.admin, {"model": "shell/openai/gpt", "input": "x"})
    with pytest.raises(Invalid, match="not a valid Responses request"):
        await plat.turns.create(org.admin, {"model": "shell", "input": 5})
    with pytest.raises(NotFound, match="no file"):
        await plat.turns.create(org.admin, {"model": "shell", "input": [{"role": "user", "content": [
            {"type": "input_file", "file_id": "file_" + "0" * 32}]}]})
    with pytest.raises(Invalid, match="not a valid Responses request"):
        await plat.turns.create(org.admin, {**BODY, "user": 5})                 # the SDK schema refuses it first
    with pytest.raises(Invalid, match="user is a string of at most 256"):
        await plat.turns.create(org.admin, {**BODY, "user": "u" * 257})


async def test_required_settings_and_secrets_gate_a_turn(plat, org):
    import json
    folder_manifest = json.loads((SHELL / "harness.json").read_text())
    manifest = {**folder_manifest, "name": "gated", "secrets": {"T": {"hosts": ["mcp.example.com"], "required": True}}}
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("harness.json", json.dumps(manifest).encode()), ("install", (SHELL / "install").read_bytes()),
                           ("run", (SHELL / "run").read_bytes())):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    await plat.harnesses.install(org.admin, buf.getvalue())
    with pytest.raises(Invalid, match="needs T set"):
        await plat.turns.create(org.admin, {"model": "gated", "input": "x"})
    h = await plat.harnesses.configure(org.admin, "gated", secrets={"T": "the-value"})
    assert h["secrets"]["T"]["set"] is True and "the-value" not in str(h)
    assert (await plat.turns.create(org.admin, {"model": "gated", "input": "x"}))["status"] == "queued"
    with pytest.raises(Invalid, match="not declared"):
        await plat.harnesses.configure(org.admin, "gated", settings={"NOPE": "x"})


async def test_an_idempotency_key_replays_the_same_request_and_refuses_another(plat, org, shell):
    first = await plat.turns.create(org.admin, BODY, idempotency_key="k1-" + org.id)
    again = await plat.turns.create(org.admin, BODY, idempotency_key="k1-" + org.id)
    assert again["id"] == first["id"]
    with pytest.raises(Conflict, match="different request"):
        await plat.turns.create(org.admin, {**BODY, "input": "echo other"}, idempotency_key="k1-" + org.id)
    with pytest.raises(Invalid, match="Idempotency-Key"):
        await plat.turns.create(org.admin, BODY, idempotency_key="x" * 201)


async def test_a_queued_turn_is_cancelled_now_and_deleted_once_it_has_ended(plat, org, shell):
    turn = await plat.turns.create(org.admin, BODY)
    with pytest.raises(Conflict, match="still running"):
        await plat.turns.delete(org.admin, turn["id"])
    queued = await plat.turns.create(org.admin, {**BODY, "previous_response_id": turn["id"]})
    assert queued["session_id"] == turn["session_id"] and queued["seq"] == 2    # the session's next turn, behind it
    await plat.turns.cancel(org.admin, queued["id"])
    doc = await plat.turns.cancel(org.admin, turn["id"])
    assert doc["status"] == "cancelled" and doc["metadata"]["hg_reason"] == "cancelled"   # the terminal event is response.failed
    assert (await plat.turns.row(org.admin, turn["id"]))["status"] == "cancelled"
    events = [e async for e in plat.turns.follow(org.admin, turn["id"]) if e]
    assert events[-1]["type"] == "response.failed"
    await plat.turns.delete(org.admin, turn["id"])
    with pytest.raises(NotFound):
        await plat.turns.response(org.admin, turn["id"])


async def test_a_run_only_key_reads_only_the_sessions_it_made(plat, org, shell):
    mine = await plat.turns.create(org.app, BODY)
    theirs = await plat.turns.create(org.developer, BODY)
    assert [c["id"] for c in (await plat.containers.list(org.app, 10, None, "desc"))["data"]] == [mine["session_id"]]
    assert {c["id"] for c in (await plat.containers.list(org.developer, 10, None, "desc"))["data"]} == \
        {mine["session_id"], theirs["session_id"]}
    with pytest.raises(NotFound):
        await plat.turns.response(org.app, theirs["id"])
    with pytest.raises(NotFound):
        await plat.containers.get(org.app, theirs["session_id"])
    with pytest.raises(NotFound):
        await plat.turns.create(org.app, {**BODY, "previous_response_id": theirs["id"]})


async def test_the_catalog_is_the_models_list_and_uninstall_keeps_sessions(plat, org, shell):
    assert [h["name"] for h in await plat.harnesses.list(org.app)] == ["shell"]
    files = await plat.harnesses.files(org.admin, "shell")
    assert {f["path"] for f in files} >= {"harness.json", "install", "run"}
    assert (await plat.harnesses.file(org.admin, "shell", "harness.json")).startswith(b"{")
    with pytest.raises(NotFound):
        await plat.harnesses.file(org.admin, "shell", "nope")
    with pytest.raises(Invalid):
        await plat.harnesses.file(org.admin, "shell", "../etc/passwd")
    turn = await plat.turns.create(org.admin, BODY)
    await plat.harnesses.uninstall(org.admin, "shell")
    assert await plat.harnesses.list(org.admin) == []
    assert (await plat.containers.get(org.admin, turn["session_id"]))["metadata"]["harness"] == "shell"
    await plat.harnesses.install(org.admin, pack(SHELL))
    assert (await plat.harnesses.get(org.admin, "shell"))["digest"] == shell["digest"]
