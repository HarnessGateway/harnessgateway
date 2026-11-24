"""`harnessgateway proxy`: the egress proxy as a process of its own. It holds no database address and no sealing
key. What it must know about a running sandbox it asks the platform, over the internal API, and what passed
through it it tells the platform the same way."""

import asyncio
import logging
import signal
import time
from collections import Counter

import httpx

from harnessgateway.domain.egress import Policy
from harnessgateway.proxy.authority import Authority
from harnessgateway.proxy.server import EgressProxy
from harnessgateway.settings import ProxySettings

log = logging.getLogger(__name__)

_KEEP_S = 30.0                                 # how long a policy is used before the platform is asked again
_KEEP_UNKNOWN_S = 2.0                          # how long "no such sandbox" is believed
_TELL_EVERY_S = 2.0
_WAIT_FOR_PLATFORM_S = 300


class PlatformLink:
    """The proxy's side of the internal API."""

    def __init__(self, url: str, proxy_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {proxy_key}"}, timeout=30,
                                       transport=transport or httpx.AsyncHTTPTransport(retries=3))
        self._policies: dict[tuple[str, str], tuple[float, Policy | None]] = {}
        self._sent: Counter[str] = Counter()       # by organization: bytes not yet told to the platform
        self._received: Counter[str] = Counter()

    async def authority(self) -> tuple[str, str]:
        answer = (await self._http.get("/internal/authority")).raise_for_status().json()
        return answer["certificate"], answer["key"]

    async def policy(self, subject_id: str, token: str) -> Policy | None:
        """The policy of the running sandbox with this id and token; None when there is none."""
        kept = self._policies.get((subject_id, token))
        if kept and kept[0] > time.monotonic():
            return kept[1]
        answer = await self._http.post("/internal/policy", json={"id": subject_id, "token": token})
        policy = None if answer.status_code == 404 else Policy.from_json(answer.raise_for_status().json())
        if len(self._policies) > 10_000:           # ended sandboxes' entries are never asked for again
            self._policies.clear()
        self._policies[(subject_id, token)] = (time.monotonic() + (_KEEP_S if policy else _KEEP_UNKNOWN_S), policy)
        return policy

    def count(self, org_id: str, sent: int, received: int) -> None:
        self._sent[org_id] += sent
        self._received[org_id] += received

    async def tell(self) -> None:
        """Tell the platform the bytes that passed. What cannot be told is kept for the next time."""
        sent, received, self._sent, self._received = self._sent, self._received, Counter(), Counter()
        passed = [{"org_id": org_id, "sent": sent[org_id], "received": received[org_id]}
                  for org_id in sent.keys() | received.keys() if sent[org_id] or received[org_id]]
        try:
            if passed:
                (await self._http.post("/internal/passed", json=passed)).raise_for_status()
        except httpx.HTTPError as error:
            log.warning("the bytes that passed could not be told, and are kept: %r", error)
            self._sent += sent
            self._received += received

    async def close(self) -> None:
        await self.tell()
        await self._http.aclose()


async def run_proxy(settings: ProxySettings) -> None:
    link = PlatformLink(settings.internal_url, settings.proxy_key)
    async with asyncio.timeout(_WAIT_FOR_PLATFORM_S):          # the proxy may start before the platform is up
        while True:
            try:
                certificate, key = await link.authority()
                break
            except httpx.HTTPError as error:
                log.info("waiting for the platform: %r", error)
                await asyncio.sleep(2)
    proxy = EgressProxy(link.policy, link.count, Authority(certificate, key), private_hosts=settings.private_hosts)
    server = await proxy.start("0.0.0.0", settings.port)        # noqa: S104 - reached from the sandbox network

    async def telling() -> None:
        while True:
            await asyncio.sleep(_TELL_EVERY_S)
            await link.tell()
    teller, stop = asyncio.create_task(telling()), asyncio.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(name, stop.set)
    log.info("the egress proxy listens on port %d", settings.port)
    await stop.wait()
    server.close()
    teller.cancel()
    await asyncio.gather(teller, return_exceptions=True)
    await link.close()
