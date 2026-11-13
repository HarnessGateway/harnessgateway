"""Spec §13.2: the model string."""

import pytest

from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.errors import Invalid


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("claude-code", ModelPath(harness="claude-code")),
        ("claude-code/deepseek", ModelPath(harness="claude-code", provider="deepseek")),
        ("codex/deepseek/deepseek-flash", ModelPath(harness="codex", provider="deepseek", model="deepseek-flash")),
        (
            "secure:claude-code/anthropic/claude-opus-5-5",
            ModelPath(environment="secure", harness="claude-code", provider="anthropic", model="claude-opus-5-5"),
        ),
        ("big:pi", ModelPath(environment="big", harness="pi")),
        # the model is everything after the second slash, and may hold slashes and colons
        ("pi/router/meta/llama-4:free", ModelPath(harness="pi", provider="router", model="meta/llama-4:free")),
    ],
)
def test_a_valid_model_string_parses(text: str, expected: ModelPath) -> None:
    assert ModelPath.parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "/",
        "claude-code/",
        "claude-code/deepseek/",
        ":claude-code",
        "secure:",
        "a:b:c",
        "Claude",
        "claude code",
        "claude-code/Deep",
        "claude-code/deepseek/" + "m" * 201,
        None,
        42,
    ],
)
def test_anything_else_is_refused_with_the_grammar(text: object) -> None:
    with pytest.raises(Invalid) as e:
        ModelPath.parse(text)
    assert "[environment:]harness[/provider[/model]]" in str(e.value)
    assert e.value.param == "model"


def test_a_resolved_path_is_written_in_full() -> None:
    path = ModelPath(environment="default", harness="codex", provider="deepseek", model="deepseek-flash")
    assert str(path) == "default:codex/deepseek/deepseek-flash"


def test_a_harness_without_a_provider_is_written_without_one() -> None:
    assert str(ModelPath(environment="default", harness="shell")) == "default:shell"
