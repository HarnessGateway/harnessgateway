"""The master key held by the platform itself: HG_MASTER_KEY (64 hex digits) or HG_MASTER_KEY_FILE."""
from __future__ import annotations

from typing import Mapping

from ...kernel.vault import Vault


def open_local(env: Mapping[str, str]) -> Vault:
    return Vault.from_env(env)
