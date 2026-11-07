"""A turn's life in a box (SPEC §9.2–§9.7, §11, §12), driven by the fake runner: installs, launches, jobs, events,
snapshots that share blobs, forks and switches without copying, the door's policy and bindings, metering, lost boxes,
deletion and the operator's view."""
import json

import pytest

from harnessgateway.errors import Conflict, NotFound, Unauthenticated

from .conftest import SHELL, Runner, said
from .test_turns import package

BODY = {"model": "shell", "input": "echo hi"}


async def box_of(plat, sid):
    async with plat.db.platform() as tx:
        return await tx.one("SELECT * FROM boxes WHERE session_id = %s ORDER BY created_at DESC LIMIT 1", sid)


async def refs(plat, org_id):
    async with plat.db.platform() as tx:
        return {r["hash"][:8]: r["refs"] for r in await tx.all("SELECT hash, refs FROM blobs WHERE org_id = %s", org_id)}


async def test_an_install_runs_once_in_its_own_box_and_admits_the_sessions_that_waited(plat, org, shell, runner):
    waiting = await plat.turns.create(org.admin, BODY)
    assert await box_of(plat, waiting["session_id"]) is None             # nothing to run it in yet
    install = await runner.install(org.id, shell["digest"])
    assert install["status"] == "ready" and install["bytes"] == 16 and install["log_tail"] == "installed"
    assert (await plat.harnesses.get(org.admin, "shell"))["install"]["status"] == "ready"
    box = await box_of(plat, waiting["session_id"])
    assert box and box["status"] == "starting" and box["kind"] == "session"
    again = await plat.installs.ensure(org.id, shell["digest"])
    assert again["id"] == install["id"]                                     # once per digest and image
    usage = await plat.usage.summary(org.admin, *window(), None)
    assert usage["data"]["total"]["agent_seconds"] > 0 and usage["data"]["total"]["cpu_seconds"] == 0.5


async def test_a_failed_install_fails_the_turns_that_waited_and_can_be_retried(plat, org, shell, runner):
    waiting = await plat.turns.create(org.admin, BODY)
    install = await runner.install(org.id, shell["digest"], ok=False, log_tail="npm: not found")
    assert install["status"] == "failed"
    doc = await plat.turns.response(org.admin, waiting["id"])
    assert doc["status"] == "failed" and doc["metadata"]["hg_reason"] == "install_failed" and "npm" in doc["error"]["message"]
    retried = await plat.installs.retry(org.id, shell["digest"])
    assert retried["status"] == "pending" and retried["id"] == install["id"]
    assert (await runner.install(org.id, shell["digest"]))["status"] == "ready"


async def test_a_new_sandbox_image_re_runs_the_install_before_the_next_turn(plat, org, ready, settings):
    """A deployment upgrades its sandbox image: the first turn on an installed harness finds no install for the new
    image, queues one, waits on it, and gets its box once it is ready. Nobody has to ask for the install."""
    import dataclasses

    from harnessgateway.services.platform import Platform

    upgraded = Platform(dataclasses.replace(settings, sandbox_image="ghcr.io/harnessgateway/sandbox:test-next"))
    await upgraded.open()
    try:
        turn = await upgraded.turns.create(org.admin, BODY)
        assert await box_of(upgraded, turn["session_id"]) is None                # the new image has no install yet
        install = (await upgraded.harnesses.get(org.admin, "shell"))["install"]
        assert install and install["status"] == "pending" and install["id"] != ready["install"]["id"]
        queued = [json.loads(f["payload"]) for _, f in await upgraded.moments.r.xrange("hg:jobs") if f["kind"] == "install"]
        assert {"install_id": install["id"], "org_id": org.id} in queued
        assert (await Runner(upgraded).install(org.id, ready["digest"]))["id"] == install["id"]
        assert (await box_of(upgraded, turn["session_id"]))["status"] == "starting"
    finally:
        await upgraded.close()


def window():
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    return now - timedelta(hours=1), now + timedelta(hours=1)


async def test_a_turn_runs_in_a_box_and_its_state_is_saved(plat, org, ready, runner):
    turn = await plat.turns.create(org.admin, {**BODY, "metadata": {"k": "v"}})
    box = await runner.boot(turn["session_id"])
    info = await runner.info(box)
    assert info["kind"] == "session" and info["new_session"] and info["snapshot"] is None
    assert info["package"]["digest"] == ready["digest"] and info["install"]["key"].endswith(".tar.zst")
    assert info["roots"] == ["workspace", "state", "home/hg"] and info["standby_s"] == 1
    assert "BEGIN CERTIFICATE" in info["ca_pem"]
    with pytest.raises(Unauthenticated):
        await plat.boxes.authenticate(box["id"], "hgb_wrong")
    job = await runner.next(box)
    assert job["turn_id"] == turn["id"] and job["seq"] == 1 and job["harness"] == "shell" and job["provider"] is None
    assert job["request_key"].endswith("/request.json") and job["history_key"].endswith("/history.json")
    assert json.loads(await plat.objects.get(job["history_key"])) == []
    assert (await plat.turns.response(org.admin, turn["id"]))["status"] == "in_progress"
    assert (await plat.containers.get(org.admin, turn["session_id"]))["metadata"]["state"] == "running"
    reply = await runner.turn(box, job, said("hello", exit_code="0"), files={"workspace/a.txt": b"A", "home/hg/.rc": b"rc"})
    assert reply["concluded"] and reply["persist"]
    doc = await plat.turns.response(org.admin, turn["id"])
    assert doc["status"] == "completed" and doc["output"][-1]["content"][0]["text"] == "hello"
    assert doc["metadata"] == {"k": "v", "session_id": turn["session_id"], "exit_code": "0"}
    events = [e async for e in plat.turns.follow(org.admin, turn["id"]) if e]
    assert events[0]["type"] == "response.created" and events[-1]["type"] == "response.completed"
    container = await plat.containers.get(org.admin, turn["session_id"])
    assert container["metadata"]["state"] == "idle" and container["metadata"]["bytes"] == "3"
    files = (await plat.containers.list_files(org.admin, turn["session_id"], 10, None))["data"]
    assert [f["path"] for f in files] == ["/workspace/a.txt"] and files[0]["source"] == "assistant"
    _, stream = await plat.containers.file_content(org.admin, turn["session_id"], files[0]["id"])
    assert b"".join([c async for c in stream]) == b"A"
    assert await refs(plat, org.id) == {"559aead0": 1, "7581e0d4": 1}   # sha256("A"), sha256("rc")
    harness = [r["data"] for r in (await plat.traces.get(org.admin, turn["id"]))["records"] if r["kind"] == "harness"]
    assert harness[0]["outcome"] == "exited" and harness[0]["cpu_seconds"] == 1.5
    usage = (await plat.usage.summary(org.admin, *window(), "session"))["data"][turn["session_id"]]
    assert usage["agent_seconds"] > 0 and usage["cpu_seconds"] == 1.5 and usage["memory_gib_seconds"] > 0
    assert (await box_of(plat, turn["session_id"]))["status"] == "standby"


async def test_a_continuation_reaches_the_standing_box_and_saves_only_what_changed(plat, org, ready, runner):
    first = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(first["session_id"])
    await runner.turn(box, await runner.next(box), files={"workspace/a.txt": b"A", "workspace/b.txt": b"B"})
    assert await runner.next(box, 0) is None                                 # standing by: nothing queued
    second = await plat.turns.create(org.admin, {**BODY, "input": "echo again", "previous_response_id": first["id"]})
    assert second["session_id"] == first["session_id"] and second["seq"] == 2
    assert (await box_of(plat, first["session_id"]))["id"] == box["id"]       # no second box
    job = await runner.next(box, 5)
    assert job["turn_id"] == second["id"]
    history = json.loads(await plat.objects.get(job["history_key"]))
    assert [h["id"] for h in history] == [first["id"]] and history[0]["status"] == "completed"
    missing = await plat.boxes.blobs_missing(box, ["559aead08264d5795d3909718cdd05abd49572e84fe55590eef31a88a08fdffd"])
    assert missing == []                                                      # "A" is held already
    await runner.turn(box, job, files={"workspace/a.txt": b"A", "workspace/b.txt": b"B2"})
    held = await refs(plat, org.id)
    assert held["559aead0"] == 2 and held["df7e70e5"] == 1                    # A twice, B once, B2 once
    files = (await plat.containers.list_files(org.admin, first["session_id"], 10, None))["data"]
    assert [f["path"] for f in files] == ["/workspace/a.txt", "/workspace/b.txt"]
    responses = (await plat.containers.list_responses(org.admin, first["session_id"], 10, None))["data"]
    assert [r["id"] for r in responses] == [first["id"], second["id"]]
    await runner.bye(box)
    assert (await box_of(plat, first["session_id"]))["status"] == "gone"
    kinds = await plat.usage.summary(org.admin, *window(), None)
    assert kinds["data"]["total"]["agent_seconds"] > 0


async def test_a_fork_and_a_switch_derive_their_first_snapshot_without_copying(plat, org, ready, runner):
    manifest = {**json.loads((SHELL / "harness.json").read_text()), "name": "other"}
    await plat.harnesses.install(org.admin, package(manifest))
    first = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(first["session_id"])
    await runner.turn(box, await runner.next(box), files={"workspace/a.txt": b"A", "home/hg/.rc": b"rc"})
    second = await plat.turns.create(org.admin, {**BODY, "previous_response_id": first["id"]})
    await runner.turn(box, await runner.next(box, 5), files={"workspace/a.txt": b"A2", "home/hg/.rc": b"rc"})
    fork = await plat.turns.create(org.admin, {**BODY, "previous_response_id": first["id"]})   # an earlier response
    assert fork["session_id"] != first["session_id"] and fork["seq"] == 1
    forked = await plat.containers.get(org.admin, fork["session_id"])
    assert forked["metadata"]["forked_from"] == first["id"] and forked["metadata"]["bytes"] == "3"
    files = (await plat.containers.list_files(org.admin, fork["session_id"], 10, None))["data"]
    _, stream = await plat.containers.file_content(org.admin, fork["session_id"], files[0]["id"])
    assert b"".join([c async for c in stream]) == b"A"                        # the state after the first turn
    switch = await plat.turns.create(org.admin, {"model": "other", "input": "x", "previous_response_id": second["id"]})
    switched = await plat.containers.get(org.admin, switch["session_id"])
    assert switched["metadata"]["harness"] == "other" and switched["metadata"]["bytes"] == "2"   # the workspace only
    held = await refs(plat, org.id)
    assert held["559aead0"] == 2 and held["7581e0d4"] == 3                    # A: first + fork; rc: first, second, fork
    fork_box = await runner.boot(fork["session_id"])
    info = await runner.info(fork_box)
    assert not info["new_session"] and info["snapshot"]["key"].endswith(".jsonl")
    job = await runner.next(fork_box)
    assert [h["id"] for h in json.loads(await plat.objects.get(job["history_key"]))] == [first["id"]]


async def test_a_fork_waits_for_the_save_it_starts_from(plat, org, ready, runner):
    """Between a turn's end and its snapshot landing, a fork from it is refused, never started from nothing."""
    first = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(first["session_id"])
    job = await runner.next(box)
    await plat.boxes.events(box, job["turn_id"], 1, said("done"))
    assert (await plat.boxes.finish(box, job["turn_id"], {"outcome": "exited", "exit_code": 0}))["persist"]
    second = await plat.turns.create(org.admin, {**BODY, "previous_response_id": first["id"]})   # the next turn, behind it
    assert second["session_id"] == first["session_id"]
    with pytest.raises(Conflict, match="not saved yet"):                      # a fork from the first: its save is in flight
        await plat.turns.create(org.admin, {"model": "shell", "input": "x", "previous_response_id": first["id"]})
    await runner.save(box, job["turn_id"], {"workspace/a.txt": b"A"})
    forked = await plat.turns.create(org.admin, {"model": "shell", "input": "x", "previous_response_id": first["id"]})
    assert forked["session_id"] != first["session_id"]


async def test_cancelling_a_running_turn_stops_it_in_the_box(plat, org, ready, runner):
    turn = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(turn["session_id"])
    job = await runner.next(box)
    assert await plat.boxes.heartbeat(box, job["turn_id"]) == {"cancel": False, "stop": False}
    doc = await plat.turns.cancel(org.admin, turn["id"])
    assert doc["status"] == "in_progress"                                     # the box ends it
    assert (await plat.boxes.heartbeat(box, job["turn_id"]))["cancel"] is True
    reply = await runner.turn(box, job, said("partial"), outcome="stopped", exit_code=-15, files={"workspace/x": b"x"})
    assert reply["persist"]                                                    # what it did so far is kept
    doc = await plat.turns.response(org.admin, turn["id"])
    assert doc["status"] == "cancelled" and doc["output"][-1]["content"][0]["text"] == "partial"
    with pytest.raises(Conflict, match="not this box's turn"):
        await plat.boxes.heartbeat(box, "resp_" + "0" * 32)


async def test_output_that_breaks_the_contract_fails_the_turn_and_stops_the_process(plat, org, ready, runner):
    turn = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(turn["session_id"])
    job = await runner.next(box)
    reply = await plat.boxes.events(box, job["turn_id"], 1, ['{"type": "response.output_item.added", "item": 5}'])
    assert reply == {"concluded": True, "stop": True}
    await plat.boxes.finish(box, job["turn_id"], {"outcome": "stopped", "exit_code": -9})
    doc = await plat.turns.response(org.admin, turn["id"])
    assert doc["status"] == "failed" and doc["metadata"]["hg_reason"] == "invalid_output"


async def test_a_lost_box_fails_its_turn_and_a_new_box_serves_the_next(plat, org, ready, runner):
    turn = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(turn["session_id"])
    await runner.next(box)
    async with plat.db.platform() as tx:
        await tx.run("UPDATE boxes SET expires_at = now() - interval '1 second' WHERE id = %s", box["id"])
    assert await plat.boxes.lost() == 1
    doc = await plat.turns.response(org.admin, turn["id"])
    assert doc["status"] == "failed" and doc["metadata"]["hg_reason"] == "box_lost"
    with pytest.raises(Unauthenticated):
        await plat.boxes.authenticate(box["id"], box["_token"])
    again = await plat.turns.create(org.admin, {**BODY, "previous_response_id": turn["id"]})
    fresh = await box_of(plat, again["session_id"])
    assert fresh["id"] != box["id"] and fresh["status"] == "starting"
    assert [b["id"] for b in await plat.boxes.listing() if b["session_id"] == again["session_id"]] == [fresh["id"]]
    stopped = await plat.boxes.stop_requested(fresh["id"], "account:ops")
    assert stopped["status"] == "gone"
    assert (await plat.turns.response(org.admin, again["id"]))["metadata"]["hg_reason"] == "cancelled"


async def test_the_door_gets_the_turns_policy_and_bindings_and_its_records_are_metered(plat, org, ready, runner):
    manifest = {**json.loads((SHELL / "harness.json").read_text()), "name": "gated",
                "run": {"egress": ["api.example.com"], "stop_grace_s": 5},
                "secrets": {"T": {"hosts": ["api.example.com"]}}, "settings": {"MODE": {"default": "fast"}}}
    await plat.harnesses.install(org.admin, package(manifest))
    await plat.harnesses.configure(org.admin, "gated", secrets={"T": "s3cret-value"})
    await plat.orgs.set_egress(org.id, ["pypi.org", "!evil.example.com"])
    await runner.install(org.id, (await plat.harnesses.get(org.admin, "gated"))["digest"])
    turn = await plat.turns.create(org.admin, {"model": "gated", "input": "x"})
    box = await runner.boot(turn["session_id"])
    idle = await plat.boxes.policy(box["_token"])
    assert idle["turn_id"] is None and idle["allow"] == [] and idle["platform"] == ["api.test:8000"]
    job = await runner.next(box)
    assert job["env"]["MODE"] == "fast" and job["env"]["T"].startswith("hgs_") and "s3cret" not in json.dumps(job)
    policy = await plat.boxes.policy(box["_token"])
    assert policy["turn_id"] == turn["id"] and policy["version"] > idle["version"]
    assert policy["allow"] == ["!evil.example.com", "api.example.com", "pypi.org"]
    assert policy["bindings"] == [{"placeholder": job["env"]["T"], "hosts": ["api.example.com"], "value": "s3cret-value"}]
    with pytest.raises(NotFound):
        await plat.boxes.policy("hgb_nobody")
    await plat.boxes.door_records(box["_token"], [{"turn": turn["id"], "host": "api.example.com", "port": 443, "out": 100,
                                                 "in": 2000, "at": 1.0, "s": 0.2}])
    await runner.turn(box, job)
    usage = (await plat.usage.summary(org.admin, *window(), None))["data"]["total"]
    assert usage["egress_bytes"] == 100 and usage["ingress_bytes"] == 2000
    door = [r["data"] for r in (await plat.traces.get(org.admin, turn["id"]))["records"] if r["kind"] == "door"]
    assert door[0]["connections"][0]["host"] == "api.example.com"
    assert (await plat.boxes.policy(box["_token"]))["bindings"] == []       # the turn is over


async def test_deleting_a_container_releases_its_snapshots_and_gc_removes_unreferenced_blobs(plat, org, ready, runner):
    turn = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(turn["session_id"])
    await runner.turn(box, await runner.next(box), files={"workspace/a.txt": b"A", "workspace/z.txt": b"Z"})
    other = await plat.turns.create(org.admin, BODY)                           # another session holds A too
    box2 = await runner.boot(other["session_id"])
    await runner.turn(box2, await runner.next(box2), files={"workspace/a.txt": b"A"})
    await plat.containers.delete(org.admin, turn["session_id"])
    with pytest.raises(NotFound):
        await plat.containers.get(org.admin, turn["session_id"])
    await plat.containers.purge(org.id, turn["session_id"])
    held = await refs(plat, org.id)
    assert held["559aead0"] == 1 and held["bbeebd87"] == 0
    assert await plat.snapshots.collect(older_than_s=0) == 1
    assert "bbeebd87" not in await refs(plat, org.id)
    await plat.containers.purge(org.id, turn["session_id"])                    # idempotent
    with pytest.raises(NotFound):
        await plat.turns.response(org.admin, turn["id"])


async def test_deleting_a_turn_keeps_the_sessions_current_snapshot(plat, org, ready, runner):
    first = await plat.turns.create(org.admin, BODY)
    box = await runner.boot(first["session_id"])
    await runner.turn(box, await runner.next(box), files={"workspace/a.txt": b"A"})
    await plat.turns.create(org.admin, {**BODY, "previous_response_id": first["id"]})
    await runner.turn(box, await runner.next(box, 5), files={"workspace/a.txt": b"A", "workspace/b.txt": b"B"})
    await plat.turns.delete(org.admin, first["id"])
    held = await refs(plat, org.id)
    assert held["559aead0"] == 1 and held["df7e70e5"] == 1
    assert (await plat.containers.get(org.admin, first["session_id"]))["metadata"]["bytes"] == "2"


async def test_concurrency_and_suspension_hold_boxes_back(plat, org, ready, runner):
    await plat.orgs.update(org.id, limits={"max_concurrent_turns": 1})
    first = await plat.turns.create(org.admin, BODY)
    second = await plat.turns.create(org.admin, BODY)
    assert await box_of(plat, first["session_id"]) and await box_of(plat, second["session_id"]) is None
    box = await runner.boot(first["session_id"])
    await runner.turn(box, await runner.next(box))
    await runner.bye(box)                                                      # frees the slot: the second gets its box
    assert (await box_of(plat, second["session_id"]))["status"] == "starting"
    await plat.orgs.update(org.id, status="suspended")
    box2 = await runner.boot(second["session_id"])
    assert await runner.next(box2) is None
    assert (await plat.turns.response(org.admin, second["id"]))["metadata"]["hg_reason"] == "suspended"


async def test_the_operator_sees_the_platform(plat, org, ready, runner):
    turn = await plat.turns.create(org.admin, BODY)
    await runner.boot(turn["session_id"])
    health = await plat.operations.health()
    assert health["database"]["ok"] and health["redis"]["ok"] and health["objects"]["ok"]
    assert health["boxes"].get("session.standby", 0) >= 1 and health["turns"]["queued"] >= 1
    metrics = await plat.operations.metrics()
    assert metrics.startswith('hg_up{version="') and "hg_turns_queued" in metrics
    assert await plat.operations.ready() == {"database": True, "redis": True}
    mine = await plat.operations.org(org.id)
    assert mine["boxes"] == 1 and mine["limits"]["standby_s"] == 1
    patched = await plat.operations.patch_org(org.id, {"limits": {"standby_s": 30}, "egress": ["pypi.org"]})
    assert patched["limits"]["standby_s"] == 30 and patched["egress"] == ["pypi.org"]
    assert patched["limits_set"] == {"standby_s": 30}
    assert org.id in {o["id"] for o in (await plat.operations.list_orgs())["data"]}
    invited = await plat.operations.invite_admin(org.id, "account:ops", f"ops-{org.id[-6:]}@example.test", "Ops")
    assert invited["role"] == "admin"
    assert (await plat.operations.versions())["migrations"] == {"harnessgateway.migrations": 1}
    assert "secret" not in json.dumps(plat.operations.config()).lower()
    total = await plat.usage.platform_summary(*window(), "org")
    assert org.id in total["data"]
    assert await plat.usage.accrue_storage() >= 1


async def test_the_door_binds_the_bare_provider_key_for_the_turns_provider(plat, org, ready, runner):
    """The CLI writes the header's shape itself (`Authorization: Bearer <placeholder>`), so the door swaps in the key alone."""
    manifest = {**json.loads((SHELL / "harness.json").read_text()), "name": "spoken", "provider": {"apis": ["openai"]}}
    await plat.harnesses.install(org.admin, package(manifest))
    await plat.providers.put(org.id, "oa", "openai", "https://api.openai.com/v1", "sk-the-provider-key")
    await runner.install(org.id, (await plat.harnesses.get(org.admin, "spoken"))["digest"])
    turn = await plat.turns.create(org.admin, {"model": "spoken/oa/gpt-5", "input": "x"})
    box = await runner.boot(turn["session_id"])
    job = await runner.next(box)
    token = job["provider"]["token"]
    assert token.startswith("hgs_") and job["provider"]["url"] == "https://api.openai.com" and "sk-the" not in json.dumps(job)
    [binding] = (await plat.boxes.policy(box["_token"]))["bindings"]
    assert binding["placeholder"] == token and binding["value"] == "sk-the-provider-key"
    assert binding["header"] == "authorization" and binding["hosts"][0].startswith("api.openai.com")
