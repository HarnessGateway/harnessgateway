"""Spec §10.2: from a model string to an environment, a harness, a provider, an API and a model."""

from typing import Any

import pytest

from harnessgateway.domain.environment import Environment
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.package import Manifest
from harnessgateway.domain.provider import Provider
from harnessgateway.domain.resolve import resolve
from harnessgateway.errors import Invalid


def harness(name: str, apis: list[str], **more: Any) -> Harness:
    extra = {k: more.pop(k) for k in ("settings", "secrets") if k in more}
    manifest = Manifest.load({"contract": 1, "name": name, "version": "1", "apis": apis, **extra})
    return Harness(manifest=manifest, digest="d" * 64, **more)


def provider(name: str, **apis: str) -> Provider:
    return Provider.load({"name": name, "apis": {k.replace("_", "-"): v for k, v in apis.items()}})


ENVIRONMENTS = {
    "default": Environment.load({"name": "default"}, default_image="sandbox:4"),
    "closed": Environment.load({"name": "closed", "network": {"mode": "closed"}}, default_image="sandbox:4"),
}
DEEPSEEK = Provider.load({
    "name": "deepseek",
    "apis": {"anthropic": "https://api.deepseek.com/anthropic", "openai-responses": "https://api.deepseek.com/v1"},
    "default_model": "deepseek-flash",
})
OPENAI = provider("openai", openai_responses="https://api.openai.com/v1")
PROVIDERS = [OPENAI, DEEPSEEK]                         # in the order they were created
HARNESSES = {
    "claude-code": harness("claude-code", ["anthropic"]),
    "codex": harness("codex", ["openai-responses"]),
    "shell": harness("shell", []),
}


def run(text: str, **overrides: Any) -> Any:
    world: dict[str, Any] = {"environments": ENVIRONMENTS, "harnesses": HARNESSES, "providers": PROVIDERS}
    return resolve(ModelPath.parse(text), **{**world, **overrides})


def test_everything_named_resolves_to_exactly_that() -> None:
    r = run("default:codex/deepseek/deepseek-v4-pro")
    assert (r.environment.name, r.harness.name, r.provider.name, r.api, r.model) == (
        "default", "codex", "deepseek", "openai-responses", "deepseek-v4-pro")
    assert r.url == "https://api.deepseek.com/v1"
    assert str(r.path) == "default:codex/deepseek/deepseek-v4-pro"


def test_the_environment_defaults_to_default() -> None:
    assert run("codex/deepseek").environment.name == "default"


def test_the_same_provider_and_model_serve_two_harnesses_over_different_apis() -> None:
    a, b = run("claude-code/deepseek/deepseek-flash"), run("codex/deepseek/deepseek-flash")
    assert (a.api, a.url) == ("anthropic", "https://api.deepseek.com/anthropic")
    assert (b.api, b.url) == ("openai-responses", "https://api.deepseek.com/v1")


def test_with_no_provider_named_the_first_created_that_shares_an_api_is_used() -> None:
    openai = Provider.load({**OPENAI.model_dump(), "default_model": "gpt"})
    providers = [openai, DEEPSEEK]
    assert run("codex", providers=providers).provider.name == "openai"         # created first, speaks responses
    assert run("claude-code", providers=providers).provider.name == "deepseek"  # openai does not speak anthropic


def test_the_harness_default_provider_wins_over_creation_order() -> None:
    harnesses = {**HARNESSES, "codex": harness("codex", ["openai-responses"], provider="deepseek")}
    assert run("codex", harnesses=harnesses).provider.name == "deepseek"


def test_the_model_falls_back_from_the_request_to_the_harness_to_the_provider() -> None:
    harnesses = {**HARNESSES, "codex": harness("codex", ["openai-responses"], provider="deepseek", model="pinned")}
    assert run("codex/deepseek/asked", harnesses=harnesses).model == "asked"
    assert run("codex/deepseek", harnesses=harnesses).model == "pinned"
    assert run("codex/deepseek").model == "deepseek-flash"


def test_the_harness_picks_its_most_preferred_api_that_the_provider_offers() -> None:
    harnesses = {"both": harness("both", ["openai-chat", "anthropic", "openai-responses"])}
    assert run("both/deepseek", harnesses=harnesses).api == "anthropic"


def test_a_harness_that_needs_no_provider_resolves_without_one() -> None:
    r = run("shell")
    assert (r.provider, r.api, r.url, r.model) == (None, None, None, None)
    assert str(r.path) == "default:shell"


def test_any_harness_runs_in_a_closed_environment() -> None:
    assert run("closed:shell").environment.name == "closed"
    with_provider = run("closed:codex/deepseek")                  # it reaches its provider, and nothing else
    assert (with_provider.environment.name, with_provider.provider.name) == ("closed", "deepseek")


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("nowhere:codex", "no environment named 'nowhere'"),
        ("aider", "no harness named 'aider'"),
        ("codex/nobody", "no provider named 'nobody'"),
        ("claude-code/openai", "speaks"),                       # no API in common
        ("codex/openai", "model"),                              # no model anywhere
        ("shell/deepseek", "uses no provider"),
    ],
)
def test_what_does_not_resolve_is_refused_and_says_why(text: str, says: str) -> None:
    with pytest.raises(Invalid) as e:
        run(text)
    assert says in str(e.value)
    assert e.value.param == "model"


def test_with_no_provider_that_shares_an_api_the_refusal_names_the_api_needed() -> None:
    with pytest.raises(Invalid) as e:
        run("claude-code", providers=[OPENAI])
    assert "anthropic" in str(e.value)


def test_a_required_setting_or_secret_without_a_value_refuses_the_request() -> None:
    needs_setting = harness("h", [], settings={"REGION": {"required": True}})
    with pytest.raises(Invalid) as e:
        run("h", harnesses={"h": needs_setting})
    assert "REGION" in str(e.value)

    needs_secret = harness("h", [], secrets={"TOKEN": {"hosts": ["a.com"], "required": True}})
    with pytest.raises(Invalid) as e:
        run("h", harnesses={"h": needs_secret})
    assert "TOKEN" in str(e.value)


def test_a_required_setting_is_met_by_a_value_or_a_default() -> None:
    by_value = harness("h", [], settings={"REGION": {"required": True}}, settings_values={"REGION": "eu"})
    by_default = harness("h", [], settings={"REGION": {"required": True, "default": "us"}})
    assert run("h", harnesses={"h": by_value}).harness.setting_values() == {"REGION": "eu"}
    assert run("h", harnesses={"h": by_default}).harness.setting_values() == {"REGION": "us"}
