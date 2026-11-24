"""Running one turn: install, restore, run, save, conclude."""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from harnessgateway.domain.contract import CACHE, STARTUP_FAILED
from harnessgateway.domain.egress import Policy
from harnessgateway.domain.events import LineError, RunOutput, storable
from harnessgateway.domain.handover import NOTE_MAX, handover, note_path, pointer, stints
from harnessgateway.domain.outcome import Observed, decide
from harnessgateway.domain.request import file_ids
from harnessgateway.domain.resolve import Resolved
from harnessgateway.domain.sandbox import (
    SandboxPlan,
    bind_secrets,
    session_folders,
    turn_folder,
    turn_plan,
    turn_policy,
)
from harnessgateway.domain.snapshot import Manifest, ManifestError
from harnessgateway.driver.base import DriverError, Measured, SandboxExit
from harnessgateway.errors import Conflict, Refused
from harnessgateway.ids import new_placeholder, new_token
from harnessgateway.platform import Platform
from harnessgateway.runner.installer import Installer, InstallFailed
from harnessgateway.store.blobs import BlobMissing
from harnessgateway.store.snapshots import Snapshot
from harnessgateway.store.turns import LOG_MAX, Turn

log = logging.getLogger(__name__)

_MIB = 1024 * 1024
_STOP_AGAIN_S = 1.0
_MEASURE_EVERY_S = 2.0


class _Log:
    """The end of everything said about a turn: what its sandbox printed to standard error (its harness, the
    agent, the environment's startup script), and what the platform noted."""

    def __init__(self) -> None:
        self._lines: deque[str] = deque()
        self._size = 0

    def add(self, text: str) -> None:
        self._lines.append(storable(text))
        self._size += len(text) + 1
        while self._size > LOG_MAX and len(self._lines) > 1:
            self._size -= len(self._lines.popleft()) + 1

    def text(self) -> str:
        return "\n".join(self._lines)[-LOG_MAX:]


class _Stop:
    """A request to stop a turn's sandbox, and why. The first reason given is the one that counts."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.asked = asyncio.Event()

    def ask(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
            self.asked.set()


class _Failed(Exception):
    """A turn could not get as far as its sandbox: it ends here."""

    def __init__(self, before: str, detail: str) -> None:
        super().__init__(detail)
        self.seen = Observed(before=before, why="the turn's log says how")   # type: ignore[arg-type]


@dataclass(frozen=True)
class _Ready:
    """A turn with everything decided, about to run."""

    turn: Turn
    resolved: Resolved
    plan: SandboxPlan
    parent: Manifest                           # what the turn it continues from left: empty when it continues from none


class TurnRunner:
    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self._installer = Installer(platform)
        self._stops: dict[str, _Stop] = {}

    def stop(self, turn_id: str, reason: str) -> None:
        """Ask for a turn that is running here to be stopped: `cancelled`, or `shutdown`."""
        if turn_id in self._stops:
            self._stops[turn_id].ask(reason)

    async def run(self, turn: Turn) -> Turn:
        """Take a claimed turn to its end. It never raises for what goes wrong inside the turn."""
        platform, org_id = self._platform, turn.org_id
        stop = self._stops.setdefault(turn.id, _Stop())
        record, output, used = _Log(), RunOutput(), Measured()
        snapshot: Snapshot | None = None
        try:
            ready = await self._prepare(turn)
            seen, used = await self._sandbox(ready, stop, output, record)
            if seen.started and turn.store:        # whatever happened inside, what it left is kept
                snapshot = await self._save(ready, record)
                seen = replace(seen, saved=snapshot is not None)
        except _Failed as failure:
            record.add(str(failure))
            seen = failure.seen
        except Exception as error:                 # a fault of the platform's own: say so, and end the turn
            log.exception("turn %s failed inside the platform", turn.id)
            record.add(f"the platform failed while running this turn: {error!r}")
            seen = Observed(before="sandbox_failed")
        finally:
            self._stops.pop(turn.id, None)
            await platform.grants.end(turn.id)
            await platform.driver.remove(turn_folder(turn.id))
        said = output.terminal
        try:
            final = await platform.turns.conclude(
                turn, decide(seen), output=output.output(), usage=said.usage if said else None,
                snapshot_id=snapshot.id if snapshot else None, log=record.text())
        except Conflict:                           # ended meanwhile, by whoever thought this runner was gone
            return await platform.turns.get(org_id, turn.id)
        await platform.announce("turn.finished", org_id, target=final.id, session_id=final.session_id,
                                model=str(final.line), status=final.status, reason=final.reason, **asdict(used))
        return final

    async def _prepare(self, turn: Turn) -> _Ready:
        """Decide everything the turn runs with, make sure its harness is installed for its environment's image,
        put the session's folders and the turn's inputs in place, and say what its sandbox may reach."""
        platform, org_id, line, driver = self._platform, turn.org_id, turn.line, self._platform.driver
        resources = platform.resources
        try:
            resolved = await resources.resolve(org_id, line)
            image_id = await driver.image_id(resolved.environment.image)
        except (Refused, DriverError) as error:
            raise _Failed("sandbox_failed", str(error)) from None
        try:
            install_id = await self._installer.ensure(org_id, resolved, image_id)
        except InstallFailed as error:
            raise _Failed("install_failed", f"install failed:\n{error}") from None
        environment = resolved.environment

        chain = await platform.turns.chain(org_id, turn.id)
        parent_snapshot = chain[-1].snapshot_id if chain else None
        session = await platform.sessions.get(org_id, turn.session_id)
        previous = chain[-1].response() if chain and chain[-1].deleted_at is None else None
        inputs = {"request.json": json.dumps(turn.request).encode(), "previous.json": json.dumps(previous).encode()}
        if environment.scripts.startup:            # run as a program: its first line may name its interpreter
            inputs["startup.sh"] = (environment.scripts.startup.rstrip() + "\n").encode()
        try:
            files = [await platform.files.get(org_id, file_id) for file_id in file_ids(turn.request or {})]
            parent = await platform.snapshots.manifest(org_id, parent_snapshot)
            inputs["handover.txt"] = (await self._handover(turn, chain, parent)).encode()
            if not chain or session.folder_snapshot_id != parent_snapshot:       # the folders hold something else
                await driver.restore(session_folders(turn.session_id), parent, platform.snapshots.chunks(org_id))
            await driver.fill(turn_folder(turn.id), inputs, executable=["startup.sh"], streams={
                f"files/{file.id}/{file.filename}": platform.files.content(org_id, file.id) for file in files})
        except (Refused, ManifestError, BlobMissing, DriverError, OSError) as error:
            raise _Failed("restore_failed", f"restore failed: {error}") from None

        key = (await resources.secrets(org_id, "providers", line.provider)).get("key") if resolved.provider else None
        bound, placeholders, provider_key = bind_secrets(
            resolved, provider_key=key, new_placeholder=new_placeholder,
            environment_secrets=await resources.secrets(org_id, "environments", line.environment))
        bound += await platform.turns.credentials(turn)        # the request's own, bound to where it said they go
        proxy_token = new_token()
        await platform.grants.put(Policy(org_id, turn.id, line.environment, turn_policy(resolved), bound), proxy_token)
        plan = turn_plan(session_id=turn.session_id, turn_id=turn.id, install_id=install_id, image_id=image_id,
                         resolved=resolved, proxy_token=proxy_token, provider_key=provider_key,
                         placeholders=placeholders)
        return _Ready(turn, resolved, plan, parent)

    async def _handover(self, turn: Turn, chain: Sequence[Turn], parent: Manifest) -> str:
        """What the turn's harness is told when it takes the session over from another: who worked on it, each
        with its package's own note of where its memory is, read from what the turn before left."""
        worked = stints([(earlier.id, earlier.harness, str(earlier.line)) for earlier in chain])
        notes: dict[str, str | None] = {}
        for harness in sorted({stint.harness for stint in worked} - {turn.harness}):
            note = parent.entries.get(note_path(harness).lstrip("/"))     # a manifest's paths begin with no slash
            notes[harness] = None if note is None or note.kind != "f" else pointer(harness) if note.size > NOTE_MAX \
                else await self._platform.snapshots.text(turn.org_id, note)
        return handover(worked, turn.harness, notes)

    async def _sandbox(self, ready: _Ready, stop: _Stop, output: RunOutput, record: _Log,
                       ) -> tuple[Observed, Measured]:
        """Run the sandbox to its end, with its disk measured beside it, the time limit over it, and a stop
        honoured. What was seen of it, and what it used."""
        platform, turn, driver = self._platform, ready.turn, self._platform.driver
        environment, folders = ready.resolved.environment, session_folders(ready.turn.session_id)
        invalid: list[str] = []

        async def on_stdout(text: str) -> None:
            if invalid:
                return
            try:
                events = output.read(text)
            except LineError as error:
                invalid.append(str(error))
                return stop.ask("invalid_output")
            for event in events:
                await platform.turns.add_event(turn, event)

        async def on_stderr(text: str) -> None:
            record.add(text)

        async def measure() -> None:               # the session's folders, against the environment's disk limit
            while True:
                await asyncio.sleep(_MEASURE_EVERY_S)
                held = await driver.measure(folders)
                if held > environment.disk_mb * _MIB:
                    record.add(f"the session's folders hold {held // _MIB} MiB, over the limit of "
                               f"{environment.disk_mb} MiB")
                    return stop.ask("out_of_disk")

        async def limit() -> None:
            await asyncio.sleep(environment.timeout_s)
            stop.ask("timed_out")

        async def enforce() -> None:               # keep asking until it has ended: a stop that arrives before
            await stop.asked.wait()                # the sandbox exists must not be lost
            while not sandbox.done():
                await driver.stop(ready.plan.name, ready.plan.stop_grace_s)
                await asyncio.sleep(_STOP_AGAIN_S)

        await platform.sessions.folders_hold(turn.org_id, turn.session_id, None)     # they are about to change
        if stop.reason is None:
            sandbox = asyncio.create_task(driver.run(ready.plan, on_stdout, on_stderr))
            guards = [asyncio.create_task(guard()) for guard in (measure, limit, enforce)]
            try:
                result = await sandbox
            except DriverError as error:
                result = SandboxExit(started=False, error=str(error))
            finally:
                for guard in guards:
                    guard.cancel()
                await asyncio.gather(*guards, return_exceptions=True)
        else:
            result = SandboxExit(started=True)     # stopped before it began
        await platform.usage.add(turn.org_id, **asdict(result.measured))
        if not result.started:
            record.add(f"the sandbox could not be started: {result.error}")
        # The startup script failed when the sandbox says so and the harness, which would have followed, said nothing.
        startup_failed = bool(environment.scripts.startup and result.exit_code == STARTUP_FAILED
                              and output.terminal is None and output.events == 0)
        return Observed(stopped=stop.reason, started=result.started, out_of_memory=result.out_of_memory,
                        startup_failed=startup_failed, exit_code=result.exit_code,
                        invalid_output=invalid[0] if invalid else None, terminal=output.terminal), result.measured

    async def _save(self, ready: _Ready, record: _Log) -> Snapshot | None:
        """Snapshot the mind and the work. None when it could not be done."""
        platform, turn, environment = self._platform, ready.turn, ready.resolved.environment
        org_id = turn.org_id
        try:
            manifest = await platform.driver.save(
                session_folders(turn.session_id), ready.parent, frozenset({f"mind/{CACHE}"}),
                platform.snapshots.chunks(org_id))
            if manifest.size() > environment.disk_mb * _MIB:
                record.add(f"not saved: the session's folders hold {manifest.size() // _MIB} MiB, and the "
                           f"environment's disk limit is {environment.disk_mb} MiB")
                return None
            snapshot = await platform.snapshots.record(org_id, session_id=turn.session_id, turn_id=turn.id,
                                                       manifest=manifest)
        except (Refused, ManifestError, BlobMissing, DriverError, OSError) as error:
            record.add(f"save failed: {error}")
            return None
        await platform.sessions.folders_hold(org_id, turn.session_id, snapshot.id)
        return snapshot
