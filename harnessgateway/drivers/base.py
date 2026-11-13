"""What a driver must be able to do: turn a plan into something that runs (spec §5.5)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from harnessgateway.domain.plan import SandboxPlan, StepPlan

OnLine = Callable[[str], Awaitable[None]]


class DriverError(Exception):
    """The infrastructure refused or failed. The message is what it said."""


@dataclass(frozen=True)
class SandboxExit:
    started: bool                      # False: the sandbox never ran; `error` says why
    exit_code: int | None = None
    out_of_memory: bool = False
    error: str = ""


@dataclass(frozen=True)
class StepResult:
    exit_code: int
    output: str                        # the end of what the step printed


class Driver(Protocol):
    name: str

    async def isolation(self) -> str:
        """`gvisor`, `none`, …: what separates a sandbox from the host's kernel."""

    async def image_id(self, ref: str) -> str:
        """The immutable id of an image reference, fetching the image if this host does not have it."""

    async def describe(self, plan: SandboxPlan) -> str:
        """The plan as this driver will run it, for an operator to read: every token masked."""

    async def ensure_volume(self, name: str) -> None: ...

    async def remove_volume(self, name: str) -> None: ...

    async def run_step(self, step: StepPlan) -> StepResult:
        """Run a trusted step to its end."""

    async def run_sandbox(self, plan: SandboxPlan, on_stdout: OnLine, on_stderr: OnLine) -> SandboxExit:
        """Run a sandbox to its end, handing over each line it prints as it prints it."""

    async def stop(self, name: str, grace_s: int) -> None:
        """Stop a sandbox or a step by name. Stopping what is not running is not an error."""

    async def reap(self, keep: set[str]) -> list[str]:
        """Remove every sandbox and step of this platform whose name is not in `keep`. The names removed."""
