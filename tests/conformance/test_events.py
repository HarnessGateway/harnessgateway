"""Conformance: the events backend appends in order, reads everything back, wakes a follower on more,
and gives up after the timeout. Over records with polling, and over Postgres with NOTIFY when a
database is there."""
import asyncio
import time

import pytest

from conftest import POSTGRES
from harnessgateway.backends.events import open_events
from harnessgateway.backends.events.records import RecordEvents
from harnessgateway.backends.records.sqlite import SqliteRecords


@pytest.fixture(params=["records", "postgres"])
def events(request, tmp_path):
    if request.param == "records":
        return RecordEvents(SqliteRecords(tmp_path / "r.db"))
    try:
        from harnessgateway.backends.records.postgres import PostgresRecords
        r = PostgresRecords(POSTGRES)
    except Exception as e:
        pytest.skip(f"no Postgres at {POSTGRES}: {e}")
    r._run("DELETE FROM records")
    return open_events(r, POSTGRES)


def test_append_read_wait_clear(events):
    events.append("acme", "resp_1", [{"seq": 1, "stream": "stdout", "text": "a"}, {"seq": 2, "stream": "stderr", "text": "b"}])
    events.append("acme", "resp_1", [{"seq": 3, "stream": "stdout", "text": "c"}])
    assert [c["text"] for c in events.read("acme", "resp_1")] == ["a", "b", "c"]
    assert events.read("acme", "resp_2") == []

    async def go():
        began = time.monotonic()
        await events.wait("acme", "resp_1", seen=3, timeout_s=0.3)          # nothing more: the timeout
        waited = time.monotonic() - began
        assert 0.25 <= waited < 1.5
        async def later():
            await asyncio.sleep(0.2)
            await asyncio.to_thread(events.append, "acme", "resp_1", [{"seq": 4, "stream": "stdout", "text": "d"}])
        asyncio.create_task(later())
        began = time.monotonic()
        await events.wait("acme", "resp_1", seen=3, timeout_s=5)            # woken by the append
        assert time.monotonic() - began < 4 and len(events.read("acme", "resp_1")) == 4
        await events.wait("acme", "resp_1", seen=2, timeout_s=5)            # already there: returns at once
    asyncio.run(go())
    events.clear("acme", "resp_1")
    assert events.read("acme", "resp_1") == []
