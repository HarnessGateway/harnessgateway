"""What is metered (SPEC §12): infrastructure, measured where it happens."""
from __future__ import annotations

from enum import StrEnum


class Meter(StrEnum):
    VCPU_SECONDS = "vcpu_seconds"
    MEMORY_GIB_SECONDS = "memory_gib_seconds"
    EGRESS_BYTES = "egress_bytes"
    INGRESS_BYTES = "ingress_bytes"
    STORAGE_GIB_HOURS = "storage_gib_hours"


class Kind(StrEnum):
    TURN = "turn"            # a box while a turn runs in it
    WARM = "warm"            # a box kept warm between turns
    INSTALL = "install"      # an install box
    STORAGE = "storage"      # objects at rest
