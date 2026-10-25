"""A turn's states and the only moves between them. A turn is one record that is both the response a client reads and
the job a node runs. Any other move is a bug, and the store refuses it by compare-and-set on the current state."""
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
        return self in FINAL


FINAL = frozenset({Status.COMPLETED, Status.INCOMPLETE, Status.FAILED, Status.CANCELLED})
MOVES: dict[Status, frozenset[Status]] = {
    Status.QUEUED: frozenset({Status.IN_PROGRESS, Status.CANCELLED, Status.FAILED}),
    Status.IN_PROGRESS: FINAL,
}


def allowed(src: Status, dst: Status) -> bool:
    return dst in MOVES.get(src, frozenset())


def sources(dst: Status) -> tuple[str, ...]:
    """The states a move into `dst` may start from, for the store's compare-and-set."""
    return tuple(s.value for s, targets in MOVES.items() if dst in targets)


class Reason(StrEnum):
    """Why the platform, not the package, decided a turn's end. Reported as metadata.hg_reason."""
    CANCELLED = "cancelled"
    CLIENT_DISCONNECTED = "client_disconnected"
    TIMEOUT = "timeout"
    NO_TERMINAL_EVENT = "no_terminal_event"
    INVALID_OUTPUT = "invalid_output"
    OUTPUT_TOO_LARGE = "output_too_large"
    SANDBOX_FAILED = "sandbox_failed"
    INSTALL_FAILED = "install_failed"
    NODE_LOST = "node_lost"
    WORKSPACE_TOO_LARGE = "workspace_too_large"
