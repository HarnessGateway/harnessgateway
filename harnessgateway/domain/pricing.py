"""What the platform charges: every infrastructure cost it accrues, metered, priced at the deployment's unit cost and
multiplied by the markup. Meters are named here and nowhere else."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum


class Meter(StrEnum):
    VCPU_SECONDS = "compute.vcpu_seconds"            # allocated vCPUs x seconds a sandbox (or install) exists
    MEMORY_GB_SECONDS = "compute.memory_gb_seconds"  # allocated memory x seconds a sandbox exists
    EGRESS_BYTES = "network.egress_bytes"            # bytes leaving: sandbox uploads, API responses to clients
    INGRESS_BYTES = "network.ingress_bytes"          # bytes entering: sandbox downloads, uploads to the API
    STORAGE_GB_HOURS = "storage.gb_hours"            # stored objects: snapshots, files, requests, responses, traces


# Illustrative unit costs in USD (a typical public cloud, on-demand). Every deployment sets its own with HG_PRICES.
DEFAULT_UNIT_COSTS: dict[str, Decimal] = {
    Meter.VCPU_SECONDS: Decimal("0.0000110"),        # ~$0.04 per vCPU-hour
    Meter.MEMORY_GB_SECONDS: Decimal("0.0000012"),   # ~$0.0045 per GB-hour
    Meter.EGRESS_BYTES: Decimal("0.00000000009"),    # ~$0.09 per GB
    Meter.INGRESS_BYTES: Decimal("0"),
    Meter.STORAGE_GB_HOURS: Decimal("0.000032"),     # ~$0.023 per GB-month
}


@dataclass(frozen=True)
class PriceTable:
    unit_costs: dict[str, Decimal]
    markup: Decimal

    @classmethod
    def build(cls, overrides: dict[str, float], markup: float) -> PriceTable:
        unknown = set(overrides) - {m.value for m in Meter}
        if unknown:
            raise ValueError(f"HG_PRICES names unknown meters: {', '.join(sorted(unknown))}")
        costs = {**DEFAULT_UNIT_COSTS, **{k: Decimal(str(v)) for k, v in overrides.items()}}
        return cls({str(k): v for k, v in costs.items()}, Decimal(str(markup)))

    def line(self, meter: str, quantity: Decimal) -> dict[str, str]:
        cost = quantity * self.unit_costs.get(meter, Decimal(0))
        return {"meter": meter, "quantity": str(quantity), "unit_cost_usd": str(self.unit_costs.get(meter, Decimal(0))),
                "cost_usd": _usd(cost), "charge_usd": _usd(cost * self.markup)}

    def bill(self, usage: dict[str, Decimal]) -> dict[str, object]:
        lines = [self.line(m, q) for m, q in sorted(usage.items())]
        total_cost = sum((Decimal(x["cost_usd"]) for x in lines), Decimal(0))
        return {"lines": lines, "markup": str(self.markup), "cost_usd": _usd(total_cost),
                "charge_usd": _usd(total_cost * self.markup)}


def _usd(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
