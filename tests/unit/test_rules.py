from decimal import Decimal

import pytest

from harnessgateway.config import ConfigError, Settings
from harnessgateway.crypto import LocalWrapper, Sealer, UrlSigner
from harnessgateway.domain.orglimits import OrgLimits
from harnessgateway.domain.pricing import Meter, PriceTable
from harnessgateway.domain.turn import FINAL, Status, allowed, sources
from harnessgateway.errors import Invalid
from harnessgateway.netguard import is_public

KEY = bytes(range(32))


def test_pricing_applies_markup():
    table = PriceTable.build({"compute.vcpu_seconds": 0.00001}, 3)
    bill = table.bill({Meter.VCPU_SECONDS.value: Decimal(1000)})
    assert bill["cost_usd"] == "0.010000" and bill["charge_usd"] == "0.030000"
    with pytest.raises(ValueError):
        PriceTable.build({"tokens": 1}, 3)


def test_turn_states():
    assert allowed(Status.QUEUED, Status.IN_PROGRESS) and allowed(Status.IN_PROGRESS, Status.COMPLETED)
    assert not allowed(Status.COMPLETED, Status.IN_PROGRESS) and not allowed(Status.QUEUED, Status.COMPLETED)
    assert all(s.final for s in FINAL) and set(sources(Status.CANCELLED)) == {"queued", "in_progress"}


def test_org_limits():
    assert OrgLimits.parse(None).vcpus == 2
    assert OrgLimits.parse({"max_sandboxes": 3}).merged({"observability": True}).observability is True
    with pytest.raises(Invalid):
        OrgLimits.parse({"vcpus": 0})
    with pytest.raises(Invalid):
        OrgLimits.parse({"unknown": 1})


def test_sealer_binds_to_context():
    s = Sealer(LocalWrapper(KEY))
    sealed = s.seal("sk-real", "provider:prv_1")
    assert "sk-real" not in sealed and s.open(sealed, "provider:prv_1") == "sk-real"
    with pytest.raises(Exception):
        s.open(sealed, "provider:prv_2")


def test_url_signer():
    signer = UrlSigner(KEY)
    token = signer.sign("orgs/org_1/x", "GET", 60)
    assert signer.verify(token, "GET") == "orgs/org_1/x"
    assert signer.verify(token, "PUT") is None
    assert signer.verify(token[:-2] + "AA", "GET") is None
    assert UrlSigner(bytes(32)).verify(token, "GET") is None
    assert signer.verify(signer.sign("k", "GET", -1), "GET") is None


def test_netguard():
    assert is_public("8.8.8.8") and not is_public("10.0.0.1") and not is_public("169.254.169.254")
    assert not is_public("::ffff:127.0.0.1") and not is_public("100.64.0.1")


def test_settings_refuse_unsafe_runtime_and_missing_values():
    base = {"HG_DATABASE_URL": "postgresql://x", "HG_SECRET_KEY": "00" * 32, "HG_S3_BUCKET": "hg-test"}
    assert Settings.from_env(("api", "gateway"), base).roles == ("api", "gateway")
    with pytest.raises(ConfigError):
        Settings.from_env(("api",), {**base, "HG_SECRET_KEY": "short"})
    node = {"HG_API_URL": "http://a", "HG_GATEWAY_URL": "http://g"}
    with pytest.raises(ConfigError, match="runsc"):
        Settings.from_env(("node",), {**node, "HG_RUNTIME": "runc"})
    assert Settings.from_env(("node",), {**node, "HG_RUNTIME": "runc", "HG_INSECURE_RUNTIME": "1"}).runtime == "runc"
    with pytest.raises(ConfigError):
        Settings.from_env(("bogus",), base)
    for bad in ("ci", "Has_Caps", "-edge", "a" * 64):          # S3 refuses these, so the api would crash at start
        with pytest.raises(ConfigError, match="HG_S3_BUCKET"):
            Settings.from_env(("api",), {**base, "HG_S3_BUCKET": bad})
