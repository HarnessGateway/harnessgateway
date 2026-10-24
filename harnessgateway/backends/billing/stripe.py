"""Billing through Stripe's meter events: one event per meter per reading, named after the meter
(`hg_cpu_seconds`, …), against the organization's Stripe customer id. A failed report is logged and
dropped; the usage record stays, so a reconciliation can replay it."""
from __future__ import annotations

import sys
from typing import Mapping

import httpx

API = "https://api.stripe.com/v1/billing/meter_events"


class StripeBilling:
    enabled = True

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise RuntimeError("set HG_STRIPE_KEY to bill through Stripe")
        self._http = httpx.Client(auth=(api_key, ""), timeout=20, transport=transport)

    def report(self, customer: str, readings: Mapping[str, float], at: float) -> None:
        if not customer:
            return                                    # an organization without a customer id is not billed
        for meter, value in readings.items():
            if not value:
                continue
            try:
                r = self._http.post(API, data={"event_name": f"hg_{meter}", "timestamp": int(at),
                                               "payload[stripe_customer_id]": customer, "payload[value]": f"{value:.6f}".rstrip("0").rstrip(".")})
                if r.status_code >= 400:
                    print(f"stripe refused {meter} for {customer}: {r.status_code} {r.text[:200]}", file=sys.stderr, flush=True)
            except httpx.HTTPError as e:
                print(f"stripe unreachable for {meter}: {e}", file=sys.stderr, flush=True)
