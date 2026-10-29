"""The extension seam (SPEC §13): how a SaaS adds organizations, users, plans, billing and its own observability
pipeline on top of the core without forking it.

`HG_EXTENSIONS` names classes as `module:Class`; each is instantiated once and implements any subset of the hooks
below. Every default does nothing, which is exactly the self-hosted behaviour. `observe`, `meter` and `turn_ended`
run after the core's own write and never fail a turn."""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from .services.platform import Platform
    from .services.principal import Principal

log = logging.getLogger("harnessgateway.extensions")


@dataclass(frozen=True)
class Admission:
    """A turn about to be queued."""
    org_id: str
    harness: str
    model: str
    previous_response_id: str | None
    background: bool


@dataclass(frozen=True)
class TraceRecord:
    """One observability record (SPEC §11): kind is harness, gateway or door."""
    id: str
    org_id: str
    response_id: str
    kind: str
    at: float
    data: dict[str, Any]


@dataclass(frozen=True)
class UsageRecord:
    """One metered quantity (SPEC §12)."""
    id: str
    org_id: str
    meter: str
    quantity: float
    kind: str
    start: float
    end: float
    harness: str | None = None
    session_id: str | None = None
    response_id: str | None = None


@dataclass(frozen=True)
class TurnSummary:
    id: str
    org_id: str
    session_id: str
    harness: str
    model: str
    status: str
    reason: str | None
    usage: dict[str, Any] | None
    created_at: float
    ended_at: float
    metadata: dict[str, str] = field(default_factory=dict)


class Extension:
    """Override what you need."""
    migrations: str | None = None            # an importable package of NNNN_*.sql files, applied after the core's

    def bind(self, platform: Platform) -> None:
        """Called once with the core's services (orgs, keys, harnesses, usage, traces, ...)."""

    def router(self) -> APIRouter | None:
        return None

    async def authenticate(self, request: Request) -> Principal | None:
        """A principal for this request, or None to let the next extension (and then the API keys) try."""
        return None

    async def admit(self, principal: Principal, turn: Admission) -> None:
        """Raise errors.Refused (or any ApiError) to refuse the turn before it is queued."""

    async def observe(self, record: TraceRecord) -> None:
        """Every observability record, as the core stores it."""

    async def meter(self, records: list[UsageRecord]) -> None:
        """Every usage batch, after the core stores it."""

    async def turn_ended(self, summary: TurnSummary) -> None:
        """A turn reached its final state."""


class Extensions:
    def __init__(self, loaded: list[Extension]):
        self.loaded = loaded

    @classmethod
    def load(cls, names: tuple[str, ...]) -> Extensions:
        loaded = []
        for name in names:
            module, _, attr = name.partition(":")
            try:
                ext = getattr(importlib.import_module(module), attr)()
            except (ImportError, AttributeError, ValueError) as e:
                raise SystemExit(f"configuration error: HG_EXTENSIONS names {name!r}, which cannot be loaded: {e}") from None
            if not isinstance(ext, Extension):
                raise SystemExit(f"configuration error: {name} is not a harnessgateway.extensions.Extension")
            loaded.append(ext)
        return cls(loaded)

    @property
    def migrations(self) -> list[str]:
        return [e.migrations for e in self.loaded if e.migrations]

    def bind(self, platform: Platform) -> None:
        for e in self.loaded:
            e.bind(platform)

    def routers(self) -> list[APIRouter]:
        return [r for e in self.loaded if (r := e.router()) is not None]

    async def authenticate(self, request: Request) -> Principal | None:
        for e in self.loaded:
            if (p := await e.authenticate(request)) is not None:
                return p
        return None

    async def admit(self, principal: Principal, turn: Admission) -> None:
        for e in self.loaded:
            await e.admit(principal, turn)

    async def _after(self, hook: str, arg: Any) -> None:
        for e in self.loaded:
            try:
                await getattr(e, hook)(arg)
            except Exception:
                log.exception("extension %s failed in %s; the core's record stands", type(e).__name__, hook)

    async def observe(self, record: TraceRecord) -> None:
        await self._after("observe", record)

    async def meter(self, records: list[UsageRecord]) -> None:
        await self._after("meter", records)

    async def turn_ended(self, summary: TurnSummary) -> None:
        await self._after("turn_ended", summary)
