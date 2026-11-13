"""Spec §24, end to end: real turns in real containers, with the shell package."""

import pytest

from harnessgateway.domain.plan import mind_volume, work_volume
from harnessgateway.errors import Conflict
from tests.helpers import Platform, text_of, variant

pytestmark = pytest.mark.usefixtures("database_url")


async def test_a_first_turn_runs_in_a_sandbox_and_completes(platform: Platform) -> None:
    turn = await platform.run("echo hello; id -u; pwd; echo $HOME; echo made > note.txt", timeout=180)
    assert (turn.status, turn.reason) == ("completed", None), turn.log_tail
    assert text_of(turn).splitlines() == ["hello", "10000", "/work", "/mind"]
    assert turn.snapshot_id is not None
    assert turn.image_id is not None
    assert turn.image_id.startswith("sha256:")
    assert turn.command is not None
    assert "docker run --name hg-sess_" in turn.command
    assert "--read-only" in turn.command
    assert "***" in turn.command                          # the proxy token is never shown
    manifest = await platform.stores.snapshots.manifest(platform.org, turn.snapshot_id)
    assert "work/note.txt" in manifest.entries


async def test_the_sandbox_can_write_only_its_two_folders_and_tmp(platform: Platform) -> None:
    turn = await platform.run(
        "touch /etc/x 2>&1; touch /harness/x 2>&1; touch /turn/x 2>&1; touch /tmp/ok $HOME/ok /work/ok && echo wrote",
        timeout=180)
    lines = text_of(turn).splitlines()
    assert len(lines) == 4, lines
    assert all("cannot touch" in line for line in lines[:3])       # refused: read-only, or not the agent's
    assert lines[3] == "wrote"


async def test_a_continued_turn_finds_its_files_and_a_fork_goes_its_own_way(platform: Platform) -> None:
    first = await platform.run("echo one > a.txt; echo mind > $HOME/.state", timeout=180)
    second = await platform.run("cat a.txt $HOME/.state; echo two > b.txt", previous_response_id=first.id)
    assert (second.session_id, text_of(second)) == (first.session_id, "one\nmind\n")
    fork = await platform.run("ls", previous_response_id=first.id)
    assert fork.session_id != first.session_id
    assert text_of(fork) == "a.txt\n"
    onward = await platform.run("ls", previous_response_id=second.id)
    assert text_of(onward) == "a.txt\nb.txt\n"


async def test_a_session_whose_volumes_were_removed_is_restored_from_its_snapshot(platform: Platform) -> None:
    first = await platform.run("echo kept > a.txt; chmod 600 a.txt; ln -s a.txt link", timeout=180)
    for volume in (mind_volume(first.session_id), work_volume(first.session_id)):
        await platform.driver.remove_volume(volume)
    await platform.stores.turns.volumes_hold(platform.org, first.session_id, None)
    second = await platform.run("cat link; stat -c '%a %u' a.txt", previous_response_id=first.id)
    assert text_of(second) == "kept\n600 10000\n"


async def test_a_harness_that_takes_over_is_told_who_ran_before_it(platform: Platform, tmp_path) -> None:  # type: ignore[no-untyped-def]
    await platform.stores.harnesses.add(platform.org, variant(tmp_path, "second", mind=".second"))
    one = await platform.run("echo one > work.txt", model="second", timeout=180)
    two = await platform.run('cat work.txt; echo "$HG_HANDOVER"', previous_response_id=one.id, timeout=180)
    assert text_of(two) == "one\nHandover from second. Review /mind/.second.\n"


async def test_a_stopped_turn_is_cancelled_and_what_it_wrote_is_kept(platform: Platform) -> None:
    turn = await platform.ask("echo partial > kept.txt; echo started; sleep 300")
    await platform.started(turn)
    await platform.stores.turns.cancel(platform.org, turn.id)
    final = await platform.final(turn, timeout=60)
    assert (final.status, final.reason) == ("cancelled", "cancelled")
    after = await platform.run("cat kept.txt", previous_response_id=turn.id)
    assert text_of(after) == "partial\n"


async def test_a_turn_past_its_time_limit_is_stopped(platform: Platform) -> None:
    await platform.stores.environments.update(platform.org, "default", {"turn_timeout_s": 3})
    turn = await platform.run("echo started; sleep 300", timeout=180)
    assert (turn.status, turn.reason) == ("failed", "timed_out")
    assert turn.snapshot_id is not None


async def test_a_turn_past_its_memory_limit_is_killed(platform: Platform) -> None:
    await platform.stores.environments.update(platform.org, "default", {"memory_mb": 256, "tmp_mb": 64})
    turn = await platform.run("python3 -c 'x = bytearray(1024 * 1024 * 1024); print(len(x))'", timeout=180)
    assert (turn.status, turn.reason) == ("failed", "out_of_memory"), turn.log_tail
    assert turn.snapshot_id is not None


async def test_a_turn_past_its_disk_limit_is_stopped_and_not_saved(platform: Platform) -> None:
    await platform.stores.environments.update(platform.org, "default", {"disk_mb": 64})
    turn = await platform.run("echo started; head -c 100000000 /dev/zero > big.bin; sleep 300", timeout=180)
    assert (turn.status, turn.reason) == ("failed", "out_of_disk"), turn.log_tail
    assert turn.snapshot_id is None
    with pytest.raises(Conflict, match="no saved state"):
        await platform.ask("ls", previous_response_id=turn.id)


async def test_a_sandbox_has_no_way_out_but_the_proxy(platform: Platform) -> None:
    probe = ("import socket\n"
             "for host in ('1.1.1.1', '172.17.0.1'):\n"
             "    try:\n"
             "        socket.create_connection((host, 80), timeout=3); print(host, 'reached')\n"
             "    except OSError as e:\n"
             "        print(host, 'unreachable')\n")
    turn = await platform.run(f"python3 -c \"{probe}\"", timeout=180)
    assert text_of(turn) == "1.1.1.1 unreachable\n172.17.0.1 unreachable\n"
