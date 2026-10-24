"""Backend: where usage goes to be billed. The platform meters compute, egress and storage per
organization (`services.metering`); a billing backend takes each meter reading. `none` keeps the
readings in the usage records and nothing else, which is a self-hosted deployment; `stripe` reports
them as Stripe meter events against the organization's customer id."""
from __future__ import annotations

from typing import Mapping, Protocol

METERS = ("cpu_seconds", "memory_gb_seconds", "egress_bytes", "storage_gb_days")


class Billing(Protocol):
    @property
    def enabled(self) -> bool: ...
    def report(self, customer: str, readings: Mapping[str, float], at: float) -> None:
        """One reading per meter for `customer`, the billing system's id for an organization."""
        ...


def open_billing(kind: str, env: Mapping[str, str]) -> Billing:
    if kind == "none":
        from .none import NoBilling
        return NoBilling()
    if kind == "stripe":
        from .stripe import StripeBilling
        return StripeBilling(env.get("HG_STRIPE_KEY", ""))
    raise ValueError(f"a billing backend is none or stripe, not {kind!r}")
