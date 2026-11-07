"""The worker (SPEC §7.5, §7.6): jobs from the queue and duties on a clock. Any number run; each job is acknowledged
only when done and each duty runs under a Postgres advisory lock, so replicas never do the same thing twice. The
worker is the one process that touches the launcher's substrate."""
from __future__ import annotations

import asyncio
import logging
import socket
import time
import zlib
from collections.abc import Awaitable, Callable
from typing import Any

from ..adapters.docker_cli import DockerError
from ..ports.launch import BoxSpec, Launcher
from ..services.platform import Platform

TICK_S = 5
UNSTORED_KEPT_MIN = 10
log = logging.getLogger("harnessgateway.worker")


class Worker:
    def __init__(self, plat: Platform, launcher: Launcher | None = None):
        self.plat = plat
        self.launcher = launcher or plat.extensions.launcher(plat.settings) or self._docker()
        self.name = f"{socket.gethostname()}-{id(self) & 0xffff:x}"
        self.duties: list[tuple[str, float, Callable[[], Awaitable[object]]]] = [
            ("lost", 15, plat.boxes.lost), ("reconcile", 30, self.reconcile), ("retention", 300, self.retention),
            ("purge", 300, self.purge), ("gc", 600, plat.snapshots.collect), ("accrue", 3600, plat.usage.accrue_storage),
            ("reap", 60, self.launcher.reap)]

    def _docker(self) -> Launcher:
        from ..adapters.launch_docker import DockerLauncher
        return DockerLauncher(self.plat.settings)

    async def run(self) -> None:
        await self.plat.open()
        prepare = getattr(self.launcher, "prepare", None)
        if prepare:
            await prepare()
        log.info("worker %s: launcher %s", self.name, type(self.launcher).__name__)
        try:
            await asyncio.gather(self.jobs(), self.clock())
        finally:
            await self.plat.close()

    # ── jobs ────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def jobs(self) -> None:
        while True:
            try:
                batch = await self.plat.moments.consume(self.name)
            except Exception:
                log.exception("the queue could not be read; trying again")
                await asyncio.sleep(TICK_S)
                continue
            for job_id, kind, payload in batch:
                try:
                    await self.job(kind, payload)
                except Exception:
                    log.exception("job %s %s failed; it stays queued", kind, payload)
                    continue
                await self.plat.moments.ack(job_id)

    async def job(self, kind: str, payload: dict[str, Any]) -> None:
        plat = self.plat
        if kind == "launch":
            await self.launch(payload["box_id"])
        elif kind == "install":
            box_id = await plat.boxes.ensure_install_box(payload["org_id"], payload["install_id"])
            if box_id:
                await self.launch(box_id)
        elif kind == "stop":
            reference = await plat.boxes.to_stop(payload["box_id"])
            if reference:
                await self.launcher.stop(reference)
        elif kind == "delete_session":
            await plat.boxes.end_for_session(payload["session_id"])
            await plat.containers.purge(payload["org_id"], payload["session_id"])
        else:
            log.warning("unknown job kind %s dropped", kind)

    async def launch(self, box_id: str) -> None:
        box = await self.plat.boxes.to_launch(box_id)
        if box is None:
            return
        limits = box["limits"]
        spec = BoxSpec(box_id, box["org_id"], box["token"], limits.vcpus, limits.memory_mb, limits.tmp_mb, limits.pids,
                       box["kind"])
        try:
            reference = await self.launcher.start(spec)
        except (DockerError, OSError) as e:
            log.error("box %s could not start: %s", box_id, e)
            await self.plat.boxes.launch_failed(box_id, str(e)[:500])
            return
        await self.plat.boxes.launched(box_id, reference)
        log.info("box %s (%s) started as %s", box_id, box["kind"], reference)

    # ── duties ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def clock(self) -> None:
        last = {name: 0.0 for name, _, _ in self.duties}
        while True:
            for name, every, duty in self.duties:
                if time.monotonic() - last[name] < every:
                    continue
                last[name] = time.monotonic()
                try:
                    async with self.plat.db.platform() as tx:
                        key = 7_000_000 + zlib.crc32(name.encode()) % 1_000_000
                        if await tx.value("SELECT pg_try_advisory_xact_lock(%s)", key):
                            await duty()
                except Exception:
                    log.exception("duty %s failed; next tick", name)
            await asyncio.sleep(TICK_S)

    async def reconcile(self) -> int:
        """Boxes that never got a launch and installs with no box: queued again."""
        plat, n = self.plat, 0
        for box in await plat.boxes.stale_launches():
            await plat.moments.enqueue("launch", box_id=box["id"])
            n += 1
        for install in await plat.installs.pending_without_box():
            await plat.moments.enqueue("install", install_id=install["id"], org_id=install["org_id"])
            n += 1
        for org_id in await self._orgs_with_waiting_turns():
            await plat.boxes.admit_pending(org_id)
        return n

    async def _orgs_with_waiting_turns(self) -> list[str]:
        async with self.plat.db.platform() as tx:
            return [r["org_id"] for r in await tx.all(
                "SELECT DISTINCT t.org_id FROM turns t WHERE t.status = 'queued' AND t.created_at < now() - interval '1 minute' "
                "AND NOT EXISTS (SELECT 1 FROM boxes b WHERE b.session_id = t.session_id AND b.status <> 'gone')")]

    async def retention(self) -> int:
        """Sessions idle past their organization's retention: deleted, with their snapshots."""
        plat = self.plat
        async with plat.db.platform() as tx:
            orgs = await tx.all("SELECT id, limits FROM orgs")
            rows: list[dict[str, Any]] = []
            for org in orgs:
                days = plat.orgs.limits_of(org).retention_days
                if days:
                    rows += await tx.all(
                        "UPDATE sessions SET status = 'deleted' WHERE org_id = %s AND status = 'active' AND last_active_at < "
                        "now() - %s * interval '1 day' AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id = sessions.id "
                        "AND t.status IN ('queued', 'in_progress')) RETURNING id, org_id", org["id"], days)
        for r in rows:
            await plat.moments.enqueue("delete_session", session_id=r["id"], org_id=r["org_id"])
        return len(rows)

    async def purge(self) -> int:
        """Unstored sessions idle a while, spent idempotency keys and sign-ins, old deleted-object rows."""
        plat = self.plat
        async with plat.db.platform() as tx:
            rows = await tx.all(
                "UPDATE sessions SET status = 'deleted' WHERE store = false AND status = 'active' AND last_active_at < now() - "
                "%s * interval '1 minute' AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id = sessions.id AND "
                "t.status IN ('queued', 'in_progress')) RETURNING id, org_id", UNSTORED_KEPT_MIN)
            await tx.run("DELETE FROM idempotency WHERE created_at < now() - interval '1 day'")
            await tx.run("DELETE FROM sign_ins WHERE expires_at < now() - interval '1 day'")
            await tx.run("DELETE FROM objects WHERE deleted_at < now() - interval '1 day'")
        for r in rows:
            await plat.moments.enqueue("delete_session", session_id=r["id"], org_id=r["org_id"])
        return len(rows)
