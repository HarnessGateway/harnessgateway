"""What steps do to folders that are mounted volumes: empty them, and hand them to the sandbox's user."""

import os
import shutil
from pathlib import Path

from harnessgateway.domain.contract import SANDBOX_UID


def give(path: Path) -> None:
    """Hand a path to the sandbox's user. Only root can; a step in a container always is."""
    if os.geteuid() == 0:
        os.lchown(path, SANDBOX_UID, SANDBOX_UID)


def give_tree(folder: Path) -> None:
    give(folder)
    for parent, folders, files in os.walk(folder):
        for name in (*folders, *files):
            give(Path(parent) / name)


def empty(folder: Path) -> None:
    """Remove everything in a folder but not the folder, which is a mount point."""
    for entry in os.scandir(folder):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)
