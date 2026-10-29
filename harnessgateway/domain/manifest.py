"""The package manifest, `harness.json` (SPEC §5.2), and how an overlay composes over its base (SPEC §5.3)."""
from __future__ import annotations

import json
import posixpath
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..errors import Invalid
from ..limits import PERSIST_MAX, SETTINGS_MAX
from . import egress
from .names import NAME, NAME_PATTERN, env_name_ok

PROVIDER_APIS = ("anthropic", "openai", "google")
_PATH = re.compile(r"[A-Za-z0-9._@+-][A-Za-z0-9._@+/-]{0,254}")
_REF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
OVERLAY_FORBIDS = ("provider", "install")


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
    extends: Annotated[str, Field(pattern=f"^{NAME_PATTERN}$")] | None = None
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
    @property
    def is_overlay(self) -> bool:
        return self.extends is not None

    def check_references(self) -> None:
        """Every ${NAME} in an MCP header names a declared secret (checked on a base and on a composed manifest)."""
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
        if raw.get("extends") is not None:
            forbidden = [k for k in OVERLAY_FORBIDS if k in raw]
            forbidden += [f"run.{k}" for k in (raw.get("run") or {}) if k != "egress"]
            if forbidden:
                raise Invalid(f"harness.json: an overlay takes these from its base and may not set them: "
                              f"{', '.join(forbidden)}", param="harness.json")
        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            err = e.errors()[0]
            where = ".".join(str(p) for p in err.get("loc", ()))
            raise Invalid(f"harness.json: {where or 'body'}: {err.get('msg', 'invalid')}", param="harness.json") from None


def compose(base: Manifest, overlay: Manifest) -> Manifest:
    """An overlay over its base: the base's scripts, provider, install and run bounds; the overlay's defaults; the union of
    egress, persist, settings, secrets and MCP servers, where a name both define is refused."""
    assert overlay.is_overlay and not base.is_overlay
    for what in ("settings", "secrets", "mcp"):
        both = sorted(set(getattr(base, what)) & set(getattr(overlay, what)))
        if both:
            raise Invalid(f"overlay {overlay.name}: {what} already defined by {base.name}: {', '.join(both)}",
                          param="harness.json")
    data: dict[str, Any] = base.model_dump()
    data.update(name=overlay.name, version=overlay.version, description=overlay.description or base.description,
                extends=overlay.extends,
                defaults=(overlay.defaults if overlay.defaults.model else base.defaults).model_dump(),
                persist=sorted({*base.persist, *overlay.persist}),
                settings={**data["settings"], **overlay.model_dump()["settings"]},
                secrets={**data["secrets"], **overlay.model_dump()["secrets"]},
                mcp={**data["mcp"], **overlay.model_dump()["mcp"]})
    data["run"]["egress"] = egress.entries([*base.run.egress, *overlay.run.egress])
    composed = Manifest.model_validate(data)
    composed.check_references()
    return composed
