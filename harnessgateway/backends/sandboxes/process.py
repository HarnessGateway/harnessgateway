"""Backend: one operating-system process as an Execution, and the development node that runs turns as such. The only code that spawns a process, streams
its output, feeds its input, stops it, kills it and times it out; the local and Docker runtimes both
run their process through it. It also holds the allowlist of host variables a child may inherit.

The process gets its own process group, so a stop or a kill reaches everything it started, and what
it leaves running when it exits is killed. Its output is read as soon as it is written, whether or
not anyone is consuming events, so a process never blocks on a full pipe and wait() never waits on a
reader. A stop signals once and arms a kill; an exit after a stop is `stopped`, a kill is `killed`.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import threading
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Mapping, Sequence

from ...errors import Invalid
from ...kernel.execution import Event, Execution, Kind, NeverStarted, Outcome, Spec
from ...contract.package import Config
from ...kernel.limits import Limits
from . import MARKER, InstallFailed, describe, locked

STREAMS = ("stdout", "stderr")
GONE = (BrokenPipeError, ConnectionResetError, RuntimeError)   # it stopped reading; RuntimeError: a closed transport
# The only host variables a child inherits: names, never patterns like *_KEY, so no stray credential
# in the server's environment ever reaches a harness or a Docker client.
ALLOWED = re.compile(r"PATH|HOME|LANG|LC_\w+|TERM|TZ|USER|LOGNAME|HTTPS?_PROXY|https?_proxy|NO_PROXY|no_proxy|"
                     r"SSL_CERT_FILE|SSL_CERT_DIR|NODE_EXTRA_CA_CERTS|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|GIT_SSL_CAINFO|"
                     r"DOCKER_HOST|DOCKER_CONFIG|DOCKER_CONTEXT|DOCKER_CERT_PATH|DOCKER_TLS_VERIFY")

Action = Callable[[], "Awaitable[object] | None"]


def host_env() -> dict[str, str]:
    """The part of this process's environment a child may inherit."""
    return {k: v for k, v in os.environ.items() if ALLOWED.fullmatch(k)}


class ProcessExecution:
    def __init__(self, proc: asyncio.subprocess.Process, timeout_s: float | None, interactive: bool,
                 interrupt: Action | None, kill: Action | None, cleanup: Callable[[], Awaitable[None]] | None,
                 sidecars: Mapping[str, asyncio.StreamReader], stop_signal: int = signal.SIGINT):
        self._proc, self._cleanup, self._stop_signal = proc, cleanup, stop_signal
        self._signal = interrupt or self._interrupt_group
        self._kill = kill
        self._loop = asyncio.get_running_loop()
        self._stopping = False                       # asked to stop: an exit now is `stopped`
        self._forced: Kind | None = None             # "killed" or "timed_out", once we ended it
        self._kill_at: asyncio.TimerHandle | None = None
        self._input_open = interactive
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._pumps = [asyncio.create_task(self._pump(name, getattr(proc, name))) for name in STREAMS]
        self._sidecars = [asyncio.create_task(self._pump(name, r, last=False)) for name, r in sidecars.items()]
        self._timer = self._loop.call_later(timeout_s, self._force, "timed_out") if timeout_s else None
        self._iterated = False
        self._chores: set[asyncio.Future] = set()    # signals and kills in flight, kept until done
        self._finished = asyncio.ensure_future(self._finish())   # teardown happens whether or not anyone waits

    @classmethod
    async def spawn(cls, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None, input: bytes,
                    interactive: bool, timeout_s: float | None, signal: Action | None = None,
                    kill: Action | None = None, cleanup: Callable[[], Awaitable[None]] | None = None,
                    sidecars: Mapping[str, asyncio.StreamReader] | None = None, stop_signal: int = 2) -> Execution:
        """Start the process and write its input. A process that can't start is a NeverStarted.
        `signal` asks it to stop (by default, `stop_signal`, SIGINT, to its group); `kill` ends it where
        it runs, before its group is killed; `cleanup` runs once it has ended; `sidecars` are more
        streams to read as events until then."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=dict(env), start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            if cleanup:
                await cleanup()
            return NeverStarted(f"could not start {argv[0]}: {e}")
        execution = cls(proc, timeout_s, interactive, signal, kill, cleanup, sidecars or {}, stop_signal)
        try:
            try:
                await execution._write(input)
            except GONE:
                pass                                 # it exited without reading, which it need not do
            if not interactive:
                execution._close_input()
        except BaseException:                        # cancelled while starting: leave nothing running
            execution._force("killed")
            await execution.wait()
            raise
        return execution

    # the Execution interface
    async def events(self) -> AsyncIterator[Event]:
        assert not self._iterated, "events() can be iterated once"
        self._iterated = True
        open_streams = len(STREAMS)
        while open_streams:
            event = await self._queue.get()
            if event is None:
                open_streams -= 1
            else:
                yield event

    async def send(self, data: bytes) -> None:
        if not self._input_open or self._stopping or self._forced or self._ended:
            raise Invalid("its input is closed")
        try:
            await self._write(data)
        except GONE:
            self._input_open = False
            raise Invalid("its input is closed: it stopped reading") from None

    async def end_input(self) -> None:
        self._close_input()

    def stop(self, timeout_s: float) -> None:
        if not timeout_s >= 0:
            raise Invalid("a stop's timeout must not be negative")
        self._soon(self._stop, timeout_s)

    def kill(self) -> None:
        self._soon(self._force, "killed")

    async def wait(self) -> Outcome:
        return await asyncio.shield(self._finished)

    async def interrupt(self, name: str) -> bool:
        """A named signal (`SIGUSR1`…) to the process itself, not its group; False once it has ended."""
        if self._ended:
            return False
        try:
            os.kill(self._proc.pid, getattr(signal, name))
            return True
        except (ProcessLookupError, AttributeError):
            return False

    # the process
    @property
    def _ended(self) -> bool:
        return self._proc.returncode is not None

    def _soon(self, action: Callable[..., None], *args: object) -> None:
        """Run it now on the loop's own thread, or hand it to the loop from any other."""
        try:
            here = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            here = False
        if here:
            action(*args)
            return
        try:
            self._loop.call_soon_threadsafe(action, *args)
        except RuntimeError:
            pass                                     # its loop is closed: it ended long ago

    def _stop(self, timeout_s: float) -> None:
        if self._ended or self._forced:
            return
        if not self._stopping:
            self._stopping = True
            self._chore(self._signal())
        deadline = self._loop.time() + timeout_s
        if self._kill_at is None or deadline < self._kill_at.when():
            if self._kill_at:
                self._kill_at.cancel()
            self._kill_at = self._loop.call_at(deadline, self._force, "killed")

    def _force(self, why: Kind) -> None:
        if self._ended or self._forced:
            return                                   # it ended already: the first reason stands
        self._forced = why
        self._chore(self._end())

    async def _end(self) -> None:
        """Kill it where it runs, then its process group here, so nothing outlives a kill."""
        if self._kill and (result := self._kill()) is not None:
            await result
        self._kill_group()

    def _chore(self, result: Awaitable[object] | None) -> None:
        if result is not None:
            chore = asyncio.ensure_future(result)
            self._chores.add(chore)
            chore.add_done_callback(self._chores.discard)

    async def _pump(self, name: str, stream: asyncio.StreamReader, last: bool = True) -> None:
        try:
            while chunk := await stream.read(65536):
                self._queue.put_nowait(Event(name, chunk))
        finally:
            if last:
                self._queue.put_nowait(None)

    async def _write(self, data: bytes) -> None:
        stdin = self._proc.stdin
        assert stdin is not None
        stdin.write(data)
        await stdin.drain()
        if stdin.is_closing():                       # the pipe broke under the write: the bytes went nowhere
            raise BrokenPipeError

    def _close_input(self) -> None:
        self._input_open = False
        try:
            self._proc.stdin.close()
        except GONE:
            pass

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self._proc.pid, sig)
        except ProcessLookupError:
            pass

    def _interrupt_group(self) -> None:
        self._signal_group(self._stop_signal)

    def _kill_group(self) -> None:
        self._signal_group(signal.SIGKILL)

    def _exited(self) -> asyncio.Future[None]:
        """Done when the process exits, though what it left running may hold its output open (and
        Python 3.11's wait() waits for that too). It reaps nothing: that stays asyncio's."""
        done = self._loop.create_future()

        def watch() -> None:
            try:
                os.waitid(os.P_PID, self._proc.pid, os.WEXITED | os.WNOWAIT)
            except ChildProcessError:
                pass                                 # reaped already: it exited
            try:
                self._loop.call_soon_threadsafe(lambda: done.done() or done.set_result(None))
            except RuntimeError:
                pass                                 # its loop is closed: nobody is waiting
        threading.Thread(target=watch, daemon=True).start()
        return done

    async def _finish(self) -> Outcome:
        await self._exited()
        self._kill_group()                           # whatever it left running, holding its output open
        code = await self._proc.wait()
        await asyncio.gather(*self._pumps)           # every byte it wrote is read before it is over
        for timer in (self._timer, self._kill_at):
            if timer:
                timer.cancel()
        for chore in self._chores:
            chore.cancel()                           # a signal or kill still trying has nothing left to reach
        await asyncio.gather(*self._chores, return_exceptions=True)
        if self._cleanup:
            await self._cleanup()
        for sidecar in self._sidecars:
            sidecar.cancel()
        if self._forced:
            return Outcome(self._forced)
        return Outcome.stopped(code) if self._stopping else Outcome.exited(code)


# the development node
class ProcessNode:
    """A node without Docker, for development only: install.sh and every turn run as processes on
    this machine, with no isolation at all (`harnessgateway serve --dev`). Never for anyone else's
    packages."""
    image = "process"

    def __init__(self, root: Path, gateway_url: str = ""):
        self.root = Path(root)
        self.gateway_url = gateway_url                 # the gateway as a process on this machine reaches it

    async def prepare(self) -> str:
        return "process"

    async def recover(self) -> None:
        pass

    def runtime_for(self, scope: str, limits: Limits, *, read_only: Sequence[Path] = (), writable: Sequence[Path] = (),
                    egress: Sequence[str] = (), gateway_url: str = "", token: str = ""):
        # no door here: the CLI calls the gateway itself, with the session token in a file of the turn's
        rundir = next((p for p in writable if p.name == "run"), None) or (writable[-1] if writable else self.root)
        rundir.mkdir(parents=True, exist_ok=True)
        token_file = rundir / ".token"
        token_file.write_text(token)
        token_file.chmod(0o600)

        class Runtime:
            provider_base = self.gateway_url or gateway_url   # this node's own, else the job's; the worker appends the provider's path
            token_path = token_file

            async def start(self, spec: Spec):
                env = {**host_env(), **spec.env}
                env["PATH"] = f"{spec.env.get('PATH', '')}:{host_env().get('PATH', '/usr/bin:/bin')}"
                return await ProcessExecution.spawn(list(spec.command), env=env, cwd=spec.workspace, input=spec.input,
                                                    interactive=spec.interactive, timeout_s=spec.timeout_s, stop_signal=15)

            async def recover(self) -> None:
                pass
        return Runtime()

    async def tools_for(self, scope: str, limits: Limits, folder: Path, config: Config, root: Path, image_id: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        tools, log = root / f"{folder.name}-process", root / f"{folder.name}-process.log"
        async with locked(root / f".{tools.name}.lock"):
            if (tools / MARKER).is_file():
                return tools
            shutil.rmtree(tools, ignore_errors=True)
            (tools / "bin").mkdir(parents=True)
            env = {**host_env(), "HG_TOOLS": str(tools), "HG_HARNESS_DIR": str(folder), "HOME": str(tools)}
            execution = await ProcessExecution.spawn(["bash", str(folder / "install.sh")], env=env, cwd=tools, input=b"", interactive=False,
                                                     timeout_s=min(config.install_timeout_s, limits.install_timeout_s), stop_signal=15)
            with open(log, "wb") as f:
                async for event in execution.events():
                    f.write(event.data)
            outcome = await execution.wait()
            if not outcome.ok:
                shutil.rmtree(tools, ignore_errors=True)
                raise InstallFailed(f"install.sh {describe(outcome)}; its log ends:\n{log.read_text(errors='replace')[-2000:]}")
            (tools / MARKER).write_text(json.dumps({"image": "process"}))
            return tools
