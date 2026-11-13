"""How a step in a container reaches the platform: the internal API, over HTTP (spec §15.1)."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from harnessgateway.errors import Refused
from harnessgateway.steps.client import InputFile, RestorePlan
from harnessgateway.steps.errors import StepError

TIMEOUT_S = 120


class HttpStepClient:
    def __init__(self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_S,
            transport=transport or httpx.AsyncHTTPTransport(retries=3))

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _refusal(status: int, body: bytes) -> Refused:
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = f"the platform answered {status}"
        return Refused(message)

    async def _call(self, method: str, path: str, **send: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **send)
        except httpx.HTTPError as e:
            raise StepError(f"the platform could not be reached: {e!r}") from e
        if not response.is_success:
            raise self._refusal(response.status_code, response.content)
        return response

    async def restore_plan(self, turn_id: str) -> RestorePlan:
        plan = (await self._call("GET", f"/internal/steps/restore/{turn_id}")).json()
        return RestorePlan(
            manifest_key=plan["manifest_key"], warm=plan["warm"], request=plan["request"], history=plan["history"],
            handover=plan["handover"], files=tuple(InputFile(**file) for file in plan["files"]))

    async def save_plan(self, turn_id: str) -> str | None:
        key: str | None = (await self._call("GET", f"/internal/steps/save/{turn_id}")).json()["manifest_key"]
        return key

    async def record(self, turn_id: str, manifest: str) -> str:
        response = await self._call("PUT", f"/internal/steps/save/{turn_id}/manifest", content=manifest.encode())
        snapshot_id: str = response.json()["snapshot_id"]
        return snapshot_id

    async def unpack_plan(self, install_id: str) -> str:
        key: str = (await self._call("GET", f"/internal/steps/unpack/{install_id}")).json()["archive_key"]
        return key

    async def object(self, key: str) -> AsyncIterator[bytes]:
        try:
            async with self._http.stream("GET", f"/internal/objects/{key}") as response:
                if not response.is_success:
                    raise self._refusal(response.status_code, await response.aread())
                async for piece in response.aiter_bytes():
                    yield piece
        except httpx.HTTPError as e:
            raise StepError(f"the platform could not be reached: {e!r}") from e

    async def missing(self, hashes: list[str]) -> list[str]:
        answer: list[str] = (await self._call("POST", "/internal/blobs/missing", json={"hashes": hashes})).json()[
            "missing"]
        return answer

    async def chunk(self, digest: str) -> bytes:
        return (await self._call("GET", f"/internal/blobs/{digest}")).content

    async def put_chunk(self, digest: str, data: bytes) -> None:
        await self._call("PUT", f"/internal/blobs/{digest}", content=data)
