"""Turns, whole: real stores on a real database, real folders, and a stand-in sandbox."""

import asyncio
import json

import pytest

from harnessgateway.api.responses import accept
from harnessgateway.driver.base import Measured, SandboxExit
from harnessgateway.errors import Conflict, NotFound
from harnessgateway.platform import Platform
from harnessgateway.runner import turn as turn_module
from harnessgateway.runner.housekeeping import housekeep
from harnessgateway.runner.runner import Runner
from tests.helpers import DONE, MANIFEST, TEXT, lines, package
from tests.integration.conftest import LINE, ORG, WHO, ready, run, work_files


async def chunk_rows(platform: Platform) -> list[dict]:
    return await platform.db.all(None, "SELECT hash, refs FROM chunks ORDER BY refs")


async def test_a_turn_runs_to_a_response(platform: Platform):
    await ready(platform, variables={"TEAM": "demo"}, scripts={"startup": "echo ready"})
    driver = platform.driver
    driver.writes = {"hello.txt": b"hello\n", "src/main.py": b"print(1)\n"}
    driver.install_exit = SandboxExit(True, 0, measured=Measured(1.0, 1e9))
    driver.exit = SandboxExit(True, 0, measured=Measured(2.0, 4e9))
    turn = await run(platform, metadata={"case": "1"})
    response = turn.response()
    assert (response["status"], response["model"], response["output"][0]["content"][0]["text"]) == ("completed", LINE, "hello")
    assert response["usage"]["total_tokens"] == 5 and response["metadata"] == {"case": "1", "hg_session": turn.session_id}
    events = [event async for event in platform.turns.follow(ORG, turn.id)]
    assert [event["sequence_number"] for event in events] == list(range(6))
    assert [events[0]["type"], events[1]["type"], events[-1]["type"]] == ["response.created", "response.in_progress", "response.completed"]
    assert [event["type"] async for event in platform.turns.follow(ORG, turn.id, after=3)] == ["response.output_item.done", "response.completed"]
    assert await work_files(platform, turn) == {"hello.txt": b"hello\n", "src/main.py": b"print(1)\n"}
    install, sandbox = driver.plans
    assert (install.command, sandbox.command, sandbox.image) == ("/harness/install", "/bin/sh", "sha256:node:22")
    assert sandbox.env["TEAM"] == "demo" and sandbox.env["HG_SESSION"] == turn.session_id and sandbox.name == turn.session_id
    assert [(mount.path, mount.read_only) for mount in sandbox.mounts] == [
        ("/mind", False), ("/work", False), ("/turn", True), ("/harness", True), ("/etc/hg", True)]
    laid = driver.folder(sandbox, "/harness")
    assert (laid / "run").stat().st_mode & 0o100                   # beside the package: the contract, and its schema
    assert "# Harness packages" in (laid / "contract" / "PACKAGES.md").read_text()
    assert len(json.loads((laid / "contract" / "output.schema.json").read_text())["anyOf"]) > 80
    given = driver.found[0]                                   # what the sandbox found under /turn
    assert json.loads(given["request.json"])["input"] == "hi" and given["startup.sh"] == "echo ready\n" and given["history.json"] == "[]"
    used = await platform.usage.total(ORG)                    # what the install and the turn used, added up
    assert (used["cpu_seconds"], used["memory_byte_seconds"]) == (3.0, 5e9)
    assert not driver.folder(sandbox, "/turn").exists()       # the turn's inputs are gone
    token = sandbox.env["HTTPS_PROXY"].split("@")[0].rsplit(":", 1)[1]
    assert await platform.grants.policy(turn.id, token) is None       # and what it was granted is worth nothing


async def test_a_session_continues_forks_and_changes_harness(platform: Platform):
    await ready(platform)
    driver = platform.driver
    await platform.resources.add_package(ORG, "other", package({**MANIFEST, "name": "other", "mind": ".other"}))
    await platform.resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["echo", "other"]})
    driver.writes = {"a.txt": b"one"}
    first = await run(platform)
    driver.writes = {"b.txt": b"two"}
    second = await run(platform, previous_response_id=first.id)
    assert second.session_id == first.session_id and second.number == 2
    assert await work_files(platform, second) == {"a.txt": b"one", "b.txt": b"two"}
    driver.writes = {"c.txt": b"three"}
    fork = await run(platform, previous_response_id=first.id)            # from an earlier turn: a new session
    assert fork.session_id != first.session_id and await work_files(platform, fork) == {"a.txt": b"one", "c.txt": b"three"}
    assert (await platform.sessions.get(ORG, fork.session_id)).forked_from_turn_id == first.id
    driver.writes = {}
    changed = await run(platform, model="default/other/none/none", previous_response_id=second.id)
    assert changed.session_id == first.session_id and changed.harness == "other"
    assert driver.found[-1]["handover.txt"] == "Handover from echo." and len(json.loads(driver.found[-1]["history.json"])) == 2
    assert await work_files(platform, changed) == {"a.txt": b"one", "b.txt": b"two"}
    assert [row["refs"] for row in await chunk_rows(platform)] == [1, 2, 4]     # a chunk is kept once, however often named
    waiting = await accept(platform, WHO, {"model": LINE, "input": "x"})
    with pytest.raises(Conflict, match="in progress"):                    # a turn continues from one that has ended
        await accept(platform, WHO, {"model": LINE, "input": "x", "previous_response_id": waiting.id})


async def test_what_goes_wrong_ends_the_turn_with_its_reason(platform: Platform, monkeypatch: pytest.MonkeyPatch):
    await ready(platform, scripts={"startup": "exit 3"}, timeout_s=1, disk_mb=64)
    driver = platform.driver
    monkeypatch.setattr(turn_module, "_MEASURE_EVERY_S", 0.05)
    driver.exit = SandboxExit(True, 137, True)
    assert (await run(platform)).reason == "out_of_memory"
    driver.exit = SandboxExit(False, error="no such image")
    never = await run(platform)
    assert (never.reason, never.snapshot_id) == ("sandbox_failed", None) and "no such image" in never.log
    driver.prints, driver.exit = [], SandboxExit(True, 78)
    failed = await run(platform)
    assert failed.reason == "startup_failed" and failed.snapshot_id is not None       # what it left is kept
    driver.prints, driver.exit = ["not json"], SandboxExit(True, 0)
    assert (await run(platform)).reason == "invalid_output"
    driver.prints, driver.hold = lines(*TEXT), True
    timed = await run(platform)
    assert timed.reason == "timed_out" and timed.response()["output"][0]["content"][0]["text"] == "hello"
    driver.stopped.clear()
    driver.writes = {"big.bin": 65 * 1024 * 1024}
    full = await run(platform)
    assert (full.reason, full.snapshot_id) == ("out_of_disk", None) and "over the limit of 64 MiB" in full.log
    with pytest.raises(Conflict, match="no saved state"):
        await accept(platform, WHO, {"model": LINE, "input": "x", "previous_response_id": full.id})
    driver.stopped.clear()
    driver.writes, driver.hold, driver.prints = {}, False, lines(*TEXT, DONE)
    await platform.resources.apply(ORG, "environments", "broken", {"image": "missing:1", "harnesses": ["echo"]})
    assert (await run(platform, model="broken/echo/none/none")).reason == "sandbox_failed"
    await platform.resources.add_package(ORG, "echo", package({**MANIFEST, "version": "2"}))
    driver.install_exit = SandboxExit(True, 1)
    broken = await run(platform)
    assert broken.reason == "install_failed" and "install exited with code 1" in broken.log
    driver.install_exit = SandboxExit(True, 0)
    assert (await run(platform)).status == "completed"                     # a failed install is tried again


async def test_a_runner_takes_turns_from_the_queue_and_a_turn_is_cancelled_waiting_or_running(platform: Platform):
    await ready(platform)
    waiting = await accept(platform, WHO, {"model": LINE, "input": "x"})
    assert (await platform.turns.cancel(ORG, waiting.id)).status == "cancelled"
    assert await platform.turns.claim("runner-a") is None
    platform.driver.hold = True
    running = asyncio.create_task(Runner(platform, heartbeat_s=0.2).run())
    turn = await accept(platform, WHO, {"model": LINE, "input": "x"})
    async for event in platform.turns.follow(ORG, turn.id):
        if event["type"] == "response.output_item.done":                   # it is running: ask for it to stop
            await platform.turns.cancel(ORG, turn.id)
    ended = await platform.turns.get(ORG, turn.id)
    assert (ended.status, ended.reason) == ("cancelled", "cancelled") and ended.snapshot_id is not None
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


async def test_a_turn_whose_runner_is_gone_is_ended_by_another(platform: Platform):
    await ready(platform)
    await accept(platform, WHO, {"model": LINE, "input": "x"})
    left = await platform.turns.claim("runner-that-died")
    assert await Runner(platform, silent_for_s=30).recover() == []         # it may only be slow
    ended = await Runner(platform, silent_for_s=0).recover()
    assert [turn.id for turn in ended] == [left.id] and "stopped while this turn was running" in ended[0].message
    assert platform.driver.discarded == [left.session_id] and await Runner(platform, silent_for_s=0).recover() == []


async def test_housekeeping_and_deleting_remove_what_nobody_needs(platform: Platform):
    await ready(platform)
    driver = platform.driver
    driver.writes = {"big.bin": b"x" * 1000}
    turn = await run(platform)
    work = driver.folder(driver.plans[-1], "/work")
    await platform.db.run(None, "UPDATE sessions SET folder_used_at = now() - interval '2 hours'")
    await housekeep(platform, elapsed_s=10)
    stored = await platform.usage.total(ORG)                   # the chunk and the package, for ten seconds
    assert stored["storage_bytes"] >= 1000 and stored["storage_byte_seconds"] == stored["storage_bytes"] * 10
    assert not work.exists()                                   # idle: the snapshot holds everything
    assert await platform.rounds.take("housekeeping", every_s=60) == 0.0 and await platform.rounds.take("housekeeping", every_s=60) is None
    driver.writes = {}
    again = await run(platform, previous_response_id=turn.id)
    assert (await work_files(platform, again))["big.bin"] == b"x" * 1000 and (work / "big.bin").is_file()
    unkept = await run(platform, store=False)
    await platform.forget(ORG, unkept.id)
    with pytest.raises(NotFound):
        await platform.sessions.get(ORG, unkept.session_id)
    await platform.remove_session(ORG, turn.session_id)
    assert await platform.db.all(None, "SELECT id FROM turns") == [] and await platform.db.all(None, "SELECT id FROM snapshots") == []
    assert all(row["refs"] == 0 for row in await chunk_rows(platform)) and not work.exists()
    assert await platform.snapshots.collect(grace_s=3600) == 0 and await platform.snapshots.collect(grace_s=0) == 1
    after = await platform.usage.total(ORG)                    # what was used stays used; what is stored is less
    assert after["storage_byte_seconds"] == stored["storage_byte_seconds"] and after["storage_bytes"] < stored["storage_bytes"]
