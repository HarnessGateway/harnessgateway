"""What is metered (SPEC §12): infrastructure, measured where it happens."""
from __future__ import annotations

from enum import StrEnum


class Meter(StrEnum):
    AGENT_SECONDS = "agent_seconds"            # a box's time: the customer's unit when the kind is `turn`
    CPU_SECONDS = "cpu_seconds"                # CPU time the box used (its cgroup)
    MEMORY_GIB_SECONDS = "memory_gib_seconds"  # memory allocated to the box over time
    EGRESS_BYTES = "egress_bytes"
    INGRESS_BYTES = "ingress_bytes"
    STORAGE_GIB_HOURS = "storage_gib_hours"


class Kind(StrEnum):
    TURN = "turn"            # a box while a turn runs in it
    STANDBY = "standby"      # a box waiting for its session's next turn
    INSTALL = "install"      # an install box
    STORAGE = "storage"      # bytes at rest
