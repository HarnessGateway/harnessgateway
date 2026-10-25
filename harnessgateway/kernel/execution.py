"""Layer 0: what running something means, whatever runs and wherever it runs.

A Runtime starts a Spec and returns an Execution. While it runs, the Execution yields Events, bytes
on named streams as they arrive; it takes more input if the Spec kept its input open; it can be
asked to stop (a signal now, a kill if it hasn't ended in time) or killed, from any thread; and it
ends in exactly one Outcome. Nothing here knows about harnesses, models, files, containers or HTTP.
A new place to run things is a new Runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import AsyncIterator, Literal, Mapping, Protocol

from ..errors import Invalid


@dataclass(frozen=True)
class Spec:
    """What to run. The runtime interprets `command`: for a process, it is the argv."""
    command: tuple[str, ...]
    input: bytes = b""                               # written first
    env: Mapping[str, str] = field(default_factory=dict)   # all it is given: nothing else of the host's
    workspace: Path | None = None                    # where it works, if it works on files
    timeout_s: float | None = None                   # then it is killed
    interactive: bool = False                        # keep input open for send(); otherwise it closes after `input`

    def __post_init__(self) -> None:
        command, env = tuple(self.command), dict(self.env)
        if not command or not all(isinstance(a, str) for a in command) or not command[0]:
            raise Invalid("command must be a non-empty list of strings, starting with what to run")
        if not isinstance(self.input, bytes):
            raise Invalid("input must be bytes")
        if not all(isinstance(k, str) and k and "=" not in k and "\0" not in k and isinstance(v, str) and "\0" not in v
                   for k, v in env.items()):
            raise Invalid("env must map names to strings")
        if self.timeout_s is not None and not self.timeout_s > 0:
            raise Invalid("timeout_s must be positive")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "env", MappingProxyType(env))
        if self.workspace is not None:
            object.__setattr__(self, "workspace", Path(self.workspace))


@dataclass(frozen=True)
class Event:
    """Bytes on one stream, as they arrived: "stdout" and "stderr" for a process."""
    stream: str
    data: bytes

    def __post_init__(self) -> None:
        assert self.stream and isinstance(self.data, bytes) and self.data, "an event is some bytes on a named stream"


Kind = Literal["exited", "timed_out", "stopped", "killed", "failed"]
KINDS = ("exited", "timed_out", "stopped", "killed", "failed")


@dataclass(frozen=True)
class Outcome:
    """How an execution ended. Exactly one of: it exited by itself, with a code; it ran past its
    timeout and was killed; it was asked to stop and exited, with a code; it was killed; or it
    failed to start at all, with the reason."""
    kind: Kind
    code: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        assert self.kind in KINDS, self.kind
        has_code = isinstance(self.code, int) and not isinstance(self.code, bool)
        assert (self.kind in ("exited", "stopped")) == has_code, "an exit, and only an exit, has a code"
        assert (self.kind == "failed") == bool(self.reason), "a failure, and only a failure, has a reason"

    @property
    def ok(self) -> bool:
        return self.kind == "exited" and self.code == 0

    @classmethod
    def exited(cls, code: int) -> Outcome:
        return cls("exited", code)

    @classmethod
    def timed_out(cls) -> Outcome:
        return cls("timed_out")

    @classmethod
    def stopped(cls, code: int) -> Outcome:
        return cls("stopped", code)

    @classmethod
    def killed(cls) -> Outcome:
        return cls("killed")

    @classmethod
    def failed(cls, reason: str) -> Outcome:
        return cls("failed", reason=reason)


class Execution(Protocol):
    """One thing running."""

    def events(self) -> AsyncIterator[Event]:
        """Its output as it arrives; the iteration ends when it does. Iterate once."""
        ...

    async def send(self, data: bytes) -> None:
        """More input, delivered or refused: it raises Invalid unless its Spec was interactive, it is
        still running, it has not been asked to stop, and the bytes were written."""
        ...

    async def end_input(self) -> None:
        """No more input: what it reads next is the end."""
        ...

    def stop(self, timeout_s: float) -> None:
        """Ask it to stop: signal it now, and kill it if it hasn't ended `timeout_s` later. A later
        stop only brings the kill closer; it never signals twice. Safe from any thread; nothing once
        it has ended."""
        ...

    def kill(self) -> None:
        """End it now. Safe from any thread; nothing once it has ended."""
        ...

    async def wait(self) -> Outcome:
        """Wait for its end, and say how it ended. The same Outcome every time."""
        ...


class Runtime(Protocol):
    """A place to run things, with the guarantees it documents (isolation, limits, network)."""

    async def start(self, spec: Spec) -> Execution:
        """Start it. A failure to start is an Execution whose Outcome is `failed`, not an
        exception; cancelling this call stops the start and leaves nothing behind."""
        ...

    async def recover(self) -> None:
        """Remove whatever runs cut off by a crash left behind."""
        ...


class NeverStarted:
    """The Execution of something that could not start: no output, no input, and a `failed` Outcome."""

    def __init__(self, reason: str):
        self._outcome = Outcome.failed(reason)

    async def events(self) -> AsyncIterator[Event]:
        return
        yield                                        # an async generator that yields nothing

    async def send(self, data: bytes) -> None:
        raise Invalid("it never started")

    async def end_input(self) -> None:
        raise Invalid("it never started")

    def stop(self, timeout_s: float) -> None:
        pass

    def kill(self) -> None:
        pass

    async def wait(self) -> Outcome:
        return self._outcome
