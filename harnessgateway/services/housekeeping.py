"""What the platform does on its own clock: turns whose node went quiet are failed, and each
organization's storage is sampled once a day. One loop in the `api` role; every pass is safe to
repeat and a failure in one pass never ends the loop."""
from __future__ import annotations

import asyncio
import sys


class Housekeeping:
    def __init__(self, scheduler, metering, every_s: float = 15, sample_every_s: float = 3600):
        self.scheduler, self.metering, self.every_s, self.sample_every_s = scheduler, metering, every_s, sample_every_s
        self._passes = 0

    def pass_once(self) -> dict[str, list[str]]:
        """One pass: lost turns always; storage samples on the hour's first pass of the day's organizations."""
        out = {"lost": self.scheduler.sweep_lost(), "sampled": []}
        if self._passes % max(1, int(self.sample_every_s // self.every_s)) == 0:
            out["sampled"] = self.metering.sample_due()
        self._passes += 1
        return out

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.pass_once)
            except Exception as e:                   # housekeeping never takes the API down
                print(f"housekeeping: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(self.every_s)
