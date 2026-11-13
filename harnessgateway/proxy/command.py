"""`harnessgateway proxy`: the egress proxy as a process (spec §11, §18.1)."""

import asyncio
import logging
import signal
import tempfile
from pathlib import Path

import httpx

from harnessgateway.proxy.authority import Authority
from harnessgateway.proxy.platform import PlatformClient
from harnessgateway.proxy.server import EgressProxy
from harnessgateway.settings import ProxySettings

log = logging.getLogger(__name__)

_WAIT_FOR_PLATFORM_S = 300


async def _authority(platform: PlatformClient) -> tuple[str, str]:
    """The platform's authority. The proxy may start before the platform is up: it waits."""
    async with asyncio.timeout(_WAIT_FOR_PLATFORM_S):
        while True:
            try:
                return await platform.authority()
            except httpx.HTTPError as e:
                log.info("waiting for the platform: %r", e)
                await asyncio.sleep(2)


async def run_proxy(settings: ProxySettings) -> None:
    platform = PlatformClient(settings.internal_url, settings.proxy_key)
    certificate, key = await _authority(platform)
    with tempfile.TemporaryDirectory(prefix="hg-proxy-") as folder:
        proxy = EgressProxy(platform, Authority(certificate, key, Path(folder)),
                            private_hosts=settings.private_hosts)
        server = await proxy.start("0.0.0.0", settings.port)  # noqa: S104 - reached from the sandbox network
        flushing = asyncio.create_task(platform.flushing())
        stop = asyncio.Event()
        for name in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(name, stop.set)
        log.info("the egress proxy listens on port %d", settings.port)
        await stop.wait()
        server.close()
        flushing.cancel()
        await asyncio.gather(flushing, return_exceptions=True)
        await platform.close()
