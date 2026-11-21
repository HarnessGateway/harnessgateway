"""How a turn ended. What the platform saw from outside the sandbox outranks what the harness printed inside it."""

from dataclasses import dataclass
from typing import Any, Literal

FINAL = ("completed", "failed", "cancelled", "incomplete")
_KILLED = 128 + 9                              # the exit code of a process sent SIGKILL


@dataclass(frozen=True)
class Terminal:
    """The one line a harness's `run` ends with."""

    kind: Literal["done", "incomplete", "failed"]
    reason: str | None = None                  # incomplete: max_output_tokens | content_filter
    code: str | None = None                    # failed: the harness's own code
    message: str | None = None                 # failed: the harness's own words
    usage: dict[str, Any] | None = None        # the tokens it reports, in the Responses usage shape


@dataclass(frozen=True)
class Observed:
    """Everything known about a turn once it is over."""

    before: Literal["install_failed", "restore_failed", "sandbox_failed"] | None = None   # it never got to run
    why: str = ""                              # and why, in a few words
    stopped: str | None = None                 # why the platform stopped it: cancelled | timed_out | out_of_disk |
    started: bool = True                       # invalid_output | shutdown
    out_of_memory: bool = False
    startup_failed: bool = False               # the environment's startup script failed: the harness never ran
    exit_code: int | None = None
    invalid_output: str | None = None
    saved: bool = True
    terminal: Terminal | None = None


@dataclass(frozen=True)
class Outcome:
    status: Literal["completed", "failed", "cancelled", "incomplete"]
    reason: str | None = None                  # metadata.hg_reason: why, as one word a program can act on
    message: str | None = None                 # error.message: why, in plain words
    code: str | None = None                    # metadata.hg_code: the harness's own code
    incomplete_reason: str | None = None       # incomplete_details.reason


CANCELLED = Outcome("cancelled", "cancelled", "the turn was cancelled")
ABANDONED = Outcome("failed", "sandbox_failed", "the platform stopped while this turn was running")
_BEFORE = {"install_failed": "the harness could not be installed for this environment",
           "restore_failed": "the session's saved state could not be restored",
           "sandbox_failed": "the sandbox could not be started, or was lost"}
_STOPPED = {"timed_out": "the turn passed its environment's time limit and was stopped",
            "out_of_disk": "the session's files passed its environment's disk limit, and the turn was stopped"}


def decide(seen: Observed) -> Outcome:
    def failed(reason: str, message: str) -> Outcome:
        return Outcome("failed", reason, message)
    before = seen.before or ("sandbox_failed" if not seen.started or seen.stopped == "shutdown" else None)
    if before:
        return failed(before, _BEFORE[before] + (f": {seen.why}" if seen.why else ""))
    if seen.stopped == "cancelled":
        return CANCELLED
    if seen.stopped in _STOPPED:
        return failed(seen.stopped, _STOPPED[seen.stopped])
    if seen.out_of_memory or (seen.exit_code == _KILLED and seen.stopped is None):
        # Only the platform, whose stops are accounted for above, and the kernel's out-of-memory killer send
        # SIGKILL in a sandbox; a runtime can report the exit before it reports why.
        return failed("out_of_memory", "the turn passed its environment's memory limit and was killed")
    if seen.startup_failed:
        return failed("startup_failed", "the environment's startup script failed: the turn's log says how")
    if seen.invalid_output is not None:
        return failed("invalid_output", f"the harness printed a line that is not a valid event: {seen.invalid_output}")
    if not seen.saved:
        return failed("save_failed", "what the turn left could not be saved")
    said = seen.terminal
    if said and said.kind == "failed":
        return Outcome("failed", "harness_failed", said.message, said.code)
    if said and said.kind == "incomplete":
        return Outcome("incomplete", said.reason, f"the turn stopped early: {said.reason}",
                       incomplete_reason=said.reason)
    if said and seen.exit_code == 0:
        return Outcome("completed")
    if said:
        return failed("harness_error", f"the harness reported success but exited with code {seen.exit_code}")
    return failed("harness_error", f"the harness exited with code {seen.exit_code} without reporting a result")
