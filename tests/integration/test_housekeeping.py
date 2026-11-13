"""Spec §12.5, §12.8, §18.5: what housekeeping removes."""

import asyncio
import contextlib
from pathlib import Path

import pytest

from harnessgateway import packaging
from harnessgateway.api.internal import internal_app
from harnessgateway.domain.plan import mind_volume, work_volume
from harnessgateway.platform import Platform as RunningPlatform
from harnessgateway.runner.housekeeping import Housekeeping
from harnessgateway.runner.runner import Runner
from harnessgateway.store.blobs import BlobMissing
from harnessgateway.stores import Stores
from tests.helpers import SHELL, Platform, text_of, variant
from tests.integration.local_driver import LocalDriver


async def test_idle_volumes_unstored_turns_and_unused_chunks_are_removed(
    stores: Stores, org: str, tmp_path: Path
) -> None:
    async with stores.db.system() as conn:
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")
    await stores.harnesses.add(org, packaging.pack(SHELL))
    driver = LocalDriver(tmp_path / "volumes", internal_app(stores))
    platform = Platform(stores, org, driver)
    runner = asyncio.create_task(Runner(stores, driver, heartbeat_s=0.2).run())
    try:
        kept = await platform.run("echo kept > a.txt")
        unstored = await platform.run("echo gone", store=False)
        housekeeping = Housekeeping(RunningPlatform.of(stores, driver))

        await housekeeping.once()                                # nothing is old enough yet
        assert driver.volume(work_volume(kept.session_id)).exists()
        assert (await stores.turns.get(org, unstored.id)).status == "completed"

        async with stores.db.system() as conn:
            await conn.execute("UPDATE sessions SET volume_used_at = now() - interval '2 hours' WHERE org_id = %s",
                               [org])
            await conn.execute("UPDATE turns SET completed_at = now() - interval '1 hour' WHERE id = %s",
                               [unstored.id])
        unused = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"       # the hash of b"test"
        await stores.snapshots.put_chunk(org, unused, b"test")
        async with stores.db.org(org) as conn:
            await conn.execute("UPDATE blobs SET unreferenced_at = now() - interval '2 hours' WHERE refs = 0")
        await housekeeping.once()

        assert not driver.volume(work_volume(kept.session_id)).exists()
        assert not driver.volume(mind_volume(kept.session_id)).exists()
        assert await stores.turns.unstored(final_for_s=0) == []
        assert await stores.snapshots.missing(org, [unused]) == [unused]

        after = await platform.run("cat a.txt", previous_response_id=kept.id)      # restored from the snapshot
        assert text_of(after) == "kept\n"
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


async def test_sessions_past_retention_and_packages_nobody_uses_are_removed(
    stores: Stores, org: str, tmp_path: Path
) -> None:
    async with stores.db.system() as conn:
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")
    first_version = await stores.harnesses.add(org, packaging.pack(SHELL))
    driver = LocalDriver(tmp_path / "volumes", internal_app(stores))
    running = RunningPlatform.of(stores, driver)
    platform = Platform(stores, org, driver)
    runner = asyncio.create_task(Runner(stores, driver, installer=running.installer, heartbeat_s=0.2).run())
    try:
        old = await platform.run("echo old > a.txt")
        recent = await platform.run("echo recent > b.txt")
        housekeeping = Housekeeping(running)

        await stores.orgs.set_retention(org, 7)
        async with stores.db.system() as conn:
            await conn.execute("UPDATE sessions SET last_active_at = now() - interval '8 days' WHERE id = %s",
                               [old.session_id])
        await housekeeping.once()
        assert [session.id for session in await stores.turns.sessions(org)] == [recent.session_id]
        assert not driver.volume(work_volume(old.session_id)).exists()

        [install] = await stores.harnesses.installs(org, first_version.digest)
        replaced = await stores.harnesses.add(org, variant(tmp_path, "shell", version="2.0.0"))
        assert replaced.digest != first_version.digest
        await housekeeping.once()
        assert await stores.harnesses.installs(org, first_version.digest) == []
        assert not driver.volume(f"hg-harness-{install.id}").exists()
        with pytest.raises(BlobMissing):
            await stores.blobs.get(f"orgs/{org}/packages/{first_version.digest}")
        after = await platform.run("echo still works")
        assert after.status == "completed"
        assert after.harness_version == "2.0.0"
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
