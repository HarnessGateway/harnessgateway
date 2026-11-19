"""The turn runner: takes waiting turns and runs them, a fixed number at once (spec §5.3, §17, §18.5)."""

import asyncio
import contextlib
import logging
import secrets

from harnessgateway.domain.plan import sandbox_name
from harnessgateway.drivers.base import Driver
from harnessgateway.errors import Conflict
from harnessgateway.extensions import Extensions
from harnessgateway.runner.prepare import Preparer
from harnessgateway.runner.turn import RESTARTED, TurnRunner
from harnessgateway.store.turns import CANCEL, CONTROL, Turn
from harnessgateway.stores import Stores

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, stores: Stores, driver: Driver, *, extensions: Extensions | None = None,
                 preparer: Preparer | None = None, heartbeat_s: float = 5.0, silent_for_s: float = 30.0) -> None:
        self.id = f"runner-{secrets.token_hex(6)}"
        self._stores = stores
        self._driver = driver
        self._extensions = extensions or Extensions()
        self._turns = TurnRunner(stores, driver, preparer or Preparer(stores, driver))
        self._heartbeat_s = heartbeat_s
        self._silent_for_s = silent_for_s
        self._active: dict[str, asyncio.Task[Turn]] = {}
        self._freed = asyncio.Event()                          # a running turn ended: there is room for another

    async def recover(self) -> list[Turn]:
        """End every turn whose runner has gone silent, so its session can move on (spec §18.5). Their
        sandboxes are stopped, and their sessions' volumes are no longer trusted to hold anything known."""
        turns, ended = self._stores.turns, []
        for turn in await turns.abandoned(silent_for_s=self._silent_for_s):
            with contextlib.suppress(Conflict):                # another runner ended it first
                await self._driver.stop(sandbox_name(turn.session_id), 0)
                await turns.volumes_hold(turn.org_id, turn.session_id, None)
                ended.append(await turns.conclude(turn, RESTARTED, log=turn.log_tail))
        return ended

    async def run(self) -> None:
        """Run turns until cancelled: whenever one is queued, one of its own ends, or a tick passes, it takes
        what it has room for. On the way out, running turns are stopped, saved and ended."""
        beat = asyncio.create_task(self._beat())
        hearing: asyncio.Task[str | None] | None = None
        try:
            async with self._stores.db.listen(CONTROL) as listener:
                while True:
                    await self._fill()
                    hearing = hearing or asyncio.create_task(listener.next(self._heartbeat_s))
                    freed = asyncio.create_task(self._freed.wait())
                    await asyncio.wait({hearing, freed}, return_when=asyncio.FIRST_COMPLETED)
                    freed.cancel()
                    self._freed.clear()
                    if hearing.done():                         # else it goes on listening while the room is filled
                        message, hearing = hearing.result(), None
                        if message and message.startswith(CANCEL):
                            self._turns.stop(message.removeprefix(CANCEL), "cancelled")
        finally:
            beat.cancel()
            if hearing is not None:
                hearing.cancel()
            for turn_id in self._turns.running():
                self._turns.stop(turn_id, "shutdown")
            await asyncio.gather(beat, *self._active.values(), return_exceptions=True)

    async def _fill(self) -> None:
        """Take waiting turns while there is room for them."""
        for turn_id in [turn_id for turn_id, task in self._active.items() if task.done()]:
            del self._active[turn_id]
        while len(self._active) < self._stores.settings.max_turns:
            turn = await self._stores.turns.claim(self.id)
            if turn is None:
                return
            self._active[turn.id] = asyncio.create_task(self._run(turn))
            self._active[turn.id].add_done_callback(lambda _: self._freed.set())

    async def _run(self, turn: Turn) -> Turn:
        final = await self._turns.run(turn)
        try:
            await self._extensions.after_turn(final)
        except Exception:
            log.exception("an extension failed after turn %s", turn.id)
        return final

    async def _beat(self) -> None:
        """Say this runner is alive, honour stops whose message was missed, and end what other runners left."""
        while True:
            try:
                for turn_id in await self._stores.turns.heartbeat(self.id):
                    self._turns.stop(turn_id, "cancelled")
                await self.recover()
            except Exception:
                log.exception("the runner's heartbeat failed; it will try again")
            await asyncio.sleep(self._heartbeat_s)
