"""An organization's capacity (SPEC §9): what each of its sandboxes gets and how many it may hold. Self-hosted takes the
defaults from configuration; a SaaS sets them per organization."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .names import NAME_PATTERN


class Capacity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vcpus: Annotated[float, Field(gt=0, le=64)] = 2.0
    memory_mb: Annotated[int, Field(ge=256, le=262_144)] = 4096
    pids: Annotated[int, Field(ge=64, le=65_536)] = 1024
    tmp_mb: Annotated[int, Field(ge=64, le=262_144)] = 4096
    max_sandboxes: Annotated[int, Field(ge=0)] = 0          # 0: no ceiling
    max_running_turns: Annotated[int, Field(ge=0)] = 0      # 0: no ceiling
    idle_ttl_s: Annotated[int, Field(ge=0, le=86_400)] = 300
    turn_timeout_s: Annotated[int, Field(ge=0)] = 0         # 0: none
    retention_days: Annotated[int, Field(ge=0)] = 0         # 0: sessions are kept until deleted
    node_pool: Annotated[str, Field(pattern=f"^({NAME_PATTERN})?$")] = ""
    observability: Literal["metadata", "full"] = "metadata"

    @classmethod
    def from_env(cls, values: dict[str, str]) -> Capacity:
        known = {k: v for k, v in values.items() if k in cls.model_fields}
        unknown = sorted(set(values) - set(known))
        if unknown:
            raise ValueError(f"unknown HG_DEFAULT_ settings: {', '.join(k.upper() for k in unknown)}")
        return cls.parse(known)

    @classmethod
    def parse(cls, value: dict[str, Any], base: Capacity | None = None) -> Capacity:
        """A capacity from stored or given fields over `base` (the configured defaults)."""
        try:
            return cls.model_validate({**(base.model_dump() if base else {}), **value})
        except ValidationError as e:
            err = e.errors()[0]
            raise ValueError(f"capacity.{'.'.join(map(str, err['loc']))}: {err['msg']}") from None
