"""The egress proxy's side of the internal API (spec §15.2): its authority, each turn's policy, and its records."""

import asyncio
import logging
import time
from typing import Any

import httpx

from harnessgateway.domain.policy import Policy

log = logging.getLogger(__name__)

KEEP_S = 30.0                                  # how long a policy is used before the platform is asked again
KEEP_UNKNOWN_S = 2.0                           # how long "no such turn" is believed
FLUSH_S = 2.0
RECORDS_MAX = 10_000                           # kept while the platform cannot be reached; older ones are dropped


class PlatformClient:
    def __init__(self, url: str, proxy_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=url, headers={"Authorization": f"Bearer {proxy_key}"}, timeout=30,
            transport=transport or httpx.AsyncHTTPTransport(retries=3))
        self._policies: dict[tuple[str, str], tuple[float, Policy | None]] = {}
        self._records: list[dict[str, Any]] = []

    async def close(self) -> None:
        await self.flush()
        await self._http.aclose()

    async def authority(self) -> tuple[str, str]:
        response = await self._http.get("/internal/proxy/authority")
        response.raise_for_status()
        data = response.json()
        return data["certificate"], data["key"]

    async def policy(self, subject_id: str, token: str) -> Policy | None:
        """The policy of the running turn or install with this id and token; None when there is none."""
        kept = self._policies.get((subject_id, token))
        if kept and kept[0] > time.monotonic():
            return kept[1]
        response = await self._http.post("/internal/proxy/policy", json={"id": subject_id, "token": token})
        if response.status_code == 404:
            policy = None
        else:
            response.raise_for_status()
            policy = Policy.from_json(response.json())
        if len(self._policies) > 10_000:
            self._policies.clear()
        self._policies[(subject_id, token)] = (time.monotonic() + (KEEP_S if policy else KEEP_UNKNOWN_S), policy)
        return policy

    def record(self, policy: Policy, *, host: str, port: int, allowed: bool, reason: str, bytes_up: int,
               bytes_down: int, started_at: float) -> None:
        self._records.append({
            "org_id": policy.org_id, "subject_id": policy.subject_id, "host": host, "port": port, "allowed": allowed,
            "reason": reason, "bytes_up": bytes_up, "bytes_down": bytes_down, "started_at": started_at,
            "ended_at": time.time()})
        del self._records[:-RECORDS_MAX]

    async def flush(self) -> None:
        """Send what has been recorded. What cannot be sent is kept for the next time."""
        if not self._records:
            return
        sending, self._records = self._records, []
        try:
            (await self._http.post("/internal/proxy/records", json={"records": sending})).raise_for_status()
        except httpx.HTTPError as e:
            log.warning("connection records could not be sent, and are kept: %r", e)
            self._records = sending + self._records

    async def flushing(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_S)
            await self.flush()
