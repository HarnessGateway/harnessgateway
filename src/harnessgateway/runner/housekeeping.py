"""Housekeeping, on a timer: what nobody asked for, and nothing would break without, for a while."""

import asyncio
import logging

from harnessgateway.domain.sandbox import install_folder, session_folders
from harnessgateway.platform import Platform

log = logging.getLogger(__name__)

EVERY_S = 60.0
UNSTORED_AFTER_S = 600                         # how long a turn that was not to be stored may outlive its end
GRANT_MAX_S = 25 * 3600                        # longer than any turn or install may take


async def housekeep(platform: Platform, *, elapsed_s: float) -> None:
    """One round: what each organization stores, added to what it used for the `elapsed_s` since the last round;
    then the folders of idle sessions (their snapshots hold everything), turns that were not to be stored, the
    installs of packages no harness is made from, chunks no snapshot names, and grants a dead process left."""
    await platform.usage.add_stored(elapsed_s)
    for session_id in await platform.sessions.idle(for_s=platform.settings.idle_s):
        for folder in session_folders(session_id).values():
            await platform.driver.remove(folder)
    for org_id, turn_id in await platform.turns.unstored(final_for_s=UNSTORED_AFTER_S):
        await platform.forget(org_id, turn_id)
    for install_id in await platform.resources.unused_installs():
        await platform.driver.remove(install_folder(install_id))
    await platform.snapshots.collect()
    await platform.grants.expire(older_than_s=GRANT_MAX_S)


async def housekeeping(platform: Platform) -> None:
    """Do a round whenever one is due. Several processes may run this: each round is done by one of them."""
    while True:
        try:
            elapsed_s = await platform.rounds.take("housekeeping", every_s=EVERY_S)
            if elapsed_s is not None:
                await housekeep(platform, elapsed_s=elapsed_s)
        except Exception:
            log.exception("housekeeping failed; it will try again")
        await asyncio.sleep(EVERY_S / 4)
