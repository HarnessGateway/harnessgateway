"""Running one turn: install, restore, run, save, conclude (spec §5.3)."""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, replace

from harnessgateway.domain.events import LineError, RunOutput
from harnessgateway.domain.handover import handover_line
from harnessgateway.domain.outcome import Observed, Outcome, decide
from harnessgateway.domain.plan import (
    SandboxPlan,
    StepPlan,
    TurnFacts,
    mind_volume,
    restore_step,
    sandbox_plan,
    save_step,
    turn_volume,
    watch_step,
    work_volume,
)
from harnessgateway.domain.resolve import Resolved
from harnessgateway.domain.sealing import new_placeholder, new_token
from harnessgateway.drivers.base import Driver, DriverError, SandboxExit
from harnessgateway.errors import Refused
from harnessgateway.runner.installer import Installer, InstallFailed
from harnessgateway.steps.watch import OVER_LIMIT
from harnessgateway.store.snapshots import Snapshot
from harnessgateway.store.turns import LOG_TAIL, Turn
from harnessgateway.stores import Stores

log = logging.getLogger(__name__)

RESTARTED = Outcome("failed", "sandbox_failed", "the platform restarted during this turn")
_STOP_AGAIN_S = 1.0


class _Log:
    """The end of everything said about a turn: its package's standard error, and what the steps reported."""

    def __init__(self) -> None:
        self._lines: deque[str] = deque()
        self._size = 0

    def add(self, text: str) -> None:
        self._lines.append(text)
        self._size += len(text) + 1
        while self._size > LOG_TAIL and len(self._lines) > 1:
            self._size -= len(self._lines.popleft()) + 1

    def text(self) -> str:
        return "\n".join(self._lines)[-LOG_TAIL:]


class _Stop:
    """A request to stop a turn's sandbox, and why. The first reason given is the one that counts."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.requested = asyncio.Event()

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
            self.requested.set()


class _Failed(Exception):
    """A step before the sandbox failed: the turn ends here."""

    def __init__(self, seen: Observed, why: str) -> None:
        super().__init__(why)
        self.seen = seen


@dataclass(frozen=True)
class _Ready:
    """A turn with everything decided, about to run."""

    turn: Turn
    resolved: Resolved
    plan: SandboxPlan
    step_token: str


class TurnRunner:
    def __init__(self, stores: Stores, driver: Driver, installer: Installer) -> None:
        self._stores = stores
        self._driver = driver
        self._installer = installer
        self._stops: dict[str, _Stop] = {}

    def stop(self, turn_id: str, reason: str) -> None:
        """Ask for a turn this runner is running to be stopped: `cancelled`, or `shutdown`."""
        if turn_id in self._stops:
            self._stops[turn_id].request(reason)

    def running(self) -> list[str]:
        return list(self._stops)

    async def run(self, turn: Turn) -> Turn:
        """Take a claimed turn to its final state. It never raises for what goes wrong inside the turn."""
        stop = self._stops.setdefault(turn.id, _Stop())
        record, output = _Log(), RunOutput(first_sequence_number=2)
        snapshot: Snapshot | None = None
        try:
            ready = await self._prepare(turn, record)
            await self._restore(ready)
            seen = await self._sandbox(ready, stop, output, record)
            snapshot = await self._save(ready, record)
            if snapshot is None:
                seen = replace(seen, save_failed=True)
        except _Failed as failure:
            record.add(str(failure))
            seen = failure.seen
        except Exception as error:                 # a fault of the platform's own: say so, and end the turn
            log.exception("turn %s failed inside the platform", turn.id)
            record.add(f"the platform failed while running this turn: {error!r}")
            seen = Observed(sandbox_failed=True)
        finally:
            self._stops.pop(turn.id, None)
            await self._driver.remove_volume(turn_volume(turn.id))
        terminal = output.terminal
        return await self._stores.turns.conclude(
            turn, decide(seen), output=output.output(), usage=terminal.usage if terminal else None,
            snapshot_id=snapshot.id if snapshot else None, log=record.text(),
            snapshot_bytes=snapshot.bytes if snapshot else 0)

    async def _prepare(self, turn: Turn, record: _Log) -> _Ready:
        """Decide everything the turn runs with, and make sure its harness is installed."""
        stores, driver = self._stores, self._driver
        try:
            resolved = await stores.resolve(turn.org_id, turn.path)
            image_id = await driver.image_id(resolved.environment.image)
        except (Refused, DriverError) as e:
            raise _Failed(Observed(sandbox_failed=True), f"the turn could not be prepared: {e}") from None

        chain = await stores.turns.chain(turn.org_id, turn.id)
        handover = handover_line([earlier.past for earlier in chain], turn.harness)
        step_token, proxy_token = new_token(), new_token()
        placeholders, in_sandbox = {}, {}
        provider_key = None
        if resolved.provider and (await stores.providers.get(turn.org_id, resolved.provider.name)).has_key:
            provider_key = new_placeholder()
            placeholders[provider_key] = {"kind": "provider", "name": resolved.provider.name}
        for kind, names in (("environment", resolved.environment.secrets), ("harness", resolved.harness.secrets_set)):
            for name in names:
                in_sandbox[name] = new_placeholder()
                placeholders[in_sandbox[name]] = {"kind": kind, "name": name}
        await stores.turns.prepare(turn, image_id=image_id, handover=handover,
                                   step_token_hash=stores.hash(step_token), proxy_token_hash=stores.hash(proxy_token),
                                   placeholders=placeholders)
        try:
            install_id = await self._installer.ensure(turn.org_id, resolved.harness, resolved.environment, image_id)
        except InstallFailed as e:
            raise _Failed(Observed(install_failed=True), f"install failed:\n{e}") from None
        plan = sandbox_plan(TurnFacts(
            org_id=turn.org_id, session_id=turn.session_id, turn_id=turn.id, install_id=install_id,
            image_id=image_id, resolved=resolved, handover=handover, proxy_token=proxy_token,
            provider_key_placeholder=provider_key, secret_placeholders=in_sandbox))
        return _Ready(turn, resolved, plan, step_token)

    def _step_facts(self, ready: _Ready) -> dict[str, str]:
        turn = ready.turn
        return {"org_id": turn.org_id, "session_id": turn.session_id, "turn_id": turn.id,
                "internal_url": self._stores.settings.internal_url, "token": ready.step_token}

    async def _restore(self, ready: _Ready) -> None:
        turn, driver = ready.turn, self._driver
        try:
            for volume in (mind_volume(turn.session_id), work_volume(turn.session_id), turn_volume(turn.id)):
                await driver.ensure_volume(volume)
            result = await driver.run_step(restore_step(**self._step_facts(ready)))
        except DriverError as e:
            raise _Failed(Observed(restore_failed=True), f"restore failed: {e}") from None
        if result.exit_code != 0:
            raise _Failed(Observed(restore_failed=True), f"restore failed:\n{result.output}")
        await self._stores.turns.stage(turn, "restored")

    async def _sandbox(self, ready: _Ready, stop: _Stop, output: RunOutput, record: _Log) -> Observed:
        """Run the sandbox to its end, with the watcher beside it, the time limit over it, and a stop honoured."""
        turn, driver, turns = ready.turn, self._driver, self._stores.turns
        invalid: list[str] = []

        async def on_stdout(text: str) -> None:
            if invalid:
                return
            try:
                line = output.read(text)
            except LineError as e:
                invalid.append(str(e))
                stop.request("invalid_output")
                return
            if line is None:
                return
            if line.event is not None:
                await turns.add_event(turn, line.event)
            elif line.text is not None:
                record.add(line.text)

        async def on_stderr(text: str) -> None:
            record.add(text)

        await turns.volumes_hold(turn.org_id, turn.session_id, None)            # they are about to change
        await turns.stage(turn, "ran", command=await driver.describe(ready.plan))
        environment = ready.resolved.environment
        watcher = watch_step(disk_mb=environment.disk_mb, **self._step_facts(ready))
        if stop.reason is None:
            sandbox = asyncio.create_task(driver.run_sandbox(ready.plan, on_stdout, on_stderr))
            guards = [asyncio.create_task(guard) for guard in (
                self._watch(watcher, stop), self._limit(environment.turn_timeout_s, stop),
                self._enforce(ready.plan, stop, sandbox))]
            try:
                result = await sandbox
            except DriverError as e:
                result = SandboxExit(started=False, error=str(e))
            finally:
                for guard in guards:
                    guard.cancel()
                await asyncio.gather(*guards, return_exceptions=True)
                await driver.stop(watcher.name, 0)
        else:
            result = SandboxExit(started=True, exit_code=None)                  # stopped before it began
        await turns.stage(turn, "exited")
        if not result.started:
            record.add(f"the sandbox could not be started: {result.error}")
        return Observed(
            sandbox_failed=not result.started or stop.reason == "shutdown",
            cancelled=stop.reason == "cancelled",
            timed_out=stop.reason == "timed_out",
            out_of_disk=stop.reason == "out_of_disk",
            out_of_memory=result.out_of_memory,
            invalid_output=invalid[0] if invalid else None,
            exit_code=result.exit_code,
            terminal=output.terminal,
        )

    async def _watch(self, watcher: StepPlan, stop: _Stop) -> None:
        """The disk watcher: a step beside the sandbox that ends, with a code of its own, when the limit is passed."""
        try:
            result = await self._driver.run_step(watcher)
        except DriverError:
            return
        if result.exit_code == OVER_LIMIT:
            stop.request("out_of_disk")

    @staticmethod
    async def _limit(seconds: int, stop: _Stop) -> None:
        await asyncio.sleep(seconds)
        stop.request("timed_out")

    async def _enforce(self, plan: SandboxPlan, stop: _Stop, sandbox: "asyncio.Task[SandboxExit]") -> None:
        """Once a stop is asked for, stop the sandbox, and keep asking until it has ended: a stop that arrives
        before the sandbox exists must not be lost."""
        await stop.requested.wait()
        while not sandbox.done():
            await self._driver.stop(plan.name, plan.stop_grace_s)
            await asyncio.sleep(_STOP_AGAIN_S)

    async def _save(self, ready: _Ready, record: _Log) -> Snapshot | None:
        """Snapshot the mind and the work. None when it could not be done."""
        turn, stores = ready.turn, self._stores
        try:
            result = await self._driver.run_step(save_step(**self._step_facts(ready)))
        except DriverError as e:
            record.add(f"save failed: {e}")
            return None
        record.add(result.output)
        snapshot = await stores.snapshots.of_turn(turn.org_id, turn.id) if result.exit_code == 0 else None
        if snapshot is not None:
            await stores.turns.volumes_hold(turn.org_id, turn.session_id, snapshot.id)
            await stores.turns.stage(turn, "saved")
        return snapshot
