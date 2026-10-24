"""Conformance: a billing backend takes meter readings for a customer. None keeps nothing; Stripe
posts one meter event per non-zero reading and skips an organization without a customer id."""
import httpx
import pytest

from harnessgateway.backends.billing import METERS, open_billing
from harnessgateway.backends.billing.none import NoBilling
from harnessgateway.backends.billing.stripe import StripeBilling


def test_none():
    b = open_billing("none", {})
    assert isinstance(b, NoBilling) and not b.enabled
    b.report("cus_1", {m: 1.0 for m in METERS}, 1700000000)


def test_stripe_meter_events():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((request.url.path, dict(x.split("=", 1) for x in request.content.decode().split("&")), request.headers.get("authorization", "")))
        return httpx.Response(200, json={"object": "billing.meter_event"})
    b = StripeBilling("sk_test_x", transport=httpx.MockTransport(handler))
    assert b.enabled
    b.report("cus_1", {"cpu_seconds": 12.5, "egress_bytes": 0, "storage_gb_days": 0.25}, 1700000000)
    assert [p[1]["event_name"] for p in posted] == ["hg_cpu_seconds", "hg_storage_gb_days"]
    assert posted[0][1]["payload%5Bstripe_customer_id%5D"] == "cus_1" and posted[0][1]["payload%5Bvalue%5D"] == "12.5" and posted[0][1]["timestamp"] == "1700000000"
    assert posted[0][2].startswith("Basic ")
    b.report("", {"cpu_seconds": 1}, 0)
    assert len(posted) == 2                                                  # no customer id: not billed
    with pytest.raises(RuntimeError):
        open_billing("stripe", {})
    with pytest.raises(ValueError):
        open_billing("paypal", {})
