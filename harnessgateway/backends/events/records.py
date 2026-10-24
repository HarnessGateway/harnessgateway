"""Chunks as records (`chunk/<org>/<rid>:<seq>`), followers polling: the one-box deployment."""
from __future__ import annotations

import asyncio

from ..records import Records
from . import Chunk

POLL_S = 0.25


class RecordEvents:
    def __init__(self, records: Records):
        self.records = records

    def append(self, org: str, rid: str, chunks: list[Chunk]) -> None:
        for c in chunks:
            self.records.put("chunk", org, f"{rid}:{int(c['seq']):08d}", {"seq": int(c["seq"]), "stream": c["stream"], "text": c["text"]})

    def read(self, org: str, rid: str) -> list[Chunk]:
        return [{"stream": c["stream"], "text": c["text"]} for c in self.records.list("chunk", org, f"{rid}:")]

    async def wait(self, org: str, rid: str, seen: int, timeout_s: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if len(self.read(org, rid)) > seen:
                return
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return
            await asyncio.sleep(min(POLL_S, left))

    def clear(self, org: str, rid: str) -> None:
        for c in self.records.list("chunk", org, f"{rid}:"):
            self.records.delete("chunk", org, f"{rid}:{int(c.get('seq', 0)):08d}")
