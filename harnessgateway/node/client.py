"""The node's connection to the control plane: one HTTPS client, its own credential, retries with backoff for the
calls that must land (events, finish, usage)."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .. import __version__

log = logging.getLogger("harnessgateway.node")


class ControlClient:
    def __init__(self, api_url: str, state_file: Path):
        self.api_url, self.state_file = api_url.rstrip("/"), state_file
        self.http = httpx.AsyncClient(base_url=self.api_url, timeout=httpx.Timeout(60, connect=15), trust_env=False)
        self.node_id = ""

    async def join(self, join_token: str, name: str, slots: int) -> str:
        """The saved credential, or a new one from the join token. The control plane may still be starting."""
        if self.state_file.is_file():
            saved = json.loads(self.state_file.read_text())
            self.http.headers["Authorization"] = f"Bearer {saved['credential']}"
            self.node_id = saved["id"]
            return self.node_id
        if not join_token:
            raise SystemExit("configuration error: no saved node credential and no HG_NODE_JOIN_TOKEN")
        for attempt in range(60):
            try:
                r = await self.http.post("/internal/v1/nodes/join", json={"name": name, "slots": slots, "version": __version__},
                                         headers={"Authorization": f"Bearer {join_token}"})
                if r.status_code == 403:
                    raise SystemExit(f"the control plane refused the join token: {r.text[:200]}")
                r.raise_for_status()
                break
            except httpx.HTTPError as e:
                if attempt == 59:
                    raise SystemExit(f"could not reach the control plane at {self.api_url}: {e}") from None
                await asyncio.sleep(2)
        doc = r.json()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"id": doc["id"], "credential": doc["credential"]}))
        self.state_file.chmod(0o600)
        self.http.headers["Authorization"] = f"Bearer {doc['credential']}"
        self.node_id = doc["id"]
        return self.node_id

    async def call(self, method: str, path: str, body: Any = None, *, attempts: int = 1) -> httpx.Response:
        for attempt in range(attempts):
            try:
                r = await self.http.request(method, path, json=body)
                if r.status_code < 500:
                    return r
            except httpx.HTTPError:
                if attempt == attempts - 1:
                    raise
            await asyncio.sleep(min(2 ** attempt, 15))
        return r

    async def heartbeat(self, running: list[str], sandboxes: list[str], slots: int) -> dict[str, Any]:
        r = await self.call("POST", "/internal/v1/nodes/heartbeat",
                            {"running": running, "sandboxes": sandboxes, "slots": slots, "version": __version__})
        r.raise_for_status()
        return r.json()

    async def claim(self) -> dict[str, Any] | None:
        r = await self.call("POST", "/internal/v1/jobs/claim", {})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    async def events(self, rid: str, batch: int, lines: list[str]) -> dict[str, Any]:
        r = await self.call("POST", f"/internal/v1/turns/{rid}/events", {"batch": batch, "lines": lines}, attempts=6)
        return r.json() if r.status_code == 200 else {"stop": None}

    async def finish(self, rid: str, report: dict[str, Any]) -> None:
        r = await self.call("POST", f"/internal/v1/turns/{rid}/finish", report, attempts=8)
        if r.status_code >= 400:
            log.error("finish for %s refused: %s %s", rid, r.status_code, r.text[:300])

    async def usage(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            r = await self.call("POST", "/internal/v1/usage", {"rows": rows}, attempts=5)
            r.raise_for_status()

    async def download(self, url: str, dest: Path) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        async with self.http.stream("GET", url, timeout=httpx.Timeout(600, connect=15)) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 1024):
                    f.write(chunk)
                    size += len(chunk)
        return size

    async def fetch(self, url: str) -> bytes:
        r = await self.http.get(url, timeout=httpx.Timeout(600, connect=15))
        r.raise_for_status()
        return r.content

    async def upload(self, url: str, source: Path | bytes) -> None:
        async def chunks():
            with Path(source).open("rb") as f:          # type: ignore[arg-type]
                while chunk := f.read(1024 * 1024):
                    yield chunk
        content = source if isinstance(source, bytes) else chunks()
        r = await self.http.put(url, content=content, timeout=httpx.Timeout(1800, connect=15))
        r.raise_for_status()
