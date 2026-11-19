"""Spec §18.1: the platform process stops when it is told to, and when a part of it that must keep running ends."""

import asyncio

import pytest

from harnessgateway.serve import stopped_or_broken


async def _forever() -> None:
    await asyncio.Event().wait()


async def _fails() -> None:
    raise ConnectionError("the database went away")


async def _ends() -> None:
    return None


async def test_a_stop_is_a_stop_and_nothing_is_broken() -> None:
    stop, runner = asyncio.Event(), asyncio.create_task(_forever())
    waiting = asyncio.create_task(stopped_or_broken(stop, {"the turn runner": runner}))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    stop.set()
    assert await waiting is None
    runner.cancel()


@pytest.mark.parametrize(("work", "said"), [
    (_fails, "the turn runner ended: ConnectionError('the database went away')"),
    (_ends, "the turn runner ended"),
])
async def test_a_worker_that_ends_is_noticed_and_named(work: object, said: str) -> None:
    # A runner that has stopped claims no turn. A process left serving requests it will never run is worse
    # than one that stops and is started again.
    housekeeping = asyncio.create_task(_forever())
    broken = await stopped_or_broken(asyncio.Event(), {
        "the turn runner": asyncio.create_task(work()), "housekeeping": housekeeping})    # type: ignore[operator]
    assert broken == said
    housekeeping.cancel()
