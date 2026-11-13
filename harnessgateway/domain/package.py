"""A harness package's manifest, harness.json (spec §8.2)."""

import re
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, PlainSerializer, model_validator

from harnessgateway.domain.contract import STATE_DIR, ApiName, is_reserved
from harnessgateway.domain.definition import Definition
from harnessgateway.domain.hosts import HostEntry, parse_entries
from harnessgateway.domain.names import check_name

DECLARED_MAX = 64
_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_VERSION = r"^[A-Za-z0-9._+-]{1,64}$"

Text = Annotated[str, Field(max_length=500)]


def entries(what: str) -> BeforeValidator:
    return BeforeValidator(lambda values: parse_entries(values, what))


AS_WRITTEN = PlainSerializer(lambda value: [str(entry) for entry in value], return_type=list[str])


def check_declared_names(names: list[str], what: str) -> None:
    """Names of settings and secrets: upper case, not reserved, and no more than the limit."""
    if len(names) > DECLARED_MAX:
        raise ValueError(f"{what} holds at most {DECLARED_MAX} entries, not {len(names)}")
    for name in names:
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError(f"{what}: {name!r} is not A-Z, 0-9 and '_', starting with a letter, up to 64 characters")
        if is_reserved(name):
            raise ValueError(f"{what}: {name} is set by the platform and cannot be used")


def _mind(value: str) -> str:
    parts = value.split("/")
    if not value or len(value) > 255 or any(part in ("", ".", "..") for part in parts):
        raise ValueError("mind is a path under $HOME with no leading '/', no '..' and no empty part")
    if parts[0] == STATE_DIR:
        raise ValueError(f"mind cannot be {STATE_DIR}, which the platform uses")
    return value


class Install(Definition):
    egress: Annotated[tuple[HostEntry, ...], entries("install.egress"), AS_WRITTEN] = ()
    timeout_s: Annotated[int, Field(ge=1, le=3600)] = 900


class Run(Definition):
    egress: Annotated[tuple[HostEntry, ...], entries("run.egress"), AS_WRITTEN] = ()
    stop_grace_s: Annotated[int, Field(ge=1, le=300)] = 10


class Setting(Definition):
    description: Text = ""
    default: Annotated[str, Field(max_length=16_384)] | None = None
    required: bool = False


class SecretDeclaration(Definition):
    description: Text = ""
    hosts: Annotated[tuple[HostEntry, ...], entries("hosts"), AS_WRITTEN, Field(min_length=1)]
    required: bool = False


def _unique_apis(value: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(set(value)) != len(value):
        raise ValueError("an API is listed twice")
    return value


class Manifest(Definition):
    contract: Literal[1]
    name: Annotated[str, AfterValidator(lambda v: check_name(v, "name"))]
    version: Annotated[str, Field(pattern=_VERSION)]
    description: Annotated[str, Field(max_length=4000)] = ""
    mind: Annotated[str, AfterValidator(_mind)] | None = None
    apis: Annotated[tuple[ApiName, ...], AfterValidator(_unique_apis)]
    install: Install = Install()
    run: Run = Run()
    settings: dict[str, Setting] = Field(default_factory=dict)
    secrets: dict[str, SecretDeclaration] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names(self) -> Self:
        check_declared_names(list(self.settings), "settings")
        check_declared_names(list(self.secrets), "secrets")
        both = sorted(set(self.settings) & set(self.secrets))
        if both:
            raise ValueError(f"{both[0]} is both a setting and a secret")
        return self
