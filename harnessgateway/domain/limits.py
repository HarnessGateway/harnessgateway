"""An organization's limits (SPEC §7.4): what each of its boxes gets and how many turns may run. Self-hosted takes the
defaults from configuration; an extension sets them per organization (a plan is a limits document)."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class OrgLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vcpus: Annotated[float, Field(gt=0, le=64)] = 2.0
    memory_mb: Annotated[int, Field(ge=256, le=262_144)] = 4096
    tmp_mb: Annotated[int, Field(ge=64, le=262_144)] = 4096
    pids: Annotated[int, Field(ge=64, le=65_536)] = 1024
    max_concurrent_turns: Annotated[int, Field(ge=0)] = 0      # 0: no ceiling
    standby_s: Annotated[int, Field(ge=0, le=86_400)] = 120
    turn_timeout_s: Annotated[int, Field(ge=0)] = 0            # 0: none
    retention_days: Annotated[int, Field(ge=0)] = 0            # 0: sessions are kept until deleted
    observability: Literal["metadata", "full"] = "metadata"

    @classmethod
    def from_env(cls, values: dict[str, str]) -> OrgLimits:
        unknown = sorted(set(values) - set(cls.model_fields))
        if unknown:
            raise ValueError(f"unknown HG_DEFAULT_ settings: {', '.join(k.upper() for k in unknown)}")
        return cls.parse(values)

    @classmethod
    def parse(cls, value: dict[str, Any], base: OrgLimits | None = None) -> OrgLimits:
        """Limits from stored or given fields over `base` (the configured defaults)."""
        try:
            return cls.model_validate({**(base.model_dump() if base else {}), **value})
        except ValidationError as e:
            err = e.errors()[0]
            raise ValueError(f"limits.{'.'.join(map(str, err['loc']))}: {err['msg']}") from None
