"""A step as the driver starts it: `harnessgateway step <kind> <id>`, in a container of the platform's image.

The session's volumes are mounted at their fixed places, and the internal API's address and the step's token
are in the environment.
"""

import sys
from collections.abc import Mapping
from pathlib import Path

from harnessgateway.domain import contract
from harnessgateway.steps.errors import StepError
from harnessgateway.steps.http import HttpStepClient
from harnessgateway.steps.restore import restore
from harnessgateway.steps.save import save
from harnessgateway.steps.unpack import unpack
from harnessgateway.steps.watch import OVER_LIMIT, watch

KINDS = ("restore", "save", "watch", "unpack")
ROOTS = {"mind": Path(contract.MIND), "work": Path(contract.WORK)}
_MIB = 1024 * 1024


async def run_step(kind: str, target: str, *, limit_mb: int, env: Mapping[str, str]) -> int:
    """Run one step to its end. The exit code: 0 done, 1 failed, 3 the watcher saw the disk limit passed."""
    if kind == "watch":
        size = watch(ROOTS, limit_bytes=limit_mb * _MIB)
        print(f"the session's folders hold {size // _MIB} MiB, over the limit of {limit_mb} MiB")
        return OVER_LIMIT
    client = HttpStepClient(env["HG_API"], env["HG_STEP_TOKEN"])
    try:
        if kind == "restore":
            await restore(client, target, ROOTS, Path(contract.TURN))
        elif kind == "save":
            saved = await save(client, target, ROOTS)
            for path in saved.skipped:
                print(f"not saved (a socket, a pipe or a device): {path}")
            print(f"saved as {saved.snapshot_id}: {saved.uploaded} new chunks")
        else:
            await unpack(client, target, Path(contract.HARNESS))
    except StepError as e:
        print(f"{kind} failed: {e}", file=sys.stderr)
        return 1
    finally:
        await client.close()
    return 0
