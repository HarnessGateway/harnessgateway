"""Chunks as records, followers woken by Postgres NOTIFY: many replicas, no polling. One listening
connection per process, started on first use; a notification carries `org:rid` and wakes every
follower of that turn. A missed notification costs a follower at most one slow poll."""
from __future__ import annotations

import asyncio

from ..records import Records
from . import Chunk
from .records import RecordEvents

CHANNEL = "hg_chunks"
SLOW_POLL_S = 2.0


class PostgresEvents(RecordEvents):
    def __init__(self, records: Records, dsn: str):
        super().__init__(records)
        self.dsn = dsn
        self._waiters: dict[str, asyncio.Event] = {}
        self._listener: asyncio.Task | None = None

    def append(self, org: str, rid: str, chunks: list[Chunk]) -> None:
        super().append(org, rid, chunks)
        self.records._run(f"NOTIFY {CHANNEL}, %s", (f"{org}:{rid}",))

    async def wait(self, org: str, rid: str, seen: int, timeout_s: float) -> None:
        self._ensure_listener()
        key = f"{org}:{rid}"
        event = self._waiters.setdefault(key, asyncio.Event())
        deadline = asyncio.get_running_loop().time() + timeout_s
        while len(self.read(org, rid)) <= seen:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=min(SLOW_POLL_S, left))
            except asyncio.TimeoutError:
                pass

    def _ensure_listener(self) -> None:
        if self._listener is None or self._listener.done():
            self._listener = asyncio.get_running_loop().create_task(self._listen())

    async def _listen(self) -> None:
        import psycopg
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    async for n in conn.notifies():
                        if (w := self._waiters.get(n.payload)) is not None:
                            w.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)               # the database went away: reconnect; followers poll meanwhile
