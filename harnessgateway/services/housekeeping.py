"""What the platform does on its own clock: turns whose node went quiet are failed. One loop in the
`api` role; every pass is safe to repeat and a failure in one pass never ends the loop."""
from __future__ import annotations

import asyncio
import sys


class Housekeeping:
    def __init__(self, scheduler, every_s: float = 15):
        self.scheduler, self.every_s = scheduler, every_s

    def pass_once(self) -> dict[str, list[str]]:
        return {"lost": self.scheduler.sweep_lost()}

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.pass_once)
            except Exception as e:                   # housekeeping never takes the API down
                print(f"housekeeping: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(self.every_s)
