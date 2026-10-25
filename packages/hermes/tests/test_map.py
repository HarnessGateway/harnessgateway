"""Offline: recorded Hermes turns (the `hermes-raw` records run.py logs: ACP session updates, Hermes's log lines, new
state.db transcript rows and the session/prompt result) replayed through map.py. Every printed line must be a valid
Responses event for the platform (harnessgateway.domain.wire.TurnOutput), and the outcome must be what the real turn
returned.

Fixtures, from real runs on the platform (traces' stdout), except cancel-local-mock (Hermes 0.19.0 against a local mock
provider, cancelled by SIGTERM, because the platform's stop signal does not reach the sandbox; see README):
    turn1-deepseek-anthropic   "Create hello.py ..." on deepseek/deepseek-flash (Anthropic Messages)
    turn1-deepseek-openai      the same on deepseek-openai/deepseek-flash (Chat Completions)
    turn2-resume               the recall turn, previous_response_id
    patch-edit                 a patch-tool edit of hello.py, then running it
    input-file-parallel-tools  an inline input_file; read_file and terminal called in parallel
    provider-error             model no-such-model: HTTP 400 from the provider
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
REPO = PACKAGE.parent.parent
sys.path[:0] = [str(PACKAGE), str(REPO / "sandbox"), str(REPO)]

from hgkit import Emitter  # noqa: E402
from map import Mapper  # noqa: E402

from harnessgateway.domain.wire import TurnOutput  # noqa: E402


def replay(name: str, mcp: dict[str, str] | None = None) -> tuple[list[dict], TurnOutput]:
    buf = io.StringIO()
    mapper = Mapper(Emitter(stream=buf), mcp)
    for line in (HERE / "fixtures" / f"{name}.jsonl").read_text().splitlines():
        kind, value = json.loads(line)
        mapper.record(kind, value)
    out = TurnOutput()
    events = []
    for line in buf.getvalue().splitlines():
        kind, value = out.line(line)
        assert kind in ("event", "terminal"), f"not a Responses event: {line[:200]}"
        if kind == "event":
            events.append(value)
    assert out.terminal is not None, "no terminal event"
    return events, out


def types(output: list[dict]) -> list[str]:
    return [i["type"] for i in output]


def text(output: list[dict]) -> str:
    return "".join(c["text"] for i in output if i["type"] == "message" for c in i["content"])


@pytest.mark.parametrize("name", ["turn1-deepseek-anthropic", "turn1-deepseek-openai"])
def test_turn1_creates_and_runs(name: str) -> None:
    events, out = replay(name)
    status, output, usage, error, _, meta = out.outcome()
    assert status == "completed" and error is None
    assert "hello from hermes" in text(output)
    patch = next(i for i in output if i["type"] == "apply_patch_call")
    assert patch["operation"] == {"type": "create_file", "path": "hello.py", "diff": "+print('hello from hermes')\n"}
    assert any(i["type"] == "apply_patch_call_output" and i["call_id"] == patch["call_id"] for i in output)
    shell = next(i for i in output if i["type"] == "shell_call")
    assert "hello.py" in shell["action"]["commands"][0]
    result = next(i for i in output if i["type"] == "shell_call_output" and i["call_id"] == shell["call_id"])
    assert result["output"][0]["stdout"].startswith("hello from hermes")
    assert result["output"][0]["outcome"] == {"type": "exit", "exit_code": 0}
    assert usage["input_tokens"] > usage["input_tokens_details"]["cached_tokens"] > 0 and usage["output_tokens"] > 0
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert meta["hermes_turn_exit"].startswith("text_response")
    deltas = [e for e in events if e["type"] == "response.output_text.delta"]
    assert len(deltas) > 5, "Hermes streams its answer token by token"


def test_turn2_recalls_without_tools() -> None:
    _, out = replay("turn2-resume")
    status, output, usage, *_ = out.outcome()
    assert status == "completed"
    assert "hello.py" in text(output) and "hello from hermes" in text(output)
    assert types(output) == ["message"] and usage["input_tokens"] > 0


def test_patch_edit_is_an_update_with_its_diff() -> None:
    _, out = replay("patch-edit")
    status, output, *_ = out.outcome()
    assert status == "completed"
    patch = next(i for i in output if i["type"] == "apply_patch_call")
    assert patch["operation"]["type"] == "update_file" and patch["operation"]["path"] == "hello.py"
    assert "-print('hello from hermes')" in patch["operation"]["diff"]
    assert "+print('hello again from hermes')" in patch["operation"]["diff"]
    assert any(i["type"] == "shell_call_output" and i["output"][0]["stdout"] == "hello again from hermes" for i in output)


def test_parallel_tools_and_function_calls_keep_their_order() -> None:
    _, out = replay("input-file-parallel-tools")
    status, output, *_ = out.outcome()
    assert status == "completed"
    seq = types(output)
    read = seq.index("function_call")
    assert output[read]["name"] == "read_file" and json.loads(output[read]["arguments"])["path"].endswith("data.csv")
    assert seq[read + 1] == "function_call_output" and "alpha,1" in output[read + 1]["output"]
    calls = {i["call_id"] for i in output if i["type"] in ("shell_call", "function_call")}
    outputs = {i["call_id"] for i in output if i["type"] in ("shell_call_output", "function_call_output")}
    assert calls == outputs, "every call has its output"
    assert "reasoning" in seq and "6" in text(output)


def test_provider_error_is_a_failed_response() -> None:
    events, out = replay("provider-error")
    status, output, usage, error, *_ = out.outcome()
    assert status == "failed" and usage is None
    assert error["code"] == "provider_error" and "no-such-model" in error["message"]
    assert not [e for e in events if e["type"] == "response.output_text.delta"], "the error is not an answer"


def test_cancel_reports_the_interrupted_command() -> None:
    _, out = replay("cancel-local-mock")
    status, output, _, error, *_ = out.outcome()
    assert status == "failed" and error["code"] == "cancelled"
    assert types(output) == ["shell_call", "shell_call_output"]
    assert output[0]["action"]["commands"] == ["sleep 300"]
    assert output[1]["output"][0]["outcome"] == {"type": "exit", "exit_code": 130}


def test_unanswered_call_and_mcp_names() -> None:
    """Synthetic: an MCP tool call (mapped by its Hermes name mcp_<server>_<tool>) and a call that never finished."""
    buf = io.StringIO()
    m = Mapper(Emitter(stream=buf), {"docs": "docs"})
    calls = [{"id": "c1", "function": {"name": "mcp_docs_search", "arguments": "{\"q\": \"x\"}"}},
             {"id": "c2", "function": {"name": "terminal", "arguments": "{\"command\": \"sleep 9\"}"}}]
    m.record("db", [{"role": "assistant", "tool_calls": json.dumps(calls)},
                    {"role": "tool", "tool_call_id": "c1", "content": "{\"hits\": 1}"}])
    m.record("end", {"stopReason": "end_turn", "usage": {"inputTokens": 5, "outputTokens": 1}, "cache_write": 2})
    out = TurnOutput()
    for line in buf.getvalue().splitlines():
        out.line(line)
    status, output, usage, *_ = out.outcome()
    assert status == "completed"
    assert output[0]["type"] == "mcp_call" and output[0]["server_label"] == "docs" and output[0]["name"] == "search"
    assert output[1] == {**output[1], "type": "shell_call", "status": "incomplete"}
    assert usage["input_tokens_details"]["cache_write_tokens"] == 2
