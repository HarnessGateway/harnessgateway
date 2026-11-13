"""Spec §12.5, §12.8, §18.5: what housekeeping removes."""

import asyncio
import contextlib
from pathlib import Path

from harnessgateway import packaging
from harnessgateway.api.internal import internal_app
from harnessgateway.domain.plan import mind_volume, work_volume
from harnessgateway.runner.housekeeping import Housekeeping
from harnessgateway.runner.runner import Runner
from harnessgateway.stores import Stores
from tests.helpers import SHELL, Platform, text_of
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
        housekeeping = Housekeeping(stores, driver)

        await housekeeping.once()                                # nothing is old enough yet
        assert driver.volume(work_volume(kept.session_id)).exists()
        assert (await stores.turns.get(org, unstored.id)).status == "completed"

        async with stores.db.system() as conn:
            await conn.execute("UPDATE sessions SET volume_used_at = now() - interval '2 hours' WHERE org_id = %s",
                               [org])
            await conn.execute("UPDATE turns SET completed_at = now() - interval '1 hour' WHERE id = %s",
                               [unstored.id])
        await stores.snapshots.put_chunk(org, "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", b"test")
        async with stores.db.org(org) as conn:
            await conn.execute("UPDATE blobs SET unreferenced_at = now() - interval '2 hours' WHERE refs = 0")
        await housekeeping.once()

        assert not driver.volume(work_volume(kept.session_id)).exists()
        assert not driver.volume(mind_volume(kept.session_id)).exists()
        assert await stores.turns.unstored(final_for_s=0) == []
        assert await stores.snapshots.missing(
            org, ["9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"]) != []

        after = await platform.run("cat a.txt", previous_response_id=kept.id)      # restored from the snapshot
        assert text_of(after) == "kept\n"
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
