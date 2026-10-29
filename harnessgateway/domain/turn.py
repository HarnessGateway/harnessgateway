"""A turn's states and the reasons the platform (not the package) ends one (SPEC §4.4)."""
from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def final(self) -> bool:
        return self not in (Status.QUEUED, Status.IN_PROGRESS)


class Reason(StrEnum):
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INSTALL_FAILED = "install_failed"
    SANDBOX_FAILED = "sandbox_failed"
    INVALID_OUTPUT = "invalid_output"
    NODE_LOST = "node_lost"
    EXITED_WITHOUT_RESULT = "exited_without_result"
