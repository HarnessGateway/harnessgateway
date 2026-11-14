"""An environment: where turns run (spec §9)."""

import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from harnessgateway.domain.contract import is_reserved
from harnessgateway.domain.definition import Definition
from harnessgateway.domain.hosts import HostEntry
from harnessgateway.domain.names import check_name
from harnessgateway.domain.package import AS_WRITTEN, DECLARED_MAX, check_declared_names, entries

_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
VALUE_MAX = 16_384


class Network(Definition):
    """What a turn's sandbox may reach (spec §9.3). Nothing is reachable unless the mode allows it: `closed` is
    the turn's provider only, `open` is any public host, and `limited` is what its three rules add to the provider."""

    mode: Literal["closed", "limited", "open"] = "limited"
    allow: Annotated[tuple[HostEntry, ...], entries("network.allow"), AS_WRITTEN] = ()
    deny: Annotated[tuple[HostEntry, ...], entries("network.deny"), AS_WRITTEN] = ()
    package_managers: bool = True

    @model_validator(mode="after")
    def _lists_belong_to_limited(self) -> Self:
        for name in ("allow", "deny"):
            if self.mode != "limited" and getattr(self, name):
                raise ValueError(f"network.{name} is a rule of mode limited; mode {self.mode} takes no list")
        return self


class EnvironmentSecret(Definition):
    """Where a secret of the environment may go, and how. Its value is kept apart, sealed (spec §10)."""

    hosts: Annotated[tuple[HostEntry, ...], entries("hosts"), AS_WRITTEN, Field(min_length=1)]
    header: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$")] | None = None
    prefix: Annotated[str, Field(max_length=64)] = ""


def _image(value: str) -> str:
    if not value or len(value) > 500 or any(c.isspace() for c in value):
        raise ValueError("image is an image reference, with no spaces")
    return value


class Environment(Definition):
    name: Annotated[str, AfterValidator(lambda v: check_name(v, "name"))]
    image: Annotated[str, AfterValidator(_image)]
    cpus: Annotated[float, Field(ge=0.25, le=64)] = 2
    memory_mb: Annotated[int, Field(ge=256, le=262_144)] = 4096
    pids: Annotated[int, Field(ge=64, le=65_536)] = 512
    tmp_mb: Annotated[int, Field(ge=64, le=65_536)] = 1024
    disk_mb: Annotated[int, Field(ge=64, le=1_048_576)] = 10_240
    turn_timeout_s: Annotated[int, Field(ge=1, le=86_400)] = 1800
    network: Network = Network()
    variables: dict[str, Annotated[str, Field(max_length=VALUE_MAX)]] = Field(default_factory=dict)
    secrets: dict[str, EnvironmentSecret] = Field(default_factory=dict)

    @classmethod
    def load(cls, data: object, *, default_image: str) -> Self:      # type: ignore[override]
        return super().load(data, image=default_image) if isinstance(data, dict) and "image" not in data \
            else super().load(data)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.tmp_mb > self.memory_mb:
            raise ValueError(f"tmp_mb ({self.tmp_mb}) counts against memory_mb ({self.memory_mb}) and cannot exceed it")
        if len(self.variables) > DECLARED_MAX:
            raise ValueError(f"variables holds at most {DECLARED_MAX} entries, not {len(self.variables)}")
        for name in self.variables:
            if not _VARIABLE.fullmatch(name):
                raise ValueError(f"variables: {name!r} is not a variable name")
            if is_reserved(name):
                raise ValueError(f"variables: {name} is set by the platform and cannot be used")
        check_declared_names(list(self.secrets), "secrets")
        both = sorted(set(self.variables) & set(self.secrets))
        if both:
            raise ValueError(f"{both[0]} is both a variable and a secret")
        return self
