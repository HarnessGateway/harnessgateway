"""Spec §5.3, §6, §13.5, §17: sessions, turns, the queue, the event stream, and how a turn ends."""

import asyncio
from typing import Any

import pytest
from openai.types.responses import Response

from harnessgateway.domain.environment import Environment
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.outcome import Outcome
from harnessgateway.domain.package import Manifest
from harnessgateway.domain.provider import Provider
from harnessgateway.domain.resolve import Resolved
from harnessgateway.errors import Conflict, NotFound
from harnessgateway.store.db import Database
from harnessgateway.store.turns import CANCEL, CONTROL, QUEUED, Turn, Turns
from tests.conftest import make_org

ENVIRONMENT = Environment.load({"name": "default"})
MESSAGE = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": []}
DEEPSEEK = Provider.load({"name": "deepseek", "apis": {"anthropic": "https://api.deepseek.com/anthropic"}})


def resolved(harness: str = "shell", *, mind: str | None = None, provider: Provider | None = None) -> Resolved:
    manifest = Manifest.load({"contract": 1, "name": harness, "version": "1.0.0",
                              "apis": ["anthropic"] if provider else [], **({"mind": mind} if mind else {})})
    return Resolved(ENVIRONMENT, Harness(manifest, digest="d" * 64), provider, "anthropic" if provider else None,
                    "deepseek-flash" if provider else None)


@pytest.fixture
def turns(db: Database) -> Turns:
    return Turns(db)


async def queue(turns: Turns, org: str, text: str = "echo hi", *, parent: Turn | None = None,
                harness: str = "shell", **request: Any) -> Turn:
    return await turns.create(org, resolved=resolved(harness), parent=parent, key_id="key_1",
                              request={"model": harness, "input": text, **request})


async def finish(turns: Turns, turn: Turn, **changes: Any) -> Turn:
    """Claim a turn and end it as completed."""
    claimed = await turns.claim("runner-test")
    assert claimed is not None
    assert claimed.id == turn.id
    return await turns.conclude(claimed, Outcome("completed"), **changes)


@pytest.fixture(autouse=True)
async def empty_queue(db: Database) -> None:
    """The queue is shared by every organization: start each test with nothing waiting."""
    async with db.system() as conn:
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")


# ── sessions ────────────────────────────────────────────────────────────────────────────────────────

async def test_a_request_with_no_parent_starts_a_session(turns: Turns, org: str) -> None:
    turn = await queue(turns, org, metadata={"ticket": "42"})
    assert turn.id.startswith("resp_")
    assert turn.session_id.startswith("sess_")
    assert (turn.status, turn.number, turn.parent_id) == ("queued", 1, None)
    assert (turn.environment, turn.harness, turn.harness_version, turn.provider, turn.model) == (
        "default", "shell", "1.0.0", None, None)
    assert (turn.background, turn.store, turn.key_id) == (False, True, "key_1")
    session = await turns.session(org, turn.session_id)
    assert (session.head_turn_id, session.forked_from_turn_id) == (turn.id, None)
    assert await turns.get(org, turn.id) == turn

    response = turn.response()
    Response.model_validate(response)
    assert (response["status"], response["model"]) == ("queued", "default:shell")
    assert response["metadata"] == {"ticket": "42", "hg_session": turn.session_id}


async def test_what_a_turn_runs_with_is_recorded_on_it(turns: Turns, org: str) -> None:
    turn = await turns.create(org, resolved=resolved("claude-code", mind=".claude", provider=DEEPSEEK), parent=None,
                              key_id=None, request={"model": "claude-code", "input": "hi", "background": True,
                                                    "store": False})
    assert (turn.harness, turn.mind_dir, turn.provider, turn.api, turn.model, turn.digest) == (
        "claude-code", ".claude", "deepseek", "anthropic", "deepseek-flash", "d" * 64)
    assert (turn.background, turn.store) == (True, False)
    assert str(turn.path) == "default:claude-code/deepseek/deepseek-flash"


async def test_naming_the_head_continues_the_session(turns: Turns, org: str) -> None:
    first = await finish(turns, await queue(turns, org))
    second = await queue(turns, org, parent=first)
    assert (second.session_id, second.number, second.parent_id) == (first.session_id, 2, first.id)
    assert (await turns.session(org, first.session_id)).head_turn_id == second.id


async def test_naming_an_earlier_turn_forks_a_new_session(turns: Turns, org: str) -> None:
    first = await finish(turns, await queue(turns, org))
    second = await finish(turns, await queue(turns, org, parent=first))
    fork = await queue(turns, org, parent=first)
    assert fork.session_id != first.session_id
    assert (fork.number, fork.parent_id) == (2, first.id)
    forked = await turns.session(org, fork.session_id)
    assert (forked.head_turn_id, forked.forked_from_turn_id) == (fork.id, first.id)
    assert (await turns.session(org, first.session_id)).head_turn_id == second.id        # untouched


async def test_the_chain_runs_back_to_the_root_through_forks(turns: Turns, org: str) -> None:
    first = await finish(turns, await queue(turns, org, "one", harness="pi"), output=[MESSAGE])
    second = await finish(turns, await queue(turns, org, "two", parent=first, harness="codex"))
    await finish(turns, await queue(turns, org, "abandoned", parent=second))
    fork = await finish(turns, await queue(turns, org, "three", parent=second, harness="hermes"))
    last = await queue(turns, org, "four", parent=fork)

    chain = await turns.chain(org, last.id)
    assert [(t.id, t.harness, t.input) for t in chain] == [
        (first.id, "pi", "one"), (second.id, "codex", "two"), (fork.id, "hermes", "three")]
    assert chain[0].output == [MESSAGE]
    assert chain[0].status == "completed"
    assert await turns.chain(org, first.id) == []


async def test_one_organization_cannot_read_another_s_turn(turns: Turns, db: Database, org: str) -> None:
    turn = await queue(turns, org)
    with pytest.raises(NotFound, match=turn.id):
        await turns.get(await make_org(db), turn.id)


# ── the queue ───────────────────────────────────────────────────────────────────────────────────────

async def test_turns_are_claimed_oldest_first_each_once(turns: Turns, org: str) -> None:
    first, second = await queue(turns, org, "one"), await queue(turns, org, "two")
    claimed = await turns.claim("runner-a")
    assert claimed is not None
    assert (claimed.id, claimed.status, claimed.runner_id) == (first.id, "in_progress", "runner-a")
    assert claimed.started_at is not None
    other = await turns.claim("runner-b")
    assert other is not None
    assert other.id == second.id
    assert await turns.claim("runner-a") is None


async def test_two_runners_claiming_at_once_never_take_the_same_turn(turns: Turns, org: str) -> None:
    queued = {(await queue(turns, org, str(n))).id for n in range(6)}
    claimed = await asyncio.gather(*(turns.claim(f"runner-{n}") for n in range(8)))
    ids = [turn.id for turn in claimed if turn is not None]
    assert sorted(ids) == sorted(queued)


async def test_queueing_a_turn_tells_the_runners(turns: Turns, db: Database, org: str) -> None:
    async with db.listen(CONTROL) as listener:
        await queue(turns, org)
        assert await listener.next(5) == QUEUED
        assert await listener.next(0.05) is None


# ── the stream ──────────────────────────────────────────────────────────────────────────────────────

DELTA = {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0,
         "delta": "hi", "logprobs": []}


async def test_the_stream_is_created_in_progress_output_then_one_final_event(turns: Turns, org: str) -> None:
    turn = await queue(turns, org)
    claimed = await turns.claim("runner-a")
    assert claimed is not None
    await turns.add_event(claimed, {**DELTA, "sequence_number": 2})
    await turns.add_event(claimed, {**DELTA, "sequence_number": 3})
    await turns.conclude(claimed, Outcome("completed"), output=[MESSAGE])
    events = await turns.events(org, turn.id)
    assert [(e["sequence_number"], e["type"]) for e in events] == [
        (0, "response.created"), (1, "response.in_progress"), (2, "response.output_text.delta"),
        (3, "response.output_text.delta"), (4, "response.completed")]
    assert [e["response"]["status"] for e in events if "response" in e] == ["queued", "in_progress", "completed"]
    assert [e["sequence_number"] for e in await turns.events(org, turn.id, after=2)] == [3, 4]


async def test_a_follower_gets_what_is_stored_then_each_new_event_until_the_end(turns: Turns, org: str) -> None:
    turn = await queue(turns, org)
    claimed = await turns.claim("runner-a")
    assert claimed is not None
    await turns.add_event(claimed, {**DELTA, "sequence_number": 2})
    seen: list[int] = []

    async def follower() -> None:
        async for event in turns.follow(org, turn.id, after=0):
            seen.append(event["sequence_number"])

    task = asyncio.create_task(follower())
    await asyncio.sleep(0.3)
    assert seen == [1, 2]
    await turns.add_event(claimed, {**DELTA, "sequence_number": 3})
    await asyncio.sleep(0.3)
    assert seen == [1, 2, 3]
    await turns.conclude(claimed, Outcome("completed"))
    await asyncio.wait_for(task, 5)
    assert seen == [1, 2, 3, 4]


async def test_following_a_finished_turn_replays_it_and_ends(turns: Turns, org: str) -> None:
    turn = await finish(turns, await queue(turns, org))
    assert [e["type"] async for e in turns.follow(org, turn.id)] == [
        "response.created", "response.in_progress", "response.completed"]


# ── ending ──────────────────────────────────────────────────────────────────────────────────────────

async def test_a_completed_turn_keeps_its_output_usage_and_snapshot(turns: Turns, db: Database, org: str) -> None:
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
             "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
             "output_tokens_details": {"reasoning_tokens": 0}}
    turn = await finish(turns, await queue(turns, org), usage=usage, snapshot_id="snap_1", log="a log line\n",
                        snapshot_bytes=1234)
    assert (turn.status, turn.reason, turn.snapshot_id, turn.log_tail) == ("completed", None, "snap_1", "a log line\n")
    assert turn.final
    assert turn.completed_at is not None
    assert turn.runner_id is None
    response = turn.response()
    Response.model_validate(response)
    assert response["usage"]["total_tokens"] == 15
    assert "hg_reason" not in response["metadata"]
    async with db.org(org) as conn:
        row = await (await conn.execute("SELECT * FROM usage WHERE turn_id = %s", [turn.id])).fetchone()
    assert row is not None
    assert (row["harness"], row["environment"], row["input_tokens"], row["output_tokens"], row["snapshot_bytes"],
            row["status"]) == ("shell", "default", 10, 5, 1234, "completed")


async def test_a_failed_turn_says_why(turns: Turns, org: str) -> None:
    await queue(turns, org)
    claimed = await turns.claim("runner-a")
    assert claimed is not None
    turn = await turns.conclude(claimed, Outcome("failed", "harness_failed", "The command exited with code 3.",
                                                 "command_failed"))
    response = turn.response()
    Response.model_validate(response)
    assert response["error"] == {"code": "server_error", "message": "The command exited with code 3."}
    assert (response["metadata"]["hg_reason"], response["metadata"]["hg_code"]) == ("harness_failed", "command_failed")
    events = await turns.events(org, turn.id)
    assert events[-1]["type"] == "response.failed"


async def test_a_turn_ends_only_once(turns: Turns, org: str) -> None:
    turn = await finish(turns, await queue(turns, org))
    with pytest.raises(Conflict, match="already final"):
        await turns.conclude(turn, Outcome("failed", "sandbox_failed", "late"))
    assert (await turns.get(org, turn.id)).status == "completed"


async def test_cancelling_a_waiting_turn_ends_it_at_once(turns: Turns, org: str) -> None:
    turn = await queue(turns, org)
    cancelled = await turns.cancel(org, turn.id)
    assert (cancelled.status, cancelled.reason) == ("cancelled", "cancelled")
    assert await turns.claim("runner-a") is None
    events = await turns.events(org, turn.id)
    assert [(e["type"], e["response"]["status"]) for e in events] == [
        ("response.created", "queued"), ("response.incomplete", "cancelled")]


async def test_cancelling_a_running_turn_asks_its_runner_to_stop_it(turns: Turns, db: Database, org: str) -> None:
    turn = await queue(turns, org)
    await turns.claim("runner-a")
    async with db.listen(CONTROL) as listener:
        asked = await turns.cancel(org, turn.id)
        assert await listener.next(5) == CANCEL + turn.id
    assert (asked.status, asked.cancel_requested) == ("in_progress", True)


async def test_cancelling_a_final_turn_returns_it_unchanged(turns: Turns, org: str) -> None:
    turn = await finish(turns, await queue(turns, org))
    assert await turns.cancel(org, turn.id) == turn
    with pytest.raises(NotFound):
        await turns.cancel(org, "resp_00000000000000000000000000")


async def test_a_deleted_turn_cannot_be_read_but_stays_in_the_chain(turns: Turns, org: str) -> None:
    first = await finish(turns, await queue(turns, org, "one"), output=[MESSAGE])
    second = await queue(turns, org, "two", parent=first)
    with pytest.raises(Conflict, match="cancel it"):
        await turns.delete(org, second.id)

    await turns.delete(org, first.id)
    with pytest.raises(NotFound):
        await turns.get(org, first.id)
    with pytest.raises(NotFound):
        await turns.delete(org, first.id)
    assert await turns.events(org, first.id) == []
    [kept] = await turns.chain(org, second.id)
    assert (kept.id, kept.harness, kept.input, kept.output) == (first.id, "shell", None, [])


# ── while it runs ───────────────────────────────────────────────────────────────────────────────────

async def test_a_step_token_finds_its_turn_until_the_turn_is_final(turns: Turns, org: str) -> None:
    await queue(turns, org)
    claimed = await turns.claim("runner-a")
    assert claimed is not None
    await turns.prepare(claimed, image_id="sha256:abc", handover="Handover from pi.", step_token_hash="step-hash",
                        proxy_token_hash="proxy-hash", placeholders={"hgs_1": {"kind": "provider"}})
    found = await turns.by_step_token("step-hash")
    assert found is not None
    assert (found.id, found.image_id, found.handover, found.placeholders) == (
        claimed.id, "sha256:abc", "Handover from pi.", {"hgs_1": {"kind": "provider"}})
    assert await turns.by_step_token("another") is None

    final = await turns.conclude(claimed, Outcome("completed"))
    assert await turns.by_step_token("step-hash") is None
    assert final.placeholders == {}


async def test_stages_are_stamped_as_they_are_reached(turns: Turns, org: str) -> None:
    await queue(turns, org)
    claimed = await turns.claim("runner-a")
    assert claimed is not None
    await turns.stage(claimed, "restored")
    await turns.stage(claimed, "ran", command="docker run ... ***")
    await turns.stage(claimed, "exited")
    await turns.stage(claimed, "saved")
    turn = await turns.get(org, claimed.id)
    assert turn.started_at
    assert turn.restored_at
    assert turn.ran_at
    assert turn.exited_at
    assert turn.saved_at
    assert turn.started_at <= turn.restored_at <= turn.ran_at <= turn.exited_at <= turn.saved_at
    assert turn.command == "docker run ... ***"


async def test_a_turn_whose_runner_went_silent_is_found(turns: Turns, db: Database, org: str) -> None:
    quiet, lively = await queue(turns, org, "one"), await queue(turns, org, "two")
    await turns.claim("runner-dead")
    await turns.claim("runner-alive")
    async with db.system() as conn:
        await conn.execute("UPDATE turns SET heartbeat_at = now() - interval '5 minutes' WHERE id IN (%s, %s)",
                           [quiet.id, lively.id])
    await turns.heartbeat("runner-alive")
    assert [turn.id for turn in await turns.abandoned(silent_for_s=60)] == [quiet.id]


async def test_the_record_of_what_the_volumes_hold(turns: Turns, db: Database, org: str) -> None:
    turn = await finish(turns, await queue(turns, org))
    assert (await turns.session(org, turn.session_id)).volume_snapshot_id is None
    await turns.volumes_hold(org, turn.session_id, "snap_1")
    assert (await turns.session(org, turn.session_id)).volume_snapshot_id == "snap_1"
    assert await turns.idle_volumes(idle_for_s=3600) == []

    busy = await queue(turns, org, "another session, with a turn waiting")
    await turns.volumes_hold(org, busy.session_id, "snap_2")
    async with db.system() as conn:
        await conn.execute("UPDATE sessions SET volume_used_at = now() - interval '2 hours' WHERE id IN (%s, %s)",
                           [turn.session_id, busy.session_id])
    assert await turns.idle_volumes(idle_for_s=3600) == [(org, turn.session_id)]
    assert (await turns.session(org, turn.session_id)).volume_snapshot_id is None
    assert (await turns.session(org, busy.session_id)).volume_snapshot_id == "snap_2"
