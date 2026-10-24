"""Layer 0: the ceilings an organization runs under. Set by the deployer on the organization, never by
the organization; a package asks for a timeout and gets at most this."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from ..errors import Invalid


@dataclass(frozen=True)
class Limits:
    cpus: float = 2                                  # per run
    memory: str = "4g"
    pids: int = 512
    run_timeout_s: float = 3600                      # the most a package may ask for a turn
    install_timeout_s: float = 1200                  # and for its install
    max_runs: int = 4                                # turns at once, across the organization
    disk_mb: int = 2048                              # a session's workspace and home, snapshotted
    output_mb: int = 16                              # of a turn's output kept in its record
    upload_mb: int = 64                              # a package or a request's files
    requests_per_minute: int = 120                   # turns created per minute, across the organization

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Limits:
        given = {**asdict(cls()), **(d or {})}
        if unknown := sorted(set(given) - set(asdict(cls()))):
            raise Invalid(f"limits: unknown keys {', '.join(unknown)}")
        for key in ("cpus", "pids", "run_timeout_s", "install_timeout_s", "max_runs", "disk_mb", "output_mb", "upload_mb", "requests_per_minute"):
            v = given[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise Invalid(f"limits: {key} is a positive number")
        if not isinstance(given["memory"], str) or not re.fullmatch(r"[0-9]+[kmg]?", given["memory"]):
            raise Invalid("limits: memory is a Docker size like 4g")
        return cls(**given)
