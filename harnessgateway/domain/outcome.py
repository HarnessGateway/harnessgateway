"""Deciding how a turn ended (spec §5.4). What the platform saw outside the sandbox wins over what was printed."""

from dataclasses import dataclass
from typing import Any, Literal

KILLED = 128 + 9                                       # the exit code of a process sent SIGKILL


@dataclass(frozen=True)
class Terminal:
    """The terminal line a package printed (spec §8.7)."""

    kind: Literal["done", "incomplete", "failed"]
    reason: str | None = None                         # incomplete: max_output_tokens | content_filter
    code: str | None = None                           # failed: the package's own code
    message: str | None = None                        # failed: the package's own words
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class Measured:
    """What a sandbox used while it ran (spec §22.1), as its driver measured it."""

    cpu_seconds: float                 # processor time, all processors added up
    memory_peak_bytes: int             # the most memory it held at once


@dataclass(frozen=True)
class Observed:
    """Everything known about a turn once its steps are over."""

    setup_failed: bool = False
    install_failed: bool = False
    restore_failed: bool = False
    sandbox_failed: bool = False
    cancelled: bool = False
    timed_out: bool = False
    out_of_disk: bool = False
    out_of_memory: bool = False
    invalid_output: str | None = None
    save_failed: bool = False
    exit_code: int | None = None
    terminal: Terminal | None = None
    measured: Measured | None = None                  # None: the sandbox did not run, or its driver cannot measure


@dataclass(frozen=True)
class Outcome:
    status: Literal["completed", "failed", "cancelled", "incomplete"]
    reason: str | None = None                         # metadata.hg_reason
    message: str | None = None                        # error.message, in plain words
    code: str | None = None                           # metadata.hg_code
    incomplete_reason: str | None = None              # incomplete_details.reason


def _failed(reason: str, message: str) -> Outcome:
    return Outcome("failed", reason, message)


def decide(seen: Observed) -> Outcome:
    if seen.setup_failed:
        return _failed("setup_failed", "the environment's setup script failed")
    if seen.install_failed:
        return _failed("install_failed", "the harness could not be installed for this environment")
    if seen.restore_failed:
        return _failed("restore_failed", "the session's saved state could not be restored")
    if seen.sandbox_failed:
        return _failed("sandbox_failed", "the sandbox could not be started, or was lost")
    if seen.cancelled:
        return Outcome("cancelled", "cancelled", "the turn was cancelled")
    if seen.timed_out:
        return _failed("timed_out", "the turn passed its environment's time limit and was stopped")
    if seen.out_of_disk:
        return _failed("out_of_disk", "the session's files passed the environment's disk limit; the turn was stopped")
    if seen.out_of_memory:
        return _failed("out_of_memory", "the turn passed its environment's memory limit and was stopped")
    if seen.exit_code == KILLED:
        # Only the platform, whose stops are known above, and the kernel's out-of-memory killer send SIGKILL in a
        # sandbox. Docker can report the exit before the kernel's event reaches it, so its flag may still be false.
        return _failed("out_of_memory", "the harness was killed (exit 137), most likely by its environment's "
                                        "memory limit")
    if seen.invalid_output is not None:
        return _failed("invalid_output", f"the harness printed a line that is not a valid event: {seen.invalid_output}")
    if seen.save_failed:
        return _failed("save_failed", "the turn's state could not be saved")
    terminal = seen.terminal
    if terminal and terminal.kind == "failed":
        return Outcome("failed", "harness_failed", terminal.message or "the harness reported a failure", terminal.code)
    if terminal and terminal.kind == "incomplete":
        return Outcome("incomplete", terminal.reason, f"the turn stopped early: {terminal.reason}",
                       incomplete_reason=terminal.reason)
    if terminal and seen.exit_code == 0:
        return Outcome("completed")
    if terminal:
        return _failed("harness_error", f"the harness reported success but exited with code {seen.exit_code}")
    return _failed("harness_error", f"the harness exited with code {seen.exit_code} without reporting a result")
