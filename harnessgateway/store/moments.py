"""Redis: moments, not records (SPEC §7.2). The live event stream of each turn, the job queue the workers consume, the
wake-up of a box waiting for its session's next turn, and an SSO exchange in flight. A lost Redis costs a delay: the
records in Postgres let every process rebuild what it needs."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ResponseError

JOBS = "hg:jobs"
GROUP = "workers"
TURN_STREAM_MAX = 20_000
TURN_STREAM_TTL_S = 600
JOB_RECLAIM_MS = 5 * 60 * 1000
SSO_TTL_S = 600
READ_TIMEOUT_S = 60              # longer than any blocking read (consume blocks 5 s, follow 15 s): a dead Redis is an
                                 # error, not a hang. redis-py 6+ bounds every read by 5 s unless told otherwise.


def turn_stream(rid: str) -> str:
    return f"hg:turn:{rid}"


def wake_channel(sid: str) -> str:
    return f"hg:wake:{sid}"


class Moments:
    def __init__(self, url: str):
        self.url = url
        self.r: redis.Redis = redis.Redis.from_url(url, decode_responses=True,  # type: ignore[type-arg]
                                                   socket_timeout=READ_TIMEOUT_S)

    async def close(self) -> None:
        await self.r.aclose()  # type: ignore[attr-defined]

    async def ping(self) -> None:
        await self.r.ping()

    # ── the live event stream of a turn ───────────────────────────────────────────────────────────────────────────
    async def publish(self, rid: str, events: list[dict[str, Any]]) -> None:
        """Events of a turn, each carrying `sequence_number`, onto its stream."""
        if not events:
            return
        async with self.r.pipeline(transaction=False) as p:
            for ev in events:
                p.xadd(turn_stream(rid), {"seq": ev["sequence_number"], "data": json.dumps(ev, ensure_ascii=False)},
                       maxlen=TURN_STREAM_MAX, approximate=True)
            await p.execute()

    async def end_stream(self, rid: str) -> None:
        """The terminal event is on the stream: keep it a while for late followers, then let it go."""
        await self.r.expire(turn_stream(rid), TURN_STREAM_TTL_S)

    async def has_stream(self, rid: str) -> bool:
        return bool(await self.r.exists(turn_stream(rid)))

    async def follow(self, rid: str, after: int, *, terminal: frozenset[str], keepalive_s: float
                     ) -> AsyncIterator[dict[str, Any] | None]:
        """The stream's events numbered above `after`, live until a terminal event; None is a keep-alive. Ends when
        the stream is gone (expired or never there)."""
        assert keepalive_s < READ_TIMEOUT_S
        last = "0-0"
        key = turn_stream(rid)
        while True:
            batch = await self.r.xread({key: last}, count=1000, block=int(keepalive_s * 1000))
            if not batch:
                if not await self.r.exists(key):
                    return
                yield None
                continue
            for _, entries in batch:
                for entry_id, fields in entries:
                    last = entry_id
                    if int(fields["seq"]) <= after:
                        continue
                    event = json.loads(fields["data"])
                    yield event
                    if event["type"] in terminal:
                        return

    # ── the job queue ─────────────────────────────────────────────────────────────────────────────────────────────
    async def ensure_group(self) -> None:
        try:
            await self.r.xgroup_create(JOBS, GROUP, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def enqueue(self, kind: str, **payload: Any) -> str:
        return str(await self.r.xadd(JOBS, {"kind": kind, "payload": json.dumps(payload), "at": str(time.time())}))

    async def consume(self, consumer: str, block_ms: int = 5000) -> list[tuple[str, str, dict[str, Any]]]:
        """Jobs for this consumer: stale pending ones reclaimed first, then new ones. Each is (id, kind, payload)."""
        assert block_ms < READ_TIMEOUT_S * 1000
        out: list[tuple[str, str, dict[str, Any]]] = []
        _, claimed, _ = await self.r.xautoclaim(JOBS, GROUP, consumer, min_idle_time=JOB_RECLAIM_MS, start_id="0-0",
                                                count=10)
        out.extend(self._jobs(claimed))
        if not out:
            fresh = await self.r.xreadgroup(GROUP, consumer, {JOBS: ">"}, count=10, block=block_ms)
            for _, entries in fresh or []:
                out.extend(self._jobs(entries))
        return out

    @staticmethod
    def _jobs(entries: Any) -> list[tuple[str, str, dict[str, Any]]]:
        return [(entry_id, fields["kind"], json.loads(fields["payload"])) for entry_id, fields in entries if fields]

    async def ack(self, job_id: str) -> None:
        await self.r.xack(JOBS, GROUP, job_id)
        await self.r.xdel(JOBS, job_id)

    async def queue_depth(self) -> int:
        return int(await self.r.xlen(JOBS))

    # ── waking a box for its session's next turn ──────────────────────────────────────────────────────────────────
    async def wake(self, sid: str) -> None:
        await self.r.publish(wake_channel(sid), "1")

    async def wait_wake(self, sid: str, timeout_s: float) -> bool:
        """True when the session was woken within the timeout."""
        async with self.r.pubsub() as ps:
            await ps.subscribe(wake_channel(sid))
            deadline = time.monotonic() + timeout_s
            while (left := deadline - time.monotonic()) > 0:
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=min(left, 1.0))
                if msg is not None:
                    return True
            return False

    # ── an SSO exchange in flight ─────────────────────────────────────────────────────────────────────────────────
    async def sso_put(self, state: str, doc: dict[str, Any]) -> None:
        await self.r.set(f"hg:sso:{state}", json.dumps(doc), ex=SSO_TTL_S)

    async def sso_take(self, state: str) -> dict[str, Any] | None:
        raw = await self.r.getdel(f"hg:sso:{state}")
        return json.loads(raw) if raw else None


async def wait_for(moments: Moments, attempts: int = 30) -> None:
    """Redis reachable, or SystemExit after `attempts` seconds."""
    for i in range(attempts):
        try:
            await moments.ping()
            return
        except Exception as e:
            if i == attempts - 1:
                raise SystemExit(f"could not reach Redis at {moments.url}: {e}") from None
            await asyncio.sleep(1)
