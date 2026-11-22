"""The runner: takes waiting turns and runs them, a fixed number at once, and ends the turns of runners that are
gone. Any number of processes may each run one: the queue is the database."""

import asyncio
import contextlib
import logging
import secrets

from harnessgateway.domain.outcome import ABANDONED
from harnessgateway.errors import Conflict
from harnessgateway.platform import Platform
from harnessgateway.runner.turn import TurnRunner
from harnessgateway.store.turns import RUNNERS, STOP, Turn

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, platform: Platform, *, heartbeat_s: float = 5.0, silent_for_s: float = 30.0) -> None:
        self.id = f"runner-{secrets.token_hex(6)}"
        self._platform = platform
        self._turns = TurnRunner(platform)
        self._heartbeat_s = heartbeat_s
        self._silent_for_s = silent_for_s          # how long a runner may say nothing before its turns are ended
        self._active: dict[str, asyncio.Task[Turn]] = {}
        self._freed = asyncio.Event()              # a turn of its own ended: there is room for another

    async def recover(self) -> list[Turn]:
        """End every turn whose runner has gone silent, so its session can move on. What is left of its sandbox
        is removed, and its session's folders are no longer trusted to hold anything known."""
        platform, ended = self._platform, []
        for turn in await platform.turns.abandoned(silent_for_s=self._silent_for_s):
            with contextlib.suppress(Conflict):    # another runner ended it first
                await platform.driver.discard(turn.session_id)
                await platform.grants.end(turn.id)
                await platform.sessions.folders_hold(turn.org_id, turn.session_id, None)
                ended.append(await platform.turns.conclude(turn, ABANDONED, log=turn.log))
        return ended

    async def run(self) -> None:
        """Run turns until cancelled: whenever one waits, one of its own ends, or a beat passes, it takes what
        it has room for. On the way out, its turns are stopped, saved and ended."""
        beating = asyncio.create_task(self._beat())
        hearing: asyncio.Task[str | None] | None = None
        try:
            async with self._platform.db.listen(RUNNERS) as listener:
                while True:
                    await self._fill()
                    hearing = hearing or asyncio.create_task(listener.next(self._heartbeat_s))
                    freed = asyncio.create_task(self._freed.wait())
                    await asyncio.wait({hearing, freed}, return_when=asyncio.FIRST_COMPLETED)
                    freed.cancel()
                    self._freed.clear()
                    if hearing.done():             # else it goes on listening while the room is filled
                        heard, hearing = hearing.result(), None
                        if heard and heard.startswith(STOP):
                            self._turns.stop(heard.removeprefix(STOP), "cancelled")
        finally:
            beating.cancel()
            if hearing is not None:
                hearing.cancel()
            for turn_id in list(self._active):
                self._turns.stop(turn_id, "shutdown")
            await asyncio.gather(beating, *self._active.values(), return_exceptions=True)

    async def _fill(self) -> None:
        """Take waiting turns while there is room for them."""
        for turn_id in [turn_id for turn_id, task in self._active.items() if task.done()]:
            del self._active[turn_id]
        while len(self._active) < self._platform.settings.max_turns:
            turn = await self._platform.turns.claim(self.id)
            if turn is None:
                return
            self._active[turn.id] = asyncio.create_task(self._turns.run(turn))
            self._active[turn.id].add_done_callback(lambda _: self._freed.set())

    async def _beat(self) -> None:
        """Say this runner is alive, honour stops whose announcement was missed, and end what dead runners left."""
        while True:
            try:
                for turn_id in await self._platform.turns.heartbeat(self.id):
                    self._turns.stop(turn_id, "cancelled")
                await self.recover()
            except Exception:
                log.exception("the runner's heartbeat failed; it will try again")
            await asyncio.sleep(self._heartbeat_s)
