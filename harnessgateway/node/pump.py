"""A turn's stdout, from the box to the API: split into whole lines and sent in numbered batches of about
EVENT_BATCH_BYTES, at least every BATCH_S while output flows. The node never parses a line; the API does. A single
line over EVENT_LINE_MAX stops the turn."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from ..domain.turn import Reason
from ..limits import EVENT_BATCH_BYTES, EVENT_LINE_MAX
from ..ports.sandbox import Process
from .client import ApiClient

BATCH_S = 0.2


class LineTooLarge(Exception):
    pass


class Pump:
    def __init__(self, client: ApiClient, rid: str, on_stop: Callable[[Reason], None], log_file: Path | None):
        self.client, self.rid, self.on_stop, self.log_file = client, rid, on_stop, log_file
        self.partial = bytearray()
        self.pending: list[str] = []
        self.pending_bytes = 0
        self.batch = 0
        self.total = 0
        self.concluded = asyncio.Event()
        self._sending = asyncio.Lock()
        self._failed: BaseException | None = None

    def _take(self, cut: int) -> None:
        for line in bytes(self.partial[:cut]).split(b"\n"):
            if line.strip():
                self.pending.append(line.decode("utf-8", "replace"))
                self.pending_bytes += len(line)
        del self.partial[:cut + 1]

    async def run(self, proc: Process) -> None:
        log = self.log_file.open("wb") if self.log_file else None
        ticker = asyncio.create_task(self._tick())
        try:
            while chunk := await proc.read():
                if self._failed:
                    raise self._failed
                self.total += len(chunk)
                if log:
                    log.write(chunk)
                self.partial += chunk
                cut = self.partial.rfind(b"\n")
                if cut >= 0:
                    self._take(cut)
                if len(self.partial) > EVENT_LINE_MAX:
                    raise LineTooLarge(f"a stdout line is over {EVENT_LINE_MAX >> 20} MiB")
                if self.pending_bytes >= EVENT_BATCH_BYTES:
                    await self.flush()
            if self.partial:
                self.partial += b"\n"
                self._take(len(self.partial) - 1)
        finally:
            async with self._sending:
                ticker.cancel()
            if log:
                log.close()
        if self._failed:
            raise self._failed
        await self.flush()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(BATCH_S)
            try:
                await self.flush()
            except Exception as e:
                self._failed = e
                return

    async def flush(self) -> None:
        async with self._sending:
            if not self.pending:
                return
            lines, number = self.pending, self.batch
            self.pending, self.pending_bytes = [], 0
            self.batch += 1
            reply = await self.client.events(self.rid, number, lines)
        if reply.get("concluded"):
            self.concluded.set()
        if reply.get("stop"):
            self.on_stop(Reason.INVALID_OUTPUT)
