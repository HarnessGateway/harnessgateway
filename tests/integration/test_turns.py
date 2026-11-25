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
from tests.helpers import DONE, TEXT, lines, package
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
    laid = driver.folder(sandbox, "/harness")                  # the package, and nothing of the platform's beside it
    assert (laid / "run").stat().st_mode & 0o100 and sorted(path.name for path in laid.iterdir()) == ["install", "run"]
    given = driver.found[0]                                   # what the sandbox found under /turn
    assert json.loads(given["request.json"])["input"] == "hi" and given["startup.sh"] == "echo ready\n" and given["handover.txt"] == ""
    assert given["previous.json"] == "null" and response["completed_at"] >= response["created_at"]
    used = await platform.usage.total(ORG)                    # what the install and the turn used, added up
    assert (used["cpu_seconds"], used["memory_byte_seconds"]) == (3.0, 5e9)
    assert not driver.folder(sandbox, "/turn").exists()       # the turn's inputs are gone
    token = sandbox.env["HTTPS_PROXY"].split("@")[0].rsplit(":", 1)[1]
    assert await platform.grants.policy(turn.id, token) is None       # and what it was granted is worth nothing


async def test_a_turn_that_needs_its_caller_ends_with_the_question_and_the_next_request_answers_it(platform: Platform):
    """The Responses API has no status for a response that waits: the turn ends, completed, with the call as its
    last item, and the answer is the next request's input, which reaches the harness as it was sent."""
    await ready(platform)
    driver, tools = platform.driver, [{"type": "function", "name": "lookup", "parameters": {"type": "object"}, "strict": False}]
    asks = {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q": "weather"}'}
    driver.prints = lines({"type": "response.output_text.delta", "delta": "Let me look."}, asks, {"type": "response.completed"})
    asked = await run(platform, tools=tools)
    said, call = asked.response()["output"]
    assert (asked.status, said["content"][0]["text"], call["type"], call["call_id"]) == ("completed", "Let me look.", "function_call", "call_1")
    assert json.loads(driver.found[-1]["request.json"])["tools"] == tools           # the harness is told the caller's tools
    answer = [{"type": "function_call_output", "call_id": "call_1", "output": "sunny"}]
    driver.prints = lines({"type": "response.output_text.delta", "delta": "It is sunny."}, {"type": "response.completed"})
    answered = await run(platform, input=answer, tools=tools, previous_response_id=asked.id)
    assert (answered.session_id, answered.response()["output"][0]["content"][0]["text"]) == (asked.session_id, "It is sunny.")
    assert json.loads(driver.found[-1]["request.json"])["input"] == answer
    previous = json.loads(driver.found[-1]["previous.json"])           # the response this request continues from: the call it answers
    assert (previous["id"], previous["status"], previous["output"][-1]["call_id"]) == (asked.id, "completed", "call_1")


async def test_a_requests_credentials_reach_only_the_proxy(platform: Platform):
    """A request may carry the credentials of the MCP servers it names. They are sealed beside the turn while it
    runs, bound to that server's host; the request is kept, shown and given to the harness with placeholders."""
    await ready(platform)
    driver = platform.driver
    tools = [{"type": "mcp", "server_label": "docs", "server_url": "https://mcp.example.com:8443/mcp",
              "authorization": "tok-secret", "headers": {"X-Key": "key-secret"}}]
    driver.hold = True
    running = asyncio.create_task(run(platform, tools=tools))
    while not driver.found:
        await asyncio.sleep(0.01)
    sandbox = driver.plans[-1]
    token = sandbox.env["HTTPS_PROXY"].split("@")[0].rsplit(":", 1)[1]
    policy = await platform.grants.policy(sandbox.env["HG_RESPONSE"], token)
    told = {secret.value: (secret.hosts, secret.placeholder) for secret in policy.secrets}
    assert set(told) == {"tok-secret", "key-secret"} and {hosts for hosts, _ in told.values()} == {("mcp.example.com:8443",)}
    given = json.loads(driver.found[-1]["request.json"])["tools"][0]
    assert (given["authorization"], given["headers"]["X-Key"]) == (told["tok-secret"][1], told["key-secret"][1])
    driver.stopped.set()
    turn = await running
    kept = json.dumps([turn.request, turn.response(), [event async for event in platform.turns.follow(ORG, turn.id)]])
    assert "secret" not in kept and given["authorization"] in kept
    assert await platform.turns.credentials(turn) == ()               # and once the turn has ended, they are gone



async def test_a_session_continues_forks_and_changes_harness(platform: Platform):
    await ready(platform)
    driver = platform.driver
    await platform.resources.add_package(ORG, "other", package(**{"README.md": b"another package"}))
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
    told = driver.found[-1]["handover.txt"]                               # who worked on it, and nothing of what was said
    assert told.startswith("Handover. ") and "history.json" not in driver.found[-1]
    assert f"1. echo, as {LINE}: {first.id}, {second.id}\n   Its memory: it left no note of where; look in /mind.\n" in told
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
    await platform.resources.add_package(ORG, "echo", package(**{"VERSION": b"2"}))
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


async def test_a_harness_taking_a_session_over_is_told_where_those_before_it_keep_their_memory(platform: Platform):
    await ready(platform)
    driver = platform.driver
    for name in ("other", "third"):
        await platform.resources.add_package(ORG, name, package(**{"README.md": name.encode()}))
    await platform.resources.apply(ORG, "environments", "default", {"image": "node:22", "harnesses": ["echo", "other", "third"]})
    driver.remembers = {".harnessgateway/echo/memory": b"/mind/.echo/log.jsonl: a JSON line for each message.\n",
                        ".echo/log.jsonl": b'{"said": "the code is HERON"}\n'}
    first = await run(platform)
    assert driver.found[-1]["handover.txt"] == ""                           # a session's first turn is no handover
    driver.remembers = {".harnessgateway/other/memory": b"x" * 20_000}       # a note too long to repeat
    second = await run(platform, model="default/other/none/none", previous_response_id=first.id)
    told = driver.found[-1]["handover.txt"]
    assert f"1. echo, as {LINE}: {first.id}\n   Its memory: /mind/.echo/log.jsonl: a JSON line for each message.\n" in told
    assert "HERON" not in told and "HERON" not in json.dumps(driver.found[-1])       # nothing said is handed over: it is read
    assert (driver.folder(driver.plans[-1], "/mind") / ".echo/log.jsonl").read_text() == '{"said": "the code is HERON"}\n'
    driver.remembers = {}
    same = await run(platform, model="default/other/none/none", previous_response_id=second.id)
    assert driver.found[-1]["handover.txt"] == ""                           # its own turn came last
    back = await run(platform, previous_response_id=same.id)                # the first harness returns
    assert driver.found[-1]["handover.txt"].endswith(
        f"1. echo, as {LINE}: {first.id}\n   This is the harness you are: its memory is your own.\n"
        f"2. other, as default/other/none/none: {second.id}, {same.id}\n   Its memory: it says where in /mind/.harnessgateway/other/memory\n")
    await run(platform, model="default/third/none/none", previous_response_id=first.id)     # a fork is told of its own past only
    assert "other" not in driver.found[-1]["handover.txt"] and f": {first.id}\n" in driver.found[-1]["handover.txt"]
    assert back.session_id == first.session_id


async def test_a_turn_that_is_not_stored_leaves_nothing_and_its_parents_session_as_it_was(platform: Platform):
    await ready(platform)
    driver = platform.driver
    driver.writes = {"a.txt": b"one"}
    first = await run(platform)
    driver.writes = {"b.txt": b"two"}
    unkept = await run(platform, store=False, previous_response_id=first.id)
    assert unkept.status == "completed" and unkept.snapshot_id is None and unkept.session_id != first.session_id
    assert sorted(path.name for path in driver.folder(driver.plans[-1], "/work").iterdir()) == ["a.txt", "b.txt"]     # it began from its parent
    assert (await platform.sessions.get(ORG, first.session_id)).head_turn_id == first.id
    assert len(await platform.db.all(None, "SELECT id FROM snapshots")) == 1
    await platform.forget(ORG, unkept.id)
    with pytest.raises(NotFound):
        await platform.sessions.get(ORG, unkept.session_id)
    assert not driver.folder(driver.plans[-1], "/work").exists()
    driver.writes = {}
    again = await run(platform, previous_response_id=first.id)              # the session continues, as if it had not been
    assert again.session_id == first.session_id and await work_files(platform, again) == {"a.txt": b"one"}
    alone = await run(platform, store=False)                                # with no parent: a session that is gone with it
    await platform.forget(ORG, alone.id)
    assert [row["id"] for row in await platform.db.all(None, "SELECT id FROM sessions")] == [first.session_id]


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
    await platform.remove_session(ORG, turn.session_id)
    assert await platform.db.all(None, "SELECT id FROM turns") == [] and await platform.db.all(None, "SELECT id FROM snapshots") == []
    assert all(row["refs"] == 0 for row in await chunk_rows(platform)) and not work.exists()
    assert await platform.snapshots.collect(grace_s=3600) == 0 and await platform.snapshots.collect(grace_s=0) == 1
    after = await platform.usage.total(ORG)                    # what was used stays used; what is stored is less
    assert after["storage_byte_seconds"] == stored["storage_byte_seconds"] and after["storage_bytes"] < stored["storage_bytes"]
