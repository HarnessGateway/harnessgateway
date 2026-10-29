"""The node's connection to the API: the node's own credential, retries with backoff for the calls that must land, and
signed-link transfers (sent without the credential: a link is its own authorization, and an S3 link refuses a second
one). A 4xx is never retried and never ignored: it raises."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .. import __version__

CHUNK = 1024 * 1024
STARTUP_ATTEMPTS = 60
log = logging.getLogger("harnessgateway.node")


class Refused(RuntimeError):
    """The API answered with 4xx: the call was wrong and retrying it would not help."""


class ApiClient:
    def __init__(self, api_url: str, state_file: Path):
        self.api_url, self.state_file = api_url.rstrip("/"), state_file
        self.http = httpx.AsyncClient(base_url=self.api_url, timeout=httpx.Timeout(60, connect=15), trust_env=False)
        self.links = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15, read=None, write=None), trust_env=False)
        self.node_id = ""

    async def close(self) -> None:
        await self.http.aclose()
        await self.links.aclose()

    # ── joining ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def join(self, join_token: str, name: str, slots: int) -> str:
        """The saved credential while the API knows it, else a new one from the join token."""
        if self.state_file.is_file():
            saved = json.loads(self.state_file.read_text())
            self.http.headers["Authorization"] = f"Bearer {saved['credential']}"
            self.node_id = saved["id"]
            r = await self._reach("POST", "/internal/nodes/heartbeat",
                                  {"running": [], "boxes": [], "saving": [], "slots": slots, "version": __version__})
            if r.status_code != 401:
                r.raise_for_status()
                return self.node_id
            log.warning("the API no longer knows node %s: joining again", self.node_id)
            self.state_file.unlink()
        if not join_token:
            raise SystemExit("configuration error: no valid node credential and no HG_NODE_JOIN_TOKEN")
        r = await self._reach("POST", "/internal/nodes/join", {"name": name, "slots": slots, "version": __version__},
                              headers={"Authorization": f"Bearer {join_token}"})
        if r.status_code in (401, 403):
            raise SystemExit(f"the API refused the join token: {r.text[:200]}")
        r.raise_for_status()
        doc = r.json()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"id": doc["id"], "credential": doc["credential"]}))
        self.state_file.chmod(0o600)
        self.http.headers["Authorization"] = f"Bearer {doc['credential']}"
        self.node_id = doc["id"]
        log.info("joined as %s in pool %r", self.node_id, doc.get("pool", ""))
        return self.node_id

    async def _reach(self, method: str, path: str, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
        for attempt in range(STARTUP_ATTEMPTS):
            try:
                r = await self.http.request(method, path, json=body, headers=headers)
                if r.status_code < 500:
                    return r
            except httpx.HTTPError as e:
                if attempt == STARTUP_ATTEMPTS - 1:
                    raise SystemExit(f"could not reach the API at {self.api_url}: {e}") from None
            await asyncio.sleep(2)
        return r

    # ── calls ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def call(self, method: str, path: str, body: Any = None, attempts: int = 1) -> httpx.Response:
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                r = await self.http.request(method, path, json=body)
            except httpx.HTTPError:
                if last:
                    raise
            else:
                if 400 <= r.status_code < 500:
                    raise Refused(f"{method} {path}: {r.status_code} {r.text[:300]}")
                if r.status_code < 500 or last:
                    r.raise_for_status()
                    return r
            await asyncio.sleep(min(2 ** attempt, 15))
        raise AssertionError("unreachable")

    async def heartbeat(self, running: list[str], boxes: list[str], saving: list[str], slots: int) -> dict[str, Any]:
        return (await self.call("POST", "/internal/nodes/heartbeat", {
            "running": running, "boxes": boxes, "saving": saving, "slots": slots, "version": __version__})).json()

    async def claim(self) -> dict[str, Any] | None:
        r = await self.call("POST", "/internal/nodes/claim", {})
        return None if r.status_code == 204 else r.json()

    async def events(self, rid: str, batch: int, lines: list[str]) -> dict[str, Any]:
        return (await self.call("POST", f"/internal/turns/{rid}/events", {"batch": batch, "lines": lines}, 8)).json()

    async def finish(self, rid: str, report: dict[str, Any]) -> dict[str, Any]:
        return (await self.call("POST", f"/internal/turns/{rid}/finish", report, 10)).json()

    async def saved(self, rid: str, report: dict[str, Any]) -> None:
        await self.call("POST", f"/internal/turns/{rid}/saved", report, 10)

    async def snapshot(self, rid: str) -> dict[str, Any]:
        return (await self.call("GET", f"/internal/turns/{rid}/snapshot", None, 5)).json()

    async def usage(self, records: list[dict[str, Any]]) -> None:
        if records:
            await self.call("POST", "/internal/usage", {"records": records}, 5)

    # ── transfers ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def download(self, link: str, dest: Path) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        async with self.links.stream("GET", link) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(CHUNK):
                    f.write(chunk)
                    size += len(chunk)
        return size

    async def fetch(self, link: str) -> bytes:
        r = await self.links.get(link)
        r.raise_for_status()
        return r.content

    async def upload(self, link: str, source: Path | bytes) -> None:
        async def chunks(path: Path) -> AsyncIterator[bytes]:
            with path.open("rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk
        if isinstance(source, bytes):
            r = await self.links.put(link, content=source)
        else:
            r = await self.links.put(link, content=chunks(source), headers={"Content-Length": str(source.stat().st_size)})
        r.raise_for_status()
