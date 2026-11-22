"""The three things an organization defines: providers, environments and harnesses.

Each is validated in full when it is written, so that a turn never starts on a definition that cannot work. A
definition holds no secret value: those are sealed beside it, by the names `secret_names` gives.
"""

import json
import re
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from harnessgateway.domain import contract
from harnessgateway.domain.contract import ApiName
from harnessgateway.domain.egress import HostEntry, entries
from harnessgateway.errors import Invalid
from harnessgateway.ids import check_name

VALUE_MAX = 16_384                             # the longest a variable or a secret may be
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_SECRET = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SAID = {"extra_forbidden": "is not a known field", "missing": "is required"}


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @classmethod
    def load(cls, data: object) -> Self:
        """The definition `data` describes; or a refusal naming the first field that is wrong. It is read as
        JSON, strictly: the text "1" is not the number 1."""
        try:
            return cls.model_validate_json(json.dumps(data), strict=True)
        except ValidationError as error:
            first = error.errors()[0]
            field = ".".join(str(part) for part in first["loc"])
            said = _SAID.get(first["type"]) or first["msg"].removeprefix("Value error, ")
            raise Invalid(f"{field}: {said}" if field else said, param=field or None) from None

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def secret_names(self) -> tuple[str, ...]:
        """The names its secret values are kept under."""
        return ()


def _hosts(values: tuple[str, ...]) -> tuple[str, ...]:
    return entries(values)


def _named(names: Any, pattern: re.Pattern[str], what: str) -> None:
    for name in names:
        if not pattern.fullmatch(name):
            raise ValueError(f"{name!r} is not a name {what} may have")
        if contract.is_reserved(name):
            raise ValueError(f"{name} is set by the platform in every sandbox")


def _base_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError("a base URL is http(s)://host[:port][/path], with no credentials and no query")
    return value.rstrip("/")


Hosts = Annotated[tuple[str, ...], AfterValidator(_hosts)]
Text = Annotated[str, Field(max_length=VALUE_MAX)]


class Provider(Definition):
    """A model endpoint: each API shape it speaks, at its base URL. Its secret is its `key`."""

    apis: Annotated[dict[ApiName, Annotated[str, AfterValidator(_base_url)]], Field(min_length=1)]

    def host(self, api: str) -> str:
        """Where `api` is served, as a host entry: what a turn that uses it must be able to reach."""
        url = urlsplit(self.apis[api])     # type: ignore[index]  # `api` is one of its keys
        return str(HostEntry(str(url.hostname), url.port))

    def secret_names(self) -> tuple[str, ...]:
        return ("key",)


class Network(Definition):
    """What a sandbox may reach. `limited` is what `allow` lists and, unless it says not, the package managers."""

    mode: Literal["closed", "limited", "open"] = "limited"
    allow: Hosts = ()
    deny: Hosts = ()
    package_managers: bool = True

    @model_validator(mode="after")
    def _lists_fit_the_mode(self) -> Self:
        if self.mode != "limited" and self.allow:
            raise ValueError(f"allow has no meaning in a network that is {self.mode}")
        if self.mode == "closed" and self.deny:
            raise ValueError("deny has no meaning in a network that is closed")
        return self


class EnvironmentSecret(Definition):
    """Where an environment's secret may be sent. With a header, the proxy sets it on every request there."""

    hosts: Annotated[Hosts, Field(min_length=1)]
    header: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$")] | None = None
    prefix: Annotated[str, Field(max_length=64)] = ""


class Scripts(Definition):
    """Programs an environment runs in its sandboxes. `startup` runs before every turn, as the sandbox's user."""

    startup: Annotated[str, Field(max_length=64 * 1024)] = ""


class Environment(Definition):
    """Where turns run: an image, the limits of one sandbox, what it may reach and is given, and the harnesses
    a request may name with it."""

    image: Annotated[str, Field(min_length=1, max_length=500, pattern=r"^\S+$")]
    harnesses: Annotated[tuple[str, ...], Field(max_length=256)] = ()
    cpus: Annotated[float, Field(ge=0.25, le=64)] = 2
    memory_mb: Annotated[int, Field(ge=256, le=262_144)] = 4096
    pids: Annotated[int, Field(ge=64, le=65_536)] = 512
    tmp_mb: Annotated[int, Field(ge=64, le=65_536)] = 1024
    disk_mb: Annotated[int, Field(ge=64, le=1_048_576)] = 10_240       # the most a session's two folders may hold
    timeout_s: Annotated[int, Field(ge=1, le=86_400)] = 1800           # the most one turn may take
    network: Network = Network()
    variables: dict[str, Text] = Field(default_factory=dict)
    secrets: dict[str, EnvironmentSecret] = Field(default_factory=dict)
    scripts: Scripts = Scripts()

    @model_validator(mode="after")
    def _names_are_usable(self) -> Self:
        for harness in self.harnesses:
            check_name(harness, "a harness")
        if len(set(self.harnesses)) != len(self.harnesses):
            raise ValueError("harnesses names a harness twice")
        _named(self.variables, _VARIABLE, "a variable")
        _named(self.secrets, _SECRET, "a secret")
        if both := sorted(set(self.variables) & set(self.secrets)):
            raise ValueError(f"{both[0]} is both a variable and a secret")
        return self

    def secret_names(self) -> tuple[str, ...]:
        return tuple(self.secrets)


class Harness(Definition):
    """An agent an organization has: a package, by the digest of its files. Everything else about how it runs is
    its environment's to say."""

    digest: str


KINDS: dict[str, type[Definition]] = {"providers": Provider, "environments": Environment, "harnesses": Harness}
SINGULAR = {"providers": "provider", "environments": "environment", "harnesses": "harness"}
