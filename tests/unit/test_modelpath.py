"""Spec §13.2: the model string, environment/harness/provider/model, every part named."""

import pytest

from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.errors import Invalid


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("default/codex/deepseek/deepseek-flash", ModelPath("default", "codex", "deepseek", "deepseek-flash")),
        ("secure/claude-code/anthropic/claude-opus-5-5",
         ModelPath("secure", "claude-code", "anthropic", "claude-opus-5-5")),
        ("default/shell/none/none", ModelPath("default", "shell", "none", "none")),
        # the model is everything after the third slash, and may hold slashes and colons
        ("default/pi/router/meta/llama-4:free", ModelPath("default", "pi", "router", "meta/llama-4:free")),
    ],
)
def test_a_valid_model_string_parses_and_is_written_back_as_it_was(text: str, expected: ModelPath) -> None:
    assert ModelPath.parse(text) == expected
    assert str(expected) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "codex",                                         # a part left out is never filled in
        "codex/deepseek",
        "default/codex/deepseek",
        "default/codex/deepseek/",
        "/codex/deepseek/flash",
        "default//deepseek/flash",
        "default:codex/deepseek/flash",
        "Default/codex/deepseek/flash",
        "default/claude code/deepseek/flash",
        "default/codex/Deep/flash",
        "default/codex/deepseek/" + "m" * 201,
        None,
        42,
    ],
)
def test_anything_else_is_refused_with_the_grammar(text: object) -> None:
    with pytest.raises(Invalid) as e:
        ModelPath.parse(text)
    assert "environment/harness/provider/model" in str(e.value)
    assert "none/none" in str(e.value)
    assert e.value.param == "model"
