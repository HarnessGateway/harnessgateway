"""What a sandbox may reach."""

import json

import pytest

from harnessgateway.domain.egress import (
    BoundSecret,
    Connection,
    EgressPolicy,
    HostEntry,
    Policy,
    entries,
    is_forbidden_address,
)
from harnessgateway.errors import Invalid


def test_a_host_entry_is_a_host_a_suffix_or_either_with_a_port():
    assert HostEntry.parse("API.Example.com").matches("api.example.com", 443)
    assert HostEntry.parse("api.example.com").matches("api.example.com", 80) and not HostEntry.parse("api.example.com").matches("api.example.com", 8443)
    assert HostEntry.parse("api.example.com:8443").matches("api.example.com", 8443)
    suffix = HostEntry.parse("*.example.com")
    assert suffix.matches("a.b.example.com", 443) and not suffix.matches("example.com", 443) and not suffix.matches("badexample.com", 443)
    assert HostEntry.parse("::1").host == "::1" and str(HostEntry.parse("10.0.0.5:11434")) == "10.0.0.5:11434"
    for wrong in ("https://api.example.com", "api.example.com/path", "api.example.com:0", " a.example.com", "", 4):
        with pytest.raises(Invalid):
            HostEntry.parse(wrong)
    assert entries(["A.example.com", "b.example.com:8080"]) == ("a.example.com", "b.example.com:8080")
    with pytest.raises(ValueError, match="no scheme"):
        entries(["http://a.example.com"])


def test_addresses_on_private_and_local_networks_are_never_reachable():
    for address in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254", "::1", "fd00::1", "::ffff:10.0.0.1", "0.0.0.0"):
        assert is_forbidden_address(address), address
    assert not is_forbidden_address("93.184.216.34") and not is_forbidden_address("2606:2800:220:1:248:1893:25c8:1946")


def test_a_policy_decides_by_mode_and_says_why_not():
    limited = EgressPolicy("limited", ("api.example.com",), ("bad.example.com",))
    assert limited.decide("api.example.com", 443) == ""
    assert "network.allow" in limited.decide("other.example.com", 443)
    assert EgressPolicy("open", (), ("bad.example.com",)).decide("anything.example.org", 443) == ""
    assert "deny list" in EgressPolicy("open", (), ("bad.example.com",)).decide("bad.example.com", 443)
    assert "deny list" in EgressPolicy("limited", ("bad.example.com",), ("bad.example.com",)).decide("bad.example.com", 443)
    closed = EgressPolicy("closed", ("provider.example.com",))
    assert closed.decide("provider.example.com", 443) == "" and "closed environment" in closed.decide("pypi.org", 443)
    policy = Policy("org", "resp_1", "default", limited, (BoundSecret(("api.example.com",), "v", "hgs_x", "X-Key", "K "),))
    assert Policy.from_json(json.loads(json.dumps(policy.to_json()))) == policy
    assert policy.secrets_for("api.example.com", 443) and not policy.secrets_for("other.example.com", 443)


def test_a_policy_is_narrowed_when_it_reaches_nothing_more_and_denies_everything_it_did():
    limited = EgressPolicy("limited", ("api.example.com", "pypi.org"), ("bad.example.com",))
    for narrower in (limited, EgressPolicy("limited", ("api.example.com",), ("bad.example.com", "worse.example.com")),
                     EgressPolicy("closed", ("api.example.com",), ("bad.example.com",)), EgressPolicy("limited", (), ("bad.example.com",))):
        assert limited.narrowed_to(narrower), narrower
    for wider in (EgressPolicy("open", (), ("bad.example.com",)), EgressPolicy("limited", ("api.example.com", "new.example.com"), ("bad.example.com",)),
                  EgressPolicy("limited", ("api.example.com",)), EgressPolicy("closed", ("new.example.com",), ("bad.example.com",))):
        assert not limited.narrowed_to(wider), wider
    anything = EgressPolicy("open", (), ("bad.example.com",))
    assert anything.narrowed_to(EgressPolicy("limited", ("only.example.com",), ("bad.example.com", "x.example.com")))
    assert not anything.narrowed_to(EgressPolicy("open")) and not anything.narrowed_to(EgressPolicy("limited", ("only.example.com",)))


def test_a_connection_is_reported_whole_or_not_at_all():
    made = Connection("org", "resp_1", "default", "api.example.com", 443, sent=10, received=20)
    assert Connection(**json.loads(json.dumps(made.to_json()))) == made and made.refused == ""
    for wrong in ({"port": "443"}, {"sent": -1}, {"host": None}, {"received": 1.5}, {"refused": 4}):
        with pytest.raises(ValueError, match="a connection is"):
            Connection(**{**made.to_json(), **wrong})
