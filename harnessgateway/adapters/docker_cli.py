"""The Docker CLI, the Docker launcher's only way to touch containers. Arguments are passed as a list (never through a
shell), environment values are passed by name so none appears on a command line, and every call has a timeout."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from ..ports.launch import LaunchError

LABEL = "harnessgateway.box"


class DockerError(LaunchError):
    pass


@dataclass(frozen=True)
class Result:
    code: int
    out: bytes
    err: bytes

    def check(self, what: str) -> Result:
        if self.code != 0:
            raise DockerError(f"{what}: {self.err.decode(errors='replace').strip()[-500:]}")
        return self


async def docker(*args: str, timeout: float = 120, env: dict[str, str] | None = None) -> Result:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={**os.environ, **(env or {})})
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise DockerError(f"docker {args[0]} timed out after {timeout}s") from None
    return Result(proc.returncode or 0, out, err)


def env_args(env: dict[str, str]) -> list[str]:
    """`-e NAME` for each variable: the value travels in the CLI's environment, not its argv."""
    return [a for name in env for a in ("-e", name)]
