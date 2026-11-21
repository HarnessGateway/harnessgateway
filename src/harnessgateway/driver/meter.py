"""What a Docker container used, read from the daemon while the container runs.

The daemon keeps a container's totals only while it runs, so they are read as they change: about one sample a
second from its stats stream, over the daemon's own socket.
"""

import asyncio
import contextlib
import json
import os
import time
from types import TracebackType
from typing import Any

import httpx

from harnessgateway.driver.base import Measured

_SOCKET = "unix:///var/run/docker.sock"
_UNTIL_IT_ENDS = httpx.Timeout(5, read=None)   # the stream is read for as long as the container runs
_NOT_THERE_YET_S = 0.2                         # how often a container `docker run` has not made yet is asked for


def with_sample(so_far: Measured, sample: dict[str, Any], elapsed_s: float) -> Measured:
    """What is known once one more sample is in, `elapsed_s` after the one before. Processor time is a running
    total: the highest seen. Memory is what the container held, less what is only the kernel's cache of files,
    for that long. A container that has ended gives empty samples, which change nothing."""
    cpu_ns = sample.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
    memory = sample.get("memory_stats", {})
    cache = memory.get("stats", {})
    held = max(memory.get("usage", 0) - cache.get("inactive_file", cache.get("total_inactive_file", 0)), 0)
    return Measured(max(so_far.cpu_seconds, cpu_ns / 1e9), so_far.memory_byte_seconds + held * elapsed_s)


class Meter:
    """Samples one container from when it appears until the block ends: `async with Meter(name) as meter`."""

    def __init__(self, container: str) -> None:
        self._container = container
        self.measured = Measured()
        self._reading: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Meter":
        host = os.environ.get("DOCKER_HOST", _SOCKET)
        if host.startswith("unix://"):             # a daemon reached any other way is not measured
            self._reading = asyncio.create_task(self._read(host.removeprefix("unix://")))
        return self

    async def __aexit__(self, kind: type[BaseException] | None, error: BaseException | None,
                        trace: TracebackType | None) -> None:
        if self._reading is not None:
            self._reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reading

    async def _read(self, socket: str) -> None:
        with contextlib.suppress(httpx.HTTPError, ValueError):       # a daemon that will not say: not measured
            transport = httpx.AsyncHTTPTransport(uds=socket)
            async with httpx.AsyncClient(transport=transport, timeout=_UNTIL_IT_ENDS) as daemon:
                while True:
                    async with daemon.stream("GET", f"http://docker/containers/{self._container}/stats") as answer:
                        if answer.status_code == 200:
                            last = time.monotonic()
                            async for line in answer.aiter_lines():
                                now = time.monotonic()
                                self.measured, last = with_sample(self.measured, json.loads(line), now - last), now
                            return
                    await asyncio.sleep(_NOT_THERE_YET_S)
