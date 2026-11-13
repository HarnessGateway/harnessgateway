"""Spec §5.1, §7, §8.3, §9.3: a turn's plans and its egress policy, built from what the turn resolved to."""

from typing import Any

from harnessgateway.domain.environment import Environment
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.package import Manifest
from harnessgateway.domain.plan import (
    Mount,
    TurnFacts,
    install_plan,
    install_policy,
    restore_step,
    sandbox_plan,
    save_step,
    turn_policy,
    unpack_step,
    watch_step,
)
from harnessgateway.domain.provider import Provider
from harnessgateway.domain.resolve import resolve

SESSION, TURN, INSTALL = "sess_" + "S" * 26, "resp_" + "T" * 26, "inst_" + "J" * 26
ENVIRONMENT = Environment.load({
    "name": "secure", "image": "acme/agents:1", "cpus": 1.5, "memory_mb": 2048, "pids": 256, "tmp_mb": 512,
    "network": {"allow": ["api.example.com"], "deny": ["bad.example.com"]},
    "variables": {"TZ": "UTC"},
    "secrets": {"EXAMPLE_TOKEN": {"hosts": ["api.example.com"], "header": "Authorization", "prefix": "Bearer "}},
}, default_image="sandbox:4")
MANIFEST = Manifest.load({
    "contract": 1, "name": "claude-code", "version": "2.1.323-1", "mind": ".claude", "apis": ["anthropic"],
    "install": {"egress": ["downloads.example.net"], "timeout_s": 300},
    "run": {"egress": ["telemetry.example.net"], "stop_grace_s": 7},
    "settings": {"EFFORT": {"default": "medium"}},
    "secrets": {"GITHUB_TOKEN": {"hosts": ["api.github.com"]}},
})
HARNESS = Harness(manifest=MANIFEST, digest="d" * 64, secrets_set=frozenset({"GITHUB_TOKEN"}))
PROVIDER = Provider.load({"name": "deepseek", "apis": {"anthropic": "https://api.deepseek.com/anthropic"},
                          "default_model": "deepseek-flash"})
RESOLVED = resolve(ModelPath.parse("secure:claude-code"), environments={"secure": ENVIRONMENT},
                   harnesses={"claude-code": HARNESS}, providers=[PROVIDER])


def facts(**changes: Any) -> TurnFacts:
    base: dict[str, Any] = {
        "org_id": "org_default", "session_id": SESSION, "turn_id": TURN, "install_id": INSTALL,
        "image_id": "sha256:abc", "resolved": RESOLVED, "handover": "Handover from codex. Review /mind/.codex.",
        "proxy_token": "ptok", "provider_key_placeholder": "hgs_" + "1" * 32,
        "secret_placeholders": {"EXAMPLE_TOKEN": "hgs_" + "2" * 32, "GITHUB_TOKEN": "hgs_" + "3" * 32},
    }
    return TurnFacts(**{**base, **changes})


def test_the_sandbox_plan_of_a_turn() -> None:
    plan = sandbox_plan(facts())
    assert plan.name == f"hg-{SESSION}"                      # the session's lock
    assert plan.labels == {"hg.org": "org_default", "hg.session": SESSION, "hg.turn": TURN}
    assert plan.image == "sha256:abc"
    assert plan.command == ["/harness/run"]
    assert plan.workdir == "/work"
    assert (plan.cpus, plan.memory_mb, plan.pids, plan.tmp_mb) == (1.5, 2048, 256, 512)
    assert plan.network == "hg-sandbox"
    assert plan.stop_grace_s == 7
    assert plan.mounts == [
        Mount(f"hg-mind-{SESSION}", "/mind", read_only=False),
        Mount(f"hg-work-{SESSION}", "/work", read_only=False),
        Mount(f"hg-turn-{TURN}", "/turn", read_only=True),
        Mount(f"hg-harness-{INSTALL}", "/harness", read_only=True),
        Mount("hg-ca", "/etc/hg", read_only=True),
    ]


def test_the_sandbox_s_variables_are_the_contract_then_the_environment_then_settings_then_placeholders() -> None:
    proxy = f"http://{TURN}:ptok@egress:8080"
    assert sandbox_plan(facts()).env == {
        "HOME": "/mind",
        "HG_CONTRACT": "1",
        "HG_TURN": "/turn",
        "HG_MIND": "/mind",
        "HG_WORK": "/work",
        "HG_HARNESS": "/harness",
        "HG_STATE": "/mind/.harnessgateway/claude-code",
        "HG_SESSION": SESSION,
        "HG_RESPONSE": TURN,
        "HG_HARNESS_NAME": "claude-code",
        "HG_HARNESS_VERSION": "2.1.323-1",
        "HG_ENVIRONMENT": "secure",
        "HG_PROVIDER": "deepseek",
        "HG_PROVIDER_API": "anthropic",
        "HG_PROVIDER_URL": "https://api.deepseek.com/anthropic",
        "HG_PROVIDER_KEY": "hgs_" + "1" * 32,
        "HG_MODEL": "deepseek-flash",
        "HG_HANDOVER": "Handover from codex. Review /mind/.codex.",
        "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
        "NO_PROXY": "", "no_proxy": "", "NODE_USE_ENV_PROXY": "1",
        "SSL_CERT_FILE": "/etc/hg/ca.pem", "REQUESTS_CA_BUNDLE": "/etc/hg/ca.pem", "CURL_CA_BUNDLE": "/etc/hg/ca.pem",
        "GIT_SSL_CAINFO": "/etc/hg/ca.pem", "NODE_EXTRA_CA_CERTS": "/etc/hg/ca.pem", "PIP_CERT": "/etc/hg/ca.pem",
        "TZ": "UTC",
        "EFFORT": "medium",
        "EXAMPLE_TOKEN": "hgs_" + "2" * 32,
        "GITHUB_TOKEN": "hgs_" + "3" * 32,
    }
    assert list(sandbox_plan(facts()).env)[:2] == ["HOME", "HG_CONTRACT"]


def test_a_harness_without_a_provider_in_an_environment_without_a_network() -> None:
    manifest = Manifest.load({"contract": 1, "name": "shell", "version": "1", "apis": []})
    shell = Harness(manifest=manifest, digest="e" * 64)
    offline = Environment.load({"name": "offline", "network": {"mode": "none"}}, default_image="sandbox:4")
    resolved = resolve(ModelPath.parse("offline:shell"), environments={"offline": offline},
                       harnesses={"shell": shell}, providers=[])
    plan = sandbox_plan(facts(resolved=resolved, provider_key_placeholder=None, secret_placeholders={}, handover=""))
    assert plan.network is None
    assert not any("PROXY" in name.upper() for name in plan.env)
    assert (plan.env["HG_PROVIDER"], plan.env["HG_PROVIDER_API"], plan.env["HG_PROVIDER_URL"],
            plan.env["HG_PROVIDER_KEY"], plan.env["HG_MODEL"], plan.env["HG_HANDOVER"]) == ("", "", "", "", "", "")
    assert plan.env["HG_STATE"] == "/mind/.harnessgateway/shell"


def test_the_turn_s_policy_is_everything_it_is_entitled_to_and_nothing_else() -> None:
    policy = turn_policy(RESOLVED)
    assert policy.mode == "limited"
    for host in ("api.deepseek.com", "api.example.com", "telemetry.example.net", "api.github.com",
                 "registry.npmjs.org"):
        assert policy.decide(host, 443).allowed, host
    for host in ("downloads.example.net", "bad.example.com", "anything.else"):   # install-only, denied, unlisted
        assert not policy.decide(host, 443).allowed, host


def test_a_harness_secret_with_no_value_opens_no_host() -> None:
    harness = Harness(manifest=MANIFEST, digest="d" * 64)                     # GITHUB_TOKEN not set
    resolved = resolve(ModelPath.parse("secure:claude-code"), environments={"secure": ENVIRONMENT},
                       harnesses={"claude-code": harness}, providers=[PROVIDER])
    assert not turn_policy(resolved).decide("api.github.com", 443).allowed


def test_without_the_package_manager_set_the_registries_are_closed() -> None:
    closed = Environment.load({"name": "c", "network": {"package_managers": False}}, default_image="sandbox:4")
    resolved = resolve(ModelPath.parse("c:claude-code"), environments={"c": closed},
                       harnesses={"claude-code": HARNESS}, providers=[PROVIDER])
    assert not turn_policy(resolved).decide("registry.npmjs.org", 443).allowed


def test_an_install_reaches_the_registries_and_what_the_package_names_but_no_provider() -> None:
    policy = install_policy(ENVIRONMENT, MANIFEST)
    assert policy.decide("registry.npmjs.org", 443).allowed
    assert policy.decide("downloads.example.net", 443).allowed
    assert not policy.decide("api.deepseek.com", 443).allowed
    assert not policy.decide("api.example.com", 443).allowed
    assert not policy.decide("bad.example.com", 443).allowed


def test_the_install_plan() -> None:
    plan = install_plan(org_id="org_default", install_id=INSTALL, image_id="sha256:abc", environment=ENVIRONMENT,
                        manifest=MANIFEST, proxy_token="itok")
    proxy = f"http://{INSTALL}:itok@egress:8080"
    assert plan.name == f"hg-install-{INSTALL}"
    assert plan.labels == {"hg.org": "org_default", "hg.install": INSTALL}
    assert plan.command == ["/harness/install"]
    assert plan.workdir == "/harness"
    assert plan.tmp_mb is None                                # disk-backed and discarded
    assert plan.mounts == [Mount(f"hg-harness-{INSTALL}", "/harness", read_only=False),
                           Mount("hg-ca", "/etc/hg", read_only=True)]
    assert plan.env["HOME"] == "/tmp/home"
    assert (plan.env["HG_HARNESS"], plan.env["HG_HARNESS_NAME"], plan.env["HG_HARNESS_VERSION"]) == (
        "/harness", "claude-code", "2.1.323-1")
    assert plan.env["HTTPS_PROXY"] == proxy
    assert "HG_PROVIDER_KEY" not in plan.env
    assert "TZ" not in plan.env                                # an install sees none of the environment's values
    assert (plan.cpus, plan.memory_mb, plan.pids) == (1.5, 2048, 256)


def test_the_trusted_steps() -> None:
    common = {"org_id": "org_default", "internal_url": "http://serve:8001", "token": "stok"}
    restore = restore_step(session_id=SESSION, turn_id=TURN, **common)
    assert restore.name == f"hg-restore-{TURN}"
    assert restore.command == ["harnessgateway", "step", "restore", TURN]
    assert restore.env == {"HG_API": "http://serve:8001", "HG_STEP_TOKEN": "stok"}
    assert restore.network == "hg-control"
    assert restore.mounts == [Mount(f"hg-mind-{SESSION}", "/mind", read_only=False),
                              Mount(f"hg-work-{SESSION}", "/work", read_only=False),
                              Mount(f"hg-turn-{TURN}", "/turn", read_only=False)]

    save = save_step(session_id=SESSION, turn_id=TURN, **common)
    assert save.command == ["harnessgateway", "step", "save", TURN]
    assert save.mounts == [Mount(f"hg-mind-{SESSION}", "/mind", read_only=True),
                           Mount(f"hg-work-{SESSION}", "/work", read_only=True)]

    watch = watch_step(session_id=SESSION, turn_id=TURN, disk_mb=100, **common)
    assert watch.command == ["harnessgateway", "step", "watch", TURN, "--limit-mb", "100"]
    assert watch.mounts == save.mounts
    assert watch.network is None                               # it only measures; it talks to nobody

    unpack = unpack_step(install_id=INSTALL, **common)
    assert unpack.command == ["harnessgateway", "step", "unpack", INSTALL]
    assert unpack.mounts == [Mount(f"hg-harness-{INSTALL}", "/harness", read_only=False)]
    assert unpack.labels == {"hg.org": "org_default", "hg.install": INSTALL}
