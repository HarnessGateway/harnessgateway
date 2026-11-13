"""The watch step: notice when the session's folders pass the disk limit (spec §12.6)."""

import time
from collections.abc import Mapping
from pathlib import Path

from harnessgateway.steps.tree import measure

OVER_LIMIT = 3                                 # the step's exit code when the limit is passed
INTERVAL_S = 2.0


def watch(roots: Mapping[str, Path], *, limit_bytes: int, interval_s: float = INTERVAL_S) -> int:
    """Measure until the folders hold more than the limit. Returns what they held then."""
    while (size := measure(roots)) <= limit_bytes:
        time.sleep(interval_s)
    return size
