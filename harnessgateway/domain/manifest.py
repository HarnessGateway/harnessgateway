"""The package manifest, `harness.json` (SPEC §5.2)."""
from __future__ import annotations

import json
import posixpath
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..errors import Invalid
from ..limits import PERSIST_MAX, SETTINGS_MAX
from . import egress
from .names import NAME, NAME_PATTERN, env_name_ok

PROVIDER_APIS = ("anthropic", "openai", "google")
_PATH = re.compile(r"[A-Za-z0-9._@+-][A-Za-z0-9._@+/-]{0,254}")
_REF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def _entries(values: list[str]) -> list[str]:
    return egress.entries(values)


def _allow_entries(values: list[str]) -> list[str]:
    out = egress.entries(values)
    if any(v.startswith("!") for v in out):
        raise ValueError("a secret's hosts are allow entries, without !")
    return out


def _persist(values: list[str]) -> list[str]:
    if len(values) > PERSIST_MAX:
        raise ValueError(f"persist names at most {PERSIST_MAX} paths")
    for v in values:
        if not _PATH.fullmatch(v) or posixpath.normpath(v) != v or v.split("/")[0] in ("..", "."):
            raise ValueError(f"a persist entry is a normalized path relative to $HOME, without '..': {v!r}")
    return sorted(set(values))


def _apis(values: list[str]) -> list[str]:
    unknown = [v for v in values if v not in PROVIDER_APIS]
    if unknown:
        raise ValueError(f"provider.apis are among {', '.join(PROVIDER_APIS)}, not {', '.join(map(repr, unknown))}")
    return values


Entries = Annotated[list[str], AfterValidator(_entries)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provider(_Strict):
    apis: Annotated[list[str], Field(min_length=1), AfterValidator(_apis)]


class Defaults(_Strict):
    model: Annotated[str, Field(max_length=200)] = ""


class Install(_Strict):
    egress: Entries = []
    timeout_s: Annotated[int, Field(ge=1, le=3600)] = 900


class Run(_Strict):
    egress: Entries = []
    timeout_s: Annotated[int, Field(ge=1)] | None = None
    stop_grace_s: Annotated[int, Field(ge=1, le=300)] = 20


class Setting(_Strict):
    description: Annotated[str, Field(max_length=500)] = ""
    default: Annotated[str, Field(max_length=16_384)] | None = None
    required: bool = False


class Secret(_Strict):
    description: Annotated[str, Field(max_length=500)] = ""
    hosts: Annotated[list[str], Field(min_length=1), AfterValidator(_allow_entries)]
    required: bool = False


class McpServer(_Strict):
    url: Annotated[str, Field(max_length=2048)]
    transport: Literal["http", "sse"] = "http"
    headers: dict[str, Annotated[str, Field(max_length=4096)]] = {}
    allowed_tools: list[Annotated[str, Field(max_length=200)]] | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        u = urlsplit(v)
        if u.scheme != "https" or not u.hostname:
            raise ValueError(f"an MCP server url is https://host/..., not {v!r}")
        egress.Entry.parse(egress.host_entry(u.hostname, u.port))
        return v

    def host(self) -> str:
        u = urlsplit(self.url)
        assert u.hostname
        return egress.host_entry(u.hostname, u.port)

    def secret_refs(self) -> set[str]:
        return {m for value in self.headers.values() for m in _REF.findall(value)}


class Manifest(_Strict):
    contract: Literal[1]
    name: Annotated[str, Field(pattern=f"^{NAME_PATTERN}$")]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    description: Annotated[str, Field(max_length=4000)] = ""
    provider: Provider | None = None
    defaults: Defaults = Defaults()
    install: Install = Install()
    run: Run = Run()
    persist: Annotated[list[str], AfterValidator(_persist)] = []
    settings: dict[str, Setting] = {}
    secrets: dict[str, Secret] = {}
    mcp: dict[str, McpServer] = {}

    @model_validator(mode="after")
    def _names(self) -> Manifest:
        for what, names in (("settings", self.settings), ("secrets", self.secrets)):
            if len(names) > SETTINGS_MAX:
                raise ValueError(f"{what}: at most {SETTINGS_MAX}")
            bad = [n for n in names if not env_name_ok(n)]
            if bad:
                raise ValueError(f"{what} are named like environment variables ([A-Z][A-Z0-9_]*), not starting HG_ and "
                                 f"not a reserved name: {', '.join(bad)}")
        both = sorted(set(self.settings) & set(self.secrets))
        if both:
            raise ValueError(f"a name is a setting or a secret, not both: {', '.join(both)}")
        bad_labels = [label for label in self.mcp if not NAME.fullmatch(label)]
        if bad_labels:
            raise ValueError(f"mcp labels are names ([a-z0-9][a-z0-9._-]*): {', '.join(bad_labels)}")
        return self

    # ── reading ────────────────────────────────────────────────────────────────────────────────────────────────────
    def check_references(self) -> None:
        """Every ${NAME} in an MCP header names a declared secret."""
        for label, server in self.mcp.items():
            unknown = sorted(server.secret_refs() - set(self.secrets))
            if unknown:
                raise Invalid(f"harness.json: mcp.{label} refers to undeclared secrets: {', '.join(unknown)}",
                              param="harness.json")

    def run_entries(self) -> list[str]:
        """The harness level of run egress: its own entries, its catalog's hosts and its secrets' hosts."""
        implied = [s.host() for s in self.mcp.values()] + [h for s in self.secrets.values() for h in s.hosts]
        return egress.entries([*self.run.egress, *implied])

    # ── parsing ────────────────────────────────────────────────────────────────────────────────────────────────────
    @classmethod
    def parse(cls, text: str) -> Manifest:
        try:
            raw = json.loads(text)
        except ValueError:
            raise Invalid("harness.json is not JSON", param="harness.json") from None
        if not isinstance(raw, dict):
            raise Invalid("harness.json is a JSON object", param="harness.json")
        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            err = e.errors()[0]
            where = ".".join(str(p) for p in err.get("loc", ()))
            raise Invalid(f"harness.json: {where or 'body'}: {err.get('msg', 'invalid')}", param="harness.json") from None

