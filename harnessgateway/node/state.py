"""A session's state (SPEC §6.4): what survives from one sandbox to the next, and nothing else.

Persistence is include-only, the way egress is allow-only: /workspace and $HG_STATE always, and under $HOME only the
paths the package's manifest names in `persist`. Everything else in a sandbox (its tools, caches, /tmp, the rest of
$HOME) is the sandbox's and goes with it. Nothing is excluded because nothing else is included.

The node lists the included paths (type, mode, size, mtime, path) inside the box, as the box's user. The listing's hash
is the state's fingerprint: when a turn leaves it unchanged, nothing is archived or uploaded. Otherwise the included
paths are archived in the box (a gzipped tar, paths relative to /) and uploaded, with the workspace's file list beside
it for the Containers API."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ports.sandbox import Box

SNAPSHOT_TIMEOUT_S = 3600
WORKSPACE, STATE, HOME = "workspace", "state", "home/hg"
BOX_WORKSPACE, BOX_STATE, BOX_HOME = f"/{WORKSPACE}", f"/{STATE}", f"/{HOME}"

# Every entry under the existing paths among the arguments (relative to /), one per line, in a stable order.
LIST = r"""cd / || exit 2
have=()
for p in "$@"; do [ -e "$p" ] && have+=("$p"); done
[ ${#have[@]} -eq 0 ] && exit 0
find "${have[@]}" -xdev -printf '%y\t%m\t%s\t%T@\t%p\n' 2>/dev/null | LC_ALL=C sort
exit 0
"""

# Seeds: the package's workspace seed for a new session, its home seed for every new box (before any restore, so the
# session's own state wins).
SEED = r"""src=/opt/hg/package/seeds
if [ "$1" = new ] && [ -d "$src/workspace" ]; then cp -R "$src/workspace/." /workspace/; fi
if [ -d "$src/home" ]; then cp -R "$src/home/." /home/hg/; fi
exit 0
"""


def included(persist: list[str]) -> list[str]:
    """The paths a session keeps, relative to the box's root."""
    return [WORKSPACE, STATE, *(f"{HOME}/{p}" for p in persist)]


@dataclass(frozen=True)
class Listing:
    raw: bytes

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    def workspace_files(self) -> list[dict[str, Any]]:
        """The workspace's regular files: {path (workspace-relative), bytes, mtime}."""
        prefix = WORKSPACE + "/"
        out = []
        for line in self.raw.decode("utf-8", "replace").splitlines():
            parts = line.split("\t", 4)
            if len(parts) == 5 and parts[0] == "f" and parts[4].startswith(prefix):
                try:
                    out.append({"path": parts[4][len(prefix):], "bytes": int(parts[2]), "mtime": int(float(parts[3]))})
                except ValueError:
                    continue
        return out


async def listing(box: Box, paths: list[str]) -> Listing:
    return Listing(await box.sh(LIST, *paths, timeout=SNAPSHOT_TIMEOUT_S))


async def seed(box: Box, new_session: bool) -> None:
    await box.sh(SEED, "new" if new_session else "restored", timeout=SNAPSHOT_TIMEOUT_S)


async def archive(box: Box, paths: list[str], dest: Path) -> int:
    """The included paths as one gzipped tar on the host; returns its size."""
    await box.tar_out(paths, dest, timeout=SNAPSHOT_TIMEOUT_S)
    return dest.stat().st_size


async def restore(box: Box, src: Path) -> None:
    await box.tar_in(src, timeout=SNAPSHOT_TIMEOUT_S)
