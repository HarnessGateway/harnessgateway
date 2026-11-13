"""Spec §5.3, §5.4, §6, §8.5, §12.5, §18.5: whole turns, from the request to the final response.

The driver here is local (no Docker): the same runner, stores, internal API, steps and shell package as in
production, with folders for volumes and a local process for the sandbox.
"""

import asyncio
import contextlib
import dataclasses
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from harnessgateway import packaging
from harnessgateway.api.internal import internal_app
from harnessgateway.domain.plan import install_volume, mind_volume, turn_volume, work_volume
from harnessgateway.errors import Conflict, Invalid
from harnessgateway.intake import Intake
from harnessgateway.runner.installer import Installer
from harnessgateway.runner.runner import Runner
from harnessgateway.runner.turn import TurnRunner
from harnessgateway.settings import Settings
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores
from tests.integration.local_driver import LocalDriver

SHELL = Path(__file__).parents[2] / "packages" / "shell"


def variant(tmp_path: Path, name: str, **changes: Any) -> bytes:
    """The shell package under another name: a second harness to switch to."""
    folder = tmp_path / f"package-{name}"
    shutil.copytree(SHELL, folder)
    manifest = json.loads((folder / "harness.json").read_text())
    (folder / "harness.json").write_text(json.dumps({**manifest, "name": name, **changes}))
    return packaging.pack(folder)


class Platform:
    """The pieces of a running platform, and the few things a test does with them."""

    def __init__(self, stores: Stores, org: str, driver: LocalDriver) -> None:
        self.stores, self.org, self.driver = stores, org, driver
        self.intake = Intake(stores)

    async def ask(self, text: Any, model: str = "shell", **request: Any) -> Turn:
        return await self.intake.accept(self.org, {"model": model, "input": text, **request})

    async def final(self, turn: Turn, timeout: float = 30) -> Turn:
        """Wait for a turn's final event, then read the turn."""
        async def last() -> None:
            async for _ in self.stores.turns.follow(self.org, turn.id):
                pass
        await asyncio.wait_for(last(), timeout)
        return await self.stores.turns.get(self.org, turn.id)

    async def run(self, text: Any, model: str = "shell", **request: Any) -> Turn:
        return await self.final(await self.ask(text, model, **request))

    async def started(self, turn: Turn, marker: str = "started") -> None:
        """Wait until a running turn has printed `marker`."""
        async with asyncio.timeout(20):
            while not any(marker in event.get("delta", "") for event in await self.stores.turns.events(
                    self.org, turn.id)):
                await asyncio.sleep(0.05)


def text_of(turn: Turn) -> str:
    return "".join(part["text"] for item in turn.output if item["type"] == "message" for part in item["content"])


@pytest.fixture
async def idle(stores: Stores, org: str, tmp_path: Path) -> Platform:
    """A platform with the shell harness added and no runner running."""
    async with stores.db.system() as conn:               # the queue is shared: start with nothing waiting
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")
    await stores.harnesses.add(org, packaging.pack(SHELL))
    return Platform(stores, org, LocalDriver(tmp_path / "volumes", internal_app(stores)))


@pytest.fixture
async def platform(idle: Platform) -> AsyncIterator[Platform]:
    """The same, with a runner taking turns."""
    task = asyncio.create_task(Runner(idle.stores, idle.driver, heartbeat_s=0.2).run())
    yield idle
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ── a turn ──────────────────────────────────────────────────────────────────────────────────────────

async def test_a_first_turn_runs_and_completes_with_its_output(platform: Platform) -> None:
    turn = await platform.run("echo hello; echo made > note.txt")
    assert (turn.status, turn.reason) == ("completed", None)
    assert text_of(turn) == "hello\n"
    assert turn.snapshot_id is not None
    assert turn.image_id == "sha256:sandbox:test"
    assert turn.started_at
    assert turn.restored_at
    assert turn.ran_at
    assert turn.exited_at
    assert turn.saved_at
    manifest = await platform.stores.snapshots.manifest(platform.org, turn.snapshot_id)
    assert "work/note.txt" in manifest.entries
    events = await platform.stores.turns.events(platform.org, turn.id)
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert (events[0]["type"], events[1]["type"], events[-1]["type"]) == (
        "response.created", "response.in_progress", "response.completed")
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "hello\n"


async def test_the_sandbox_is_given_the_contract_s_variables(platform: Platform) -> None:
    turn = await platform.run('echo "$HG_SESSION $HG_RESPONSE $HG_HARNESS_NAME $HG_ENVIRONMENT [$HG_PROVIDER]"; pwd')
    lines = text_of(turn).splitlines()
    assert lines[0] == f"{turn.session_id} {turn.id} shell default []"
    assert lines[1] == str(platform.driver.volume(work_volume(turn.session_id)))
    plan = platform.driver.sandboxes[-1]
    assert plan.name == f"hg-{turn.session_id}"
    assert plan.labels == {"hg.org": platform.org, "hg.session": turn.session_id, "hg.turn": turn.id}
    assert (plan.cpus, plan.memory_mb, plan.network) == (2, 4096, "hg-sandbox")


async def test_the_harness_is_installed_once_before_its_first_turn(platform: Platform) -> None:
    first = await platform.run("ls $HG_HARNESS")
    await platform.run("true")
    assert text_of(first).split() == ["README.md", "harness.json", "install", "kit", "run", "tests"]
    harness = await platform.stores.harnesses.get(platform.org, "shell")
    [install] = await platform.stores.harnesses.installs(platform.org, harness.digest)
    assert (install.status, install.image_id) == ("ready", "sha256:sandbox:test")
    assert sum(1 for name in platform.driver.steps if name.startswith("hg-unpack-")) == 1
    assert [plan.command for plan in platform.driver.sandboxes] == [
        ["/harness/install"], ["/harness/run"], ["/harness/run"]]
    assert platform.driver.sandboxes[0].mounts[0].volume == install_volume(install.id)


async def test_the_turn_s_own_volume_is_gone_when_it_ends(platform: Platform) -> None:
    turn = await platform.run("ls $HG_TURN")
    assert text_of(turn).split() == ["files", "handover.txt", "history.json", "request.json"]
    assert not platform.driver.volume(turn_volume(turn.id)).exists()
    assert platform.driver.volume(mind_volume(turn.session_id)).exists()


# ── sessions ────────────────────────────────────────────────────────────────────────────────────────

async def test_a_continued_turn_finds_what_the_turn_before_left(platform: Platform) -> None:
    first = await platform.run("echo remembered > note.txt; echo state > $HOME/.state")
    second = await platform.run("cat note.txt $HOME/.state", previous_response_id=first.id)
    assert (second.session_id, second.number) == (first.session_id, 2)
    assert text_of(second) == "remembered\nstate\n"
    assert [name.split("-")[1] for name in platform.driver.steps if "restore" in name or "save" in name] == [
        "restore", "save", "restore", "save"]


async def test_volumes_that_were_lost_are_rebuilt_from_the_snapshot(platform: Platform) -> None:
    first = await platform.run("echo remembered > note.txt")
    for volume in (mind_volume(first.session_id), work_volume(first.session_id)):
        await platform.driver.remove_volume(volume)
    await platform.stores.turns.volumes_hold(platform.org, first.session_id, None)
    second = await platform.run("cat note.txt", previous_response_id=first.id)
    assert text_of(second) == "remembered\n"


async def test_a_fork_starts_from_the_named_turn_and_leaves_the_original_alone(platform: Platform) -> None:
    first = await platform.run("echo one > a.txt")
    second = await platform.run("echo two > b.txt", previous_response_id=first.id)
    fork = await platform.run("ls; echo three > c.txt", previous_response_id=first.id)
    assert fork.session_id != first.session_id
    assert text_of(fork) == "a.txt\n"
    onward = await platform.run("ls", previous_response_id=second.id)
    assert (onward.session_id, text_of(onward)) == (first.session_id, "a.txt\nb.txt\n")
    forked_onward = await platform.run("ls", previous_response_id=fork.id)
    assert text_of(forked_onward) == "a.txt\nc.txt\n"


async def test_a_second_turn_on_a_session_with_one_in_progress_is_refused(platform: Platform) -> None:
    running = await platform.ask("echo started; sleep 30")
    with pytest.raises(Conflict, match=f"the session has a turn in progress: {running.id}"):
        await platform.ask("echo again", previous_response_id=running.id)
    await platform.started(running)
    await platform.stores.turns.cancel(platform.org, running.id)
    await platform.final(running)


async def test_a_request_that_does_not_resolve_is_refused_and_nothing_is_queued(platform: Platform) -> None:
    with pytest.raises(Invalid, match="there is no harness named 'absent'"):
        await platform.ask("x", model="absent")
    with pytest.raises(Invalid, match="names no response"):
        await platform.ask("x", previous_response_id="resp_00000000000000000000000000")
    with pytest.raises(Invalid, match="a file that does not exist: file_x"):
        await platform.ask([{"role": "user", "content": [{"type": "input_file", "file_id": "file_x"}]}])
    assert await platform.stores.turns.claim("someone") is None


# ── switching ───────────────────────────────────────────────────────────────────────────────────────

async def test_a_switched_to_harness_gets_the_handover_the_history_and_the_same_folders(
    platform: Platform, tmp_path: Path
) -> None:
    await platform.stores.harnesses.add(platform.org, variant(tmp_path, "second", mind=".second"))
    await platform.stores.harnesses.add(platform.org, variant(tmp_path, "third", mind=".third"))
    one = await platform.run("echo one > work.txt; echo [$HG_HANDOVER]")
    two = await platform.run("cat work.txt; echo [$HG_HANDOVER]", model="second", previous_response_id=one.id)
    three = await platform.run('echo "[$HG_HANDOVER]"; cat $HG_TURN/handover.txt', model="third",
                               previous_response_id=two.id)
    back = await platform.run('echo "[$HG_HANDOVER]"; cat $HG_TURN/history.json', previous_response_id=three.id)
    again = await platform.run('echo "[$HG_HANDOVER]"; cat $HG_TURN/history.json', previous_response_id=back.id)

    assert text_of(one) == "[]\n"
    assert text_of(two) == "one\n[Handover from shell.]\n"                         # shell keeps no mind folder
    line = "Handover from second. Before it: shell. Review /mind/.second."
    assert text_of(three) == f"[{line}]\n{line}\n"
    assert three.handover == line
    lines = text_of(back).splitlines()
    assert lines[0] == "[Handover from third. Before it: second. Review /mind/.third and /mind/.second.]"
    assert [(t["id"], t["harness"]) for t in json.loads(lines[1])] == [(two.id, "second"), (three.id, "third")]
    assert text_of(again).splitlines() == ["[]", "[]"]
    assert {turn.session_id for turn in (one, two, three, back, again)} == {one.session_id}


async def test_another_environment_is_another_install_and_another_sandbox(platform: Platform) -> None:
    await platform.stores.environments.create(platform.org, {
        "name": "small", "image": "other:image", "cpus": 0.5, "memory_mb": 512, "tmp_mb": 64,
        "network": {"mode": "none"}, "variables": {"GREETING": "hi"}})
    one = await platform.run("echo made > f.txt")
    two = await platform.run('cat f.txt; echo "$GREETING $HG_ENVIRONMENT"', model="small:shell",
                             previous_response_id=one.id)
    assert text_of(two) == "made\nhi small\n"
    assert (two.environment, two.image_id) == ("small", "sha256:other:image")
    plan = platform.driver.sandboxes[-1]
    assert (plan.cpus, plan.memory_mb, plan.tmp_mb, plan.network) == (0.5, 512, 64, None)
    harness = await platform.stores.harnesses.get(platform.org, "shell")
    assert {i.image_id for i in await platform.stores.harnesses.installs(platform.org, harness.digest)} == {
        "sha256:sandbox:test", "sha256:other:image"}


# ── how a turn ends ─────────────────────────────────────────────────────────────────────────────────

async def test_a_command_that_fails_ends_the_turn_as_failed_and_its_state_is_still_saved(platform: Platform) -> None:
    turn = await platform.run("echo before > kept.txt; echo oops; exit 3")
    assert (turn.status, turn.reason, turn.code) == ("failed", "harness_failed", "command_failed")
    assert turn.message == "The command exited with code 3."
    assert text_of(turn) == "oops\n"
    after = await platform.run("cat kept.txt", previous_response_id=turn.id)
    assert text_of(after) == "before\n"


async def test_a_stopped_turn_is_cancelled_saved_and_can_be_continued(platform: Platform) -> None:
    turn = await platform.ask("echo partial > kept.txt; echo started; sleep 60; echo never")
    await platform.started(turn)
    asked = await platform.stores.turns.cancel(platform.org, turn.id)
    assert asked.cancel_requested
    final = await platform.final(turn)
    assert (final.status, final.reason) == ("cancelled", "cancelled")
    assert text_of(final) == "started\n"
    assert final.snapshot_id is not None
    events = await platform.stores.turns.events(platform.org, turn.id)
    assert (events[-1]["type"], events[-1]["response"]["status"]) == ("response.incomplete", "cancelled")
    after = await platform.run("cat kept.txt", previous_response_id=turn.id)
    assert text_of(after) == "partial\n"


async def test_a_turn_past_its_time_limit_is_stopped_and_failed(platform: Platform) -> None:
    await platform.stores.environments.update(platform.org, "default", {"turn_timeout_s": 1})
    turn = await platform.run("echo started; sleep 60")
    assert (turn.status, turn.reason) == ("failed", "timed_out")
    assert turn.snapshot_id is not None
    response = turn.response()
    assert response["error"]["code"] == "server_error"
    assert "time limit" in response["error"]["message"]
    assert response["metadata"]["hg_reason"] == "timed_out"


async def test_a_turn_that_fills_the_disk_is_stopped_and_failed_and_not_saved(platform: Platform) -> None:
    await platform.stores.environments.update(platform.org, "default", {"disk_mb": 64})
    turn = await platform.run("echo started; head -c 70000000 /dev/zero > big.bin; sleep 60")
    assert (turn.status, turn.reason) == ("failed", "out_of_disk")
    assert turn.snapshot_id is None
    assert "disk limit" in turn.log_tail
    with pytest.raises(Conflict, match="no saved state"):
        await platform.ask("ls", previous_response_id=turn.id)


async def test_a_package_that_prints_something_that_is_not_an_event_fails_the_turn(
    platform: Platform, tmp_path: Path
) -> None:
    folder = tmp_path / "bad"
    shutil.copytree(SHELL, folder)
    (folder / "harness.json").write_text(json.dumps({"contract": 1, "name": "bad", "version": "1", "apis": []}))
    (folder / "run").write_text("#!/bin/sh\necho 'this is not an event'\nsleep 60\n")
    await platform.stores.harnesses.add(platform.org, packaging.pack(folder))
    turn = await platform.run("anything", model="bad")
    assert (turn.status, turn.reason) == ("failed", "invalid_output")
    assert "not JSON" in (turn.message or "")


async def test_a_package_that_exits_without_a_terminal_line_fails_the_turn(platform: Platform, tmp_path: Path) -> None:
    folder = tmp_path / "quiet"
    shutil.copytree(SHELL, folder)
    (folder / "harness.json").write_text(json.dumps({"contract": 1, "name": "quiet", "version": "1", "apis": []}))
    (folder / "run").write_text("#!/bin/sh\necho 'something went wrong' >&2\nexit 4\n")
    await platform.stores.harnesses.add(platform.org, packaging.pack(folder))
    turn = await platform.run("anything", model="quiet")
    assert (turn.status, turn.reason) == ("failed", "harness_error")
    assert "code 4" in (turn.message or "")
    assert "something went wrong" in turn.log_tail


async def test_a_package_whose_install_fails_fails_the_turn_and_is_tried_again_next_time(
    platform: Platform, tmp_path: Path
) -> None:
    folder = tmp_path / "broken"
    shutil.copytree(SHELL, folder)
    (folder / "harness.json").write_text(json.dumps({"contract": 1, "name": "broken", "version": "1", "apis": []}))
    (folder / "install").write_text("#!/bin/sh\necho 'the image has no such tool' >&2\nexit 1\n")
    harness = await platform.stores.harnesses.add(platform.org, packaging.pack(folder))
    turn = await platform.run("echo hi", model="broken")
    assert (turn.status, turn.reason) == ("failed", "install_failed")
    assert turn.snapshot_id is None
    assert "the image has no such tool" in turn.log_tail
    [install] = await platform.stores.harnesses.installs(platform.org, harness.digest)
    assert install.status == "failed"
    assert "the image has no such tool" in install.log_tail
    assert not platform.driver.volume(install_volume(install.id)).exists()

    again = await platform.run("echo hi", model="broken")
    assert again.reason == "install_failed"
    assert sum(1 for plan in platform.driver.sandboxes if plan.command == ["/harness/install"]) == 2


async def test_usage_the_package_reports_is_kept_on_the_turn(platform: Platform, tmp_path: Path) -> None:
    folder = tmp_path / "counted"
    shutil.copytree(SHELL, folder)
    (folder / "harness.json").write_text(json.dumps({"contract": 1, "name": "counted", "version": "1", "apis": []}))
    (folder / "run").write_text(
        "#!/bin/sh\necho '{\"type\": \"hg.done\", \"usage\": {\"input_tokens\": 120, \"output_tokens\": 30}}'\n")
    await platform.stores.harnesses.add(platform.org, packaging.pack(folder))
    turn = await platform.run("anything", model="counted")
    assert turn.status == "completed"
    assert turn.usage is not None
    assert (turn.usage["input_tokens"], turn.usage["output_tokens"], turn.usage["total_tokens"]) == (120, 30, 150)


# ── the runner ──────────────────────────────────────────────────────────────────────────────────────

async def test_a_definition_that_vanished_before_the_turn_ran_fails_it(idle: Platform) -> None:
    queued = await idle.ask("echo hi")
    await idle.stores.harnesses.delete(idle.org, "shell")
    claimed = await idle.stores.turns.claim("runner-test")
    assert claimed is not None
    turn = await TurnRunner(idle.stores, idle.driver, Installer(idle.stores, idle.driver)).run(claimed)
    assert (turn.id, turn.status, turn.reason) == (queued.id, "failed", "sandbox_failed")
    assert "there is no harness named 'shell'" in turn.log_tail


async def test_no_more_turns_run_at_once_than_the_setting_allows(idle: Platform, settings: Settings) -> None:
    stores = dataclasses.replace(idle.stores, settings=dataclasses.replace(settings, max_turns=1))
    runner = asyncio.create_task(Runner(stores, idle.driver, heartbeat_s=0.2).run())
    try:
        first = await idle.ask("echo started; sleep 1")
        second = await idle.ask("echo started")
        await idle.started(first)
        assert (await idle.stores.turns.get(idle.org, second.id)).status == "queued"
        done = await idle.final(second)
        assert done.status == "completed"
        earlier = await idle.stores.turns.get(idle.org, first.id)
        assert earlier.completed_at is not None
        assert done.started_at is not None
        assert earlier.completed_at <= done.started_at
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


async def test_a_turn_left_in_progress_by_a_runner_that_died_is_failed_and_its_session_moves_on(
    platform: Platform,
) -> None:
    first = await platform.run("echo one > a.txt")
    async with platform.stores.db.system() as conn:      # a turn as a runner that died mid-turn left it
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status = 'queued'")
    lost = await platform.stores.turns.create(
        platform.org, resolved=await platform.stores.resolve(platform.org, first.path), parent=first, key_id=None,
        request={"model": "shell", "input": "sleep 600"})
    async with platform.stores.db.system() as conn:
        await conn.execute(
            "UPDATE turns SET status = 'in_progress', runner_id = 'runner-dead', "
            "heartbeat_at = now() - interval '10 minutes' WHERE id = %s", [lost.id])

    final = await platform.final(lost)                   # the living runner's heartbeat finds it
    assert (final.status, final.reason, final.message) == (
        "failed", "sandbox_failed", "the platform restarted during this turn")
    assert (await platform.stores.turns.session(platform.org, final.session_id)).volume_snapshot_id is None
    with pytest.raises(Conflict, match="no saved state"):
        await platform.ask("ls", previous_response_id=lost.id)

    onward = await platform.run("cat a.txt", previous_response_id=first.id)        # from the turn before it: a fork
    assert text_of(onward) == "one\n"
    assert onward.session_id != first.session_id


async def test_when_the_runner_is_stopped_its_running_turns_are_stopped_saved_and_failed(idle: Platform) -> None:
    runner = asyncio.create_task(Runner(idle.stores, idle.driver, heartbeat_s=0.2).run())
    turn = await idle.ask("echo partial > kept.txt; echo started; sleep 60")
    await idle.started(turn)
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    final = await idle.stores.turns.get(idle.org, turn.id)
    assert (final.status, final.reason) == ("failed", "sandbox_failed")
    assert final.snapshot_id is not None
    manifest = await idle.stores.snapshots.manifest(idle.org, final.snapshot_id)
    assert "work/kept.txt" in manifest.entries
