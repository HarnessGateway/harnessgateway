"""The launcher port (SPEC §6): start a box from the sandbox image with its two environment values, stop it, ask
whether it is alive. Docker on one host, Kubernetes on a cluster (HG_LAUNCHER); an extension may register another."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LaunchError(RuntimeError):
    """A box could not be started or stopped: the worker records the launch as failed and goes on."""


@dataclass(frozen=True)
class BoxSpec:
    box_id: str
    org_id: str
    token: str                     # the box token, as the runner receives it (HG_BOX_TOKEN)
    vcpus: float
    memory_mb: int
    tmp_mb: int
    pids: int
    kind: str                      # "session" | "install"

    def env(self, api_url: str, door: str) -> dict[str, str]:
        return {"HG_BOX_ID": self.box_id, "HG_BOX_TOKEN": self.token, "HG_API_URL": api_url, "HG_DOOR": door}


class Launcher(Protocol):
    async def start(self, spec: BoxSpec) -> str:
        """Start the box; the substrate's reference to it (a container id, a task ARN)."""
        ...

    async def stop(self, reference: str) -> None: ...

    async def alive(self, reference: str) -> bool: ...

    async def reap(self) -> int:
        """Remove what finished boxes left behind (networks, exited containers); how many."""
        ...
