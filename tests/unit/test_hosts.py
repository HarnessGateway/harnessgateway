"""Spec §11.3, §11.4, §9.3: host entries, forbidden addresses, and the egress decision."""

import pytest

from harnessgateway.domain.hosts import (
    PACKAGE_MANAGER_HOSTS,
    EgressPolicy,
    HostEntry,
    is_forbidden_address,
    parse_entries,
)
from harnessgateway.errors import Invalid


@pytest.mark.parametrize(
    ("entry", "host", "port", "expected"),
    [
        ("api.example.com", "api.example.com", 443, True),
        ("api.example.com", "API.Example.com", 80, True),
        ("api.example.com", "api.example.com", 8443, False),       # no port given: 80 and 443 only
        ("api.example.com:8443", "api.example.com", 8443, True),
        ("api.example.com:8443", "api.example.com", 443, False),
        ("api.example.com", "evil-api.example.com", 443, False),
        ("*.example.org", "a.example.org", 443, True),
        ("*.example.org", "a.b.example.org", 443, True),
        ("*.example.org", "example.org", 443, False),              # the suffix itself is not matched
        ("*.example.org", "badexample.org", 443, False),
        ("10.1.2.3", "10.1.2.3", 443, True),
        ("*.1.2.3", "10.1.2.3", 443, False),                       # wildcards never match addresses
    ],
)
def test_matching(entry: str, host: str, port: int, expected: bool) -> None:
    assert HostEntry.parse(entry).matches(host, port) is expected


@pytest.mark.parametrize(
    "entry",
    ["", " ", "*", "https://example.com", "example.com/path", "exa mple.com", "example.com:0", "example.com:99999",
     "example.com:abc", "a.*.com", "*example.com", "-bad.example.com", None, 5],
)
def test_a_malformed_entry_is_refused(entry: object) -> None:
    with pytest.raises(Invalid):
        HostEntry.parse(entry)


def test_a_list_of_entries_is_limited_and_names_itself() -> None:
    assert len(parse_entries(["a.com", "b.com"], "network.allow")) == 2
    with pytest.raises(Invalid) as e:
        parse_entries([f"h{i}.com" for i in range(257)], "network.allow")
    assert "network.allow" in str(e.value)
    assert "256" in str(e.value)
    with pytest.raises(Invalid):
        parse_entries("a.com", "network.allow")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "0.0.0.0", "224.0.0.1",
     "::1", "fe80::1", "fd00::1", "::ffff:10.0.0.1", "::"],
)
def test_addresses_that_are_never_reached(address: str) -> None:
    assert is_forbidden_address(address) is True


@pytest.mark.parametrize("address", ["1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_reachable(address: str) -> None:
    assert is_forbidden_address(address) is False


def test_limited_mode_allows_only_what_is_listed() -> None:
    policy = EgressPolicy(mode="limited", allow=parse_entries(["api.example.com"], "allow"), deny=())
    assert policy.decide("api.example.com", 443).allowed
    refused = policy.decide("other.example.com", 443)
    assert not refused.allowed
    assert "other.example.com" in refused.reason


def test_a_denied_host_is_refused_whatever_allows_it() -> None:
    policy = EgressPolicy(
        mode="limited",
        allow=parse_entries(["*.example.com"], "allow"),
        deny=parse_entries(["bad.example.com"], "deny"),
    )
    assert policy.decide("good.example.com", 443).allowed
    assert not policy.decide("bad.example.com", 443).allowed


def test_open_mode_allows_any_host_and_port() -> None:
    policy = EgressPolicy(mode="open", allow=(), deny=())
    assert policy.decide("anything.example.net", 8443).allowed


def test_closed_mode_reaches_the_provider_it_was_given_and_says_so_of_everything_else() -> None:
    policy = EgressPolicy(mode="closed", allow=parse_entries(["api.deepseek.com"], "allow"), deny=())
    assert policy.decide("api.deepseek.com", 443).allowed
    refused = policy.decide("registry.npmjs.org", 443)
    assert not refused.allowed
    assert refused.reason == ("registry.npmjs.org:443 is not allowed: "
                              "a closed environment reaches the turn's provider only")
    assert not EgressPolicy(mode="closed", allow=(), deny=()).decide("api.deepseek.com", 443).allowed


def test_the_package_manager_set_is_registries_source_hosts_and_tools_and_no_cloud_platform() -> None:
    def reached(host: str) -> bool:
        return any(entry.matches(host, 443) for entry in PACKAGE_MANAGER_HOSTS)

    for host in ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "crates.io", "proxy.golang.org",
                 "rubygems.org", "repo.maven.apache.org", "api.nuget.org", "github.com", "codeload.github.com",
                 "gitlab.com", "bitbucket.org", "ghcr.io", "registry-1.docker.io", "deb.debian.org",
                 "security.debian.org", "archive.ubuntu.com", "ports.ubuntu.com", "dl-cdn.alpinelinux.org",
                 "nodejs.org", "astral.sh", "releases.hashicorp.com"):
        assert reached(host), host
    for host in ("s3.amazonaws.com", "storage.googleapis.com", "portal.azure.com", "api.datadoghq.com",
                 "api.anthropic.com", "api.openai.com", "example.com"):
        assert not reached(host), host
    assert len({str(entry) for entry in PACKAGE_MANAGER_HOSTS}) == len(PACKAGE_MANAGER_HOSTS)      # each once
