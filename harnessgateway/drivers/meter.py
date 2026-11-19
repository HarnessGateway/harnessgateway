"""What a Docker container used, read from the daemon while the container runs (spec §22.1).

The daemon keeps a container's running totals only while it runs, so they are read as they change: one sample
a second from its stats stream, over the daemon's own socket.
"""

import asyncio
import contextlib
import functools
import json
import os
from collections.abc import Iterable
from typing import Any

import httpx

from harnessgateway.domain.outcome import Measured

_DEFAULT_HOST = "unix:///var/run/docker.sock"
_UNTIL_IT_ENDS = httpx.Timeout(5, read=None)    # the stream is read for as long as the container runs
_FIRST_SAMPLE_S = 0.2                      # how often a container that is not there yet is asked for again


def with_sample(so_far: Measured | None, sample: dict[str, Any]) -> Measured:
    """What is known once one more sample is in. Processor time is a running total: the highest seen. Memory is
    the peak the kernel kept, where it keeps one, and else the most any sample showed. A container that has
    ended gives empty samples, which change nothing."""
    before = so_far or Measured(cpu_seconds=0.0, memory_peak_bytes=0)
    cpu_ns = sample.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
    held = sample.get("memory_stats", {})
    return Measured(
        cpu_seconds=max(before.cpu_seconds, cpu_ns / 1e9),
        memory_peak_bytes=max(before.memory_peak_bytes, held.get("max_usage", 0), held.get("usage", 0)))


def measured(samples: Iterable[dict[str, Any]]) -> Measured | None:
    """What a container used, from all its samples. None when there was none."""
    return functools.reduce(with_sample, samples, None)


class Meter:
    """Samples one container from when it appears until `stop()`. Used as `async with Meter(name) as meter`."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.measured: Measured | None = None
        self._reading: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Meter":
        host = os.environ.get("DOCKER_HOST", _DEFAULT_HOST)
        if host.startswith("unix://"):                     # a daemon reached any other way is not measured
            self._reading = asyncio.create_task(self._read(host.removeprefix("unix://")))
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._reading is not None:
            self._reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reading

    async def _read(self, socket: str) -> None:
        with contextlib.suppress(httpx.HTTPError, ValueError):       # a daemon that will not say: not measured
            transport = httpx.AsyncHTTPTransport(uds=socket)
            async with httpx.AsyncClient(transport=transport, timeout=_UNTIL_IT_ENDS) as daemon:
                while True:
                    async with daemon.stream("GET", f"http://docker/containers/{self._name}/stats") as answer:
                        if answer.status_code == 200:
                            async for line in answer.aiter_lines():
                                self.measured = with_sample(self.measured, json.loads(line))
                            return
                    await asyncio.sleep(_FIRST_SAMPLE_S)             # `docker run` has not made it yet
