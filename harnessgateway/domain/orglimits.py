"""The ceilings an organization runs under. The operator sets them per organization; an organization never sets its
own. A package asks for a turn timeout and gets at most `turn_timeout_s`."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import Invalid


class OrgLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vcpus: Annotated[float, Field(gt=0, le=32)] = 2              # per sandbox
    memory_mb: Annotated[int, Field(ge=256, le=131_072)] = 4096  # per sandbox
    pids: Annotated[int, Field(ge=64, le=32_768)] = 1024         # per sandbox
    max_sandboxes: Annotated[int, Field(ge=1, le=10_000)] = 10   # warm or running, across the organization
    max_running_turns: Annotated[int, Field(ge=1, le=10_000)] = 10
    turn_timeout_s: Annotated[int, Field(ge=10, le=86_400)] = 3600
    idle_ttl_s: Annotated[int, Field(ge=0, le=86_400)] = 300     # a warm sandbox waits this long for the next turn
    session_retention_days: Annotated[int, Field(ge=1, le=3650)] = 30
    workspace_mb: Annotated[int, Field(ge=16, le=1_048_576)] = 4096
    storage_gb: Annotated[float, Field(gt=0, le=100_000)] = 50   # every object the organization keeps
    requests_per_minute: Annotated[int, Field(ge=1, le=100_000)] = 600
    monthly_charge_usd: Annotated[float, Field(ge=0)] = 0        # 0: no cap
    observability: bool = False                                  # capture harness and provider exchanges
    dedicated_nodes: bool = False                                # run only on nodes joined for this organization

    @classmethod
    def parse(cls, given: dict[str, Any] | None) -> OrgLimits:
        try:
            return cls.model_validate(given or {})
        except ValidationError as e:
            err = e.errors()[0]
            raise Invalid(f"limits.{'.'.join(map(str, err['loc']))}: {err['msg']}", param="limits") from None

    def merged(self, patch: dict[str, Any]) -> OrgLimits:
        return OrgLimits.parse({**self.model_dump(), **patch})
