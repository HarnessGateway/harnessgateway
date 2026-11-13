"""Spec §8.2, §9, §10.1: the manifest, the environment and the provider. Every rule, and every refusal."""

from typing import Any

import pytest

from harnessgateway.domain.contract import is_reserved
from harnessgateway.domain.environment import Environment
from harnessgateway.domain.package import Manifest
from harnessgateway.domain.provider import Provider
from harnessgateway.errors import Invalid


def manifest(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"contract": 1, "name": "claude-code", "version": "2.1.323-1", "apis": ["anthropic"]}
    return {**base, **changes}


# ── the manifest ────────────────────────────────────────────────────────────────────────────────────

def test_the_smallest_manifest_gets_its_defaults() -> None:
    m = Manifest.load(manifest())
    assert (m.name, m.version, m.mind, m.apis) == ("claude-code", "2.1.323-1", None, ("anthropic",))
    assert (m.install.timeout_s, m.install.egress) == (900, ())
    assert (m.run.stop_grace_s, m.run.egress) == (10, ())
    assert m.settings == {}
    assert m.secrets == {}


def test_a_full_manifest_is_read() -> None:
    m = Manifest.load(manifest(
        description="Claude Code, unmodified.",
        mind=".claude",
        apis=["anthropic", "openai-chat"],
        install={"egress": ["registry.npmjs.org"], "timeout_s": 60},
        run={"egress": ["telemetry.example.com:8443"], "stop_grace_s": 5},
        settings={"EFFORT": {"description": "effort", "default": "medium"}},
        secrets={"GITHUB_TOKEN": {"description": "github", "hosts": ["api.github.com"], "required": True}},
    ))
    assert m.mind == ".claude"
    assert m.install.egress[0].host == "registry.npmjs.org"
    assert m.run.egress[0].port == 8443
    assert m.settings["EFFORT"].default == "medium"
    assert m.secrets["GITHUB_TOKEN"].hosts[0].host == "api.github.com"
    assert m.secrets["GITHUB_TOKEN"].required is True


def test_a_harness_that_needs_no_provider_lists_no_apis() -> None:
    assert Manifest.load(manifest(name="shell", apis=[])).apis == ()


@pytest.mark.parametrize(
    ("changes", "names"),
    [
        ({"contract": 2}, "contract"),
        ({"name": "Claude"}, "name"),
        ({"version": ""}, "version"),
        ({"version": "1 2"}, "version"),
        ({"apis": ["grpc"]}, "apis"),
        ({"apis": ["anthropic", "anthropic"]}, "apis"),
        ({"mind": "/abs"}, "mind"),
        ({"mind": "../up"}, "mind"),
        ({"mind": "a//b"}, "mind"),
        ({"mind": ""}, "mind"),
        ({"mind": ".harnessgateway"}, "mind"),
        ({"mind": ".harnessgateway/x"}, "mind"),
        ({"install": {"timeout_s": 0}}, "install"),
        ({"install": {"timeout_s": 3601}}, "install"),
        ({"install": {"egress": ["https://x"]}}, "install"),
        ({"run": {"stop_grace_s": 301}}, "run"),
        ({"settings": {"lower": {}}}, "settings"),
        ({"settings": {"HG_MODEL": {}}}, "settings"),
        ({"settings": {"HOME": {}}}, "settings"),
        ({"secrets": {"TOKEN": {"hosts": []}}}, "secrets"),
        ({"secrets": {"TOKEN": {}}}, "secrets"),
        ({"settings": {"X": {}}, "secrets": {"X": {"hosts": ["a.com"]}}}, "X"),
        ({"surprise": True}, "surprise"),
        ({"description": "d" * 4001}, "description"),
    ],
)
def test_the_manifest_refuses_what_is_not_allowed_and_names_it(changes: dict[str, Any], names: str) -> None:
    with pytest.raises(Invalid) as e:
        Manifest.load(manifest(**changes))
    assert names in str(e.value)


def test_a_manifest_missing_a_required_field_names_the_field() -> None:
    for field in ("contract", "name", "version", "apis"):
        data = manifest()
        del data[field]
        with pytest.raises(Invalid) as e:
            Manifest.load(data)
        assert field in str(e.value)


def test_a_manifest_holds_at_most_64_settings() -> None:
    with pytest.raises(Invalid) as e:
        Manifest.load(manifest(settings={f"S{i}": {} for i in range(65)}))
    assert "64" in str(e.value)


def test_something_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(Invalid):
        Manifest.load(["not", "an", "object"])


# ── reserved names ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["HG_ANYTHING", "HOME", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "SSL_CERT_FILE",
                                  "NODE_EXTRA_CA_CERTS", "PIP_CERT"])
def test_the_contract_reserves_its_own_names(name: str) -> None:
    assert is_reserved(name)


@pytest.mark.parametrize("name", ["TZ", "EFFORT", "PATH", "GITHUB_TOKEN"])
def test_other_names_are_free(name: str) -> None:
    assert not is_reserved(name)


# ── the environment ─────────────────────────────────────────────────────────────────────────────────

def test_an_environment_with_only_a_name_gets_the_defaults() -> None:
    e = Environment.load({"name": "default"}, default_image="sandbox:4")
    assert (e.image, e.cpus, e.memory_mb, e.pids, e.tmp_mb, e.disk_mb, e.turn_timeout_s) == (
        "sandbox:4", 2, 4096, 512, 1024, 10240, 1800)
    assert (e.network.mode, e.network.allow, e.network.deny, e.network.package_managers) == ("limited", (), (), True)
    assert e.variables == {}
    assert e.secrets == {}


def test_a_full_environment_is_read() -> None:
    e = Environment.load({
        "name": "secure", "image": "ghcr.io/acme/agents:1", "cpus": 0.5, "memory_mb": 512, "pids": 64,
        "tmp_mb": 64, "disk_mb": 100, "turn_timeout_s": 60,
        "network": {"mode": "open", "allow": ["a.com"], "deny": ["*.bad.com"], "package_managers": False},
        "variables": {"TZ": "UTC", "npm_config_loglevel": "error"},
        "secrets": {"EXAMPLE_TOKEN": {"hosts": ["api.example.com"], "header": "Authorization", "prefix": "Bearer "}},
    }, default_image="sandbox:4")
    assert e.image == "ghcr.io/acme/agents:1"
    assert e.network.deny[0].host == "*.bad.com"
    assert e.secrets["EXAMPLE_TOKEN"].header == "Authorization"
    assert e.secrets["EXAMPLE_TOKEN"].prefix == "Bearer "


@pytest.mark.parametrize(
    ("changes", "names"),
    [
        ({"name": "Bad Name"}, "name"),
        ({"image": ""}, "image"),
        ({"image": "has space"}, "image"),
        ({"cpus": 0.1}, "cpus"),
        ({"cpus": 65}, "cpus"),
        ({"memory_mb": 255}, "memory_mb"),
        ({"pids": 63}, "pids"),
        ({"tmp_mb": 63}, "tmp_mb"),
        ({"disk_mb": 63}, "disk_mb"),
        ({"turn_timeout_s": 0}, "turn_timeout_s"),
        ({"turn_timeout_s": 86401}, "turn_timeout_s"),
        ({"network": {"mode": "wide"}}, "network"),
        ({"network": {"allow": ["*"]}}, "network.allow"),
        ({"variables": {"HG_X": "1"}}, "HG_X"),
        ({"variables": {"1BAD": "1"}}, "variables"),
        ({"variables": {"A": "x" * 16385}}, "variables"),
        ({"secrets": {"T": {"hosts": []}}}, "secrets"),
        ({"secrets": {"HOME": {"hosts": ["a.com"]}}}, "HOME"),
        ({"variables": {"T": "1"}, "secrets": {"T": {"hosts": ["a.com"]}}}, "T"),
        ({"secrets": {"T": {"hosts": ["a.com"], "value": "shh"}}}, "value"),
        ({"extra": 1}, "extra"),
    ],
)
def test_the_environment_refuses_what_is_not_allowed_and_names_it(changes: dict[str, Any], names: str) -> None:
    with pytest.raises(Invalid) as e:
        Environment.load({"name": "x", **changes}, default_image="sandbox:4")
    assert names in str(e.value)


def test_tmp_must_fit_in_memory() -> None:
    with pytest.raises(Invalid) as e:
        Environment.load({"name": "x", "memory_mb": 512, "tmp_mb": 1024}, default_image="sandbox:4")
    assert "tmp_mb" in str(e.value)


# ── the provider ────────────────────────────────────────────────────────────────────────────────────

def test_a_provider_is_read() -> None:
    p = Provider.load({
        "name": "deepseek",
        "apis": {"anthropic": "https://api.deepseek.com/anthropic/", "openai-chat": "https://api.deepseek.com/v1"},
        "default_model": "deepseek-flash",
    })
    assert p.apis["anthropic"] == "https://api.deepseek.com/anthropic"      # the trailing slash is dropped
    assert p.default_model == "deepseek-flash"
    assert p.host("openai-chat").host == "api.deepseek.com"
    assert p.host("openai-chat").port is None


def test_a_provider_on_the_local_network_may_use_http_and_a_port() -> None:
    p = Provider.load({"name": "local", "apis": {"openai-chat": "http://192.168.1.20:11434/v1"}})
    assert (p.host("openai-chat").host, p.host("openai-chat").port) == ("192.168.1.20", 11434)


@pytest.mark.parametrize(
    ("changes", "names"),
    [
        ({"name": "Deep Seek"}, "name"),
        ({"apis": {}}, "apis"),
        ({"apis": {"grpc": "https://x.com"}}, "apis"),
        ({"apis": {"anthropic": "ftp://x.com"}}, "apis"),
        ({"apis": {"anthropic": "https://"}}, "apis"),
        ({"apis": {"anthropic": "https://user:pw@x.com"}}, "apis"),
        ({"apis": {"anthropic": "https://x.com/v1?key=1"}}, "apis"),
        ({"default_model": ""}, "default_model"),
        ({"default_model": "m" * 201}, "default_model"),
        ({"api_key": "sk-123"}, "api_key"),
    ],
)
def test_the_provider_refuses_what_is_not_allowed_and_names_it(changes: dict[str, Any], names: str) -> None:
    base = {"name": "p", "apis": {"anthropic": "https://api.example.com"}}
    with pytest.raises(Invalid) as e:
        Provider.load({**base, **changes})
    assert names in str(e.value)
