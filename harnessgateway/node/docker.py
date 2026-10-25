"""The Docker CLI, the node's only way to touch containers. Arguments are passed as a list (never through a shell),
environment values are passed by name so none appears on a command line, and every call has a timeout."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

LABEL = "harnessgateway.node"


class DockerError(RuntimeError):
    pass


@dataclass
class Result:
    code: int
    out: bytes
    err: bytes

    def check(self, what: str) -> Result:
        if self.code != 0:
            raise DockerError(f"{what}: {self.err.decode(errors='replace').strip()[-500:]}")
        return self


async def docker(*args: str, input: bytes | None = None, timeout: float = 120, env: dict[str, str] | None = None
                 ) -> Result:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, **(env or {})})
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise DockerError(f"docker {args[0]} timed out after {timeout}s") from None
    return Result(proc.returncode or 0, out, err)


async def docker_file(*args: str, stdin_path: Path | None = None, stdout_path: Path | None = None,
                      timeout: float = 600) -> Result:
    """A docker call streaming a file in (stdin) or out (stdout) without holding it in memory."""
    fin = stdin_path.open("rb") if stdin_path else None
    fout = stdout_path.open("wb") if stdout_path else None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdin=fin or asyncio.subprocess.DEVNULL, stdout=fout or asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise DockerError(f"docker {args[0]} timed out after {timeout}s") from None
        return Result(proc.returncode or 0, out or b"", err)
    finally:
        for f in (fin, fout):
            if f:
                f.close()


def env_args(env: dict[str, str]) -> list[str]:
    """`-e NAME` for each variable: the value travels in the CLI's environment, not its argv."""
    out: list[str] = []
    for name in env:
        out += ["-e", name]
    return out


class Exec:
    """A running `docker exec`: stdout read line by line, stderr collected, the exit code at the end."""

    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc

    @classmethod
    async def start(cls, container: str, command: Sequence[str], *, user: str, workdir: str, env: dict[str, str],
                    stdin: bytes | None = None) -> Exec:
        args = ["exec", *(["-i"] if stdin is not None else []), "-u", user, "-w", workdir, *env_args(env)]
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, container, *command, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=2 * 1024 * 1024,
            env={**os.environ, **env})
        if stdin is not None:
            assert proc.stdin is not None
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()
        return cls(proc)

    async def lines(self) -> AsyncIterator[bytes]:
        assert self.proc.stdout is not None
        while True:
            try:
                line = await self.proc.stdout.readline()
            except ValueError:                             # a line over the stream limit: hand it on in pieces
                line = await self.proc.stdout.read(2 * 1024 * 1024)
            if not line:
                return
            yield line

    async def stderr(self) -> AsyncIterator[bytes]:
        assert self.proc.stderr is not None
        while chunk := await self.proc.stderr.read(65536):
            yield chunk

    async def wait(self) -> int:
        return await self.proc.wait()

    def kill(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
