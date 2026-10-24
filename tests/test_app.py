"""The endpoint, driven by the real openai client over ASGI. Without a key and the CLI, Claude Code
is stood in for by its recorded stream-json events; with both, one real turn runs too."""
import asyncio
import base64
import json
import os
import shutil
import sys
from pathlib import Path

import httpx
import openai
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as hg  # noqa: E402

# Claude Code's events for "Create hello.py that prints hello and run it", as recorded
RECORDED = [
    {"type": "system", "subtype": "init", "session_id": "ddc0b34c-d85d-42e2-905d-20d85543aad4"},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Write the file, run it, answer."}, {"type": "text", "text": "I'll create the file and run it."}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call_1", "name": "Write", "input": {"file_path": "hello.py", "content": 'print("hello")\n'}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "File created successfully at: hello.py"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call_2", "name": "Bash", "input": {"command": "python3 hello.py", "description": "Run it"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call_2", "content": "hello", "is_error": False}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call_3", "name": "Read", "input": {"file_path": "hello.py"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call_3", "content": [{"type": "text", "text": 'print("hello")'}]}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "ddc0b34c-d85d-42e2-905d-20d85543aad4",
     "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 100}},
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(hg, "DATA", tmp_path)
    monkeypatch.setattr(hg, "claude_lines", lambda t: iter(json.dumps(ev) + "\n" for ev in RECORDED))
    transport = httpx.ASGITransport(app=hg.app)
    return openai.AsyncOpenAI(base_url="http://hg.test/v1", api_key="hg_test", http_client=httpx.AsyncClient(transport=transport))


def test_a_turn_comes_back_as_the_schemas_items(client):
    async def go():
        r = await client.responses.create(model="claude-code", input="Create hello.py that prints hello and run it", metadata={"ticket": "HG-12"})
        assert r.status == "completed" and r.model == "claude-code/deepseek-chat" and r.metadata["ticket"] == "HG-12"
        assert [i.type for i in r.output] == ["reasoning", "message", "apply_patch_call", "apply_patch_call_output", "shell_call", "shell_call_output",
                                              "function_call", "function_call_output", "message"]
        assert r.output[0].summary[0].text == "Write the file, run it, answer."
        assert r.output[2].operation.type == "create_file" and r.output[2].operation.path == "hello.py"
        assert r.output[4].action.commands == ["python3 hello.py"] and r.output[5].output[0].stdout == "hello" and r.output[5].output[0].outcome.exit_code == 0
        assert r.output[6].name == "Read" and json.loads(r.output[6].arguments) == {"file_path": "hello.py"} and r.output[7].output == 'print("hello")'
        assert r.output_text == "I'll create the file and run it.done"
        assert r.usage.input_tokens == 120 and r.usage.total_tokens == 150 and r.usage.input_tokens_details.cached_tokens == 100
        assert r.metadata["session_id"] == "ddc0b34c-d85d-42e2-905d-20d85543aad4"
        assert (await client.responses.retrieve(r.id)).output_text == r.output_text
    asyncio.run(go())


def test_a_streamed_continuation_with_a_file(client, tmp_path):
    async def go():
        first = await client.responses.create(model="claude-code/deepseek-reasoner", input="start")
        stream = await client.responses.create(model="claude-code", previous_response_id=first.id, stream=True, input=[
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "again"},
                {"type": "input_file", "filename": "notes.txt", "file_data": "data:text/plain;base64," + base64.b64encode(b"milk\n").decode()}]}])
        events = [e async for e in stream]
        kinds = [e.type for e in events]
        assert kinds[:2] == ["response.created", "response.in_progress"] and kinds[-1] == "response.completed"
        assert [e.sequence_number for e in events] == list(range(len(events)))
        added = [e.item.type for e in events if e.type == "response.output_item.added"]
        assert added == ["reasoning", "message", "apply_patch_call", "apply_patch_call_output", "shell_call", "shell_call_output", "function_call", "function_call_output", "message"]
        text = "".join(e.delta for e in events if e.type == "response.output_text.delta")
        final = events[-1].response
        assert text == final.output_text == "I'll create the file and run it.done"
        assert final.previous_response_id == first.id and final.metadata["workspace"] == first.metadata["workspace"]
        assert final.model == "claude-code/deepseek-chat" and first.model == "claude-code/deepseek-reasoner"
        assert (Path(final.metadata["workspace"]) / "notes.txt").read_bytes() == b"milk\n"
    asyncio.run(go())


@pytest.mark.parametrize("kwargs, param, status", [
    ({"model": "gpt-4.1"}, "model", 400),
    ({"tools": [{"type": "function", "name": "run", "parameters": {}, "strict": False}]}, "tools", 400),
    ({"temperature": 0.2}, "temperature", 400),
    ({"input": [{"type": "input_file", "filename": "a", "file_data": 1}]}, None, 400),
    ({"previous_response_id": "resp_nope"}, "previous_response_id", 404),
])
def test_what_claude_code_cannot_take_is_refused(client, kwargs, param, status):
    async def go():
        body = {"model": "claude-code", "input": "x", **kwargs}
        with pytest.raises(openai.APIStatusError) as e:
            await client.responses.create(**body)
        assert e.value.status_code == status and (param is None or e.value.body["param"] == param)
    asyncio.run(go())


def test_a_failed_turn_is_a_failed_response(client, monkeypatch):
    failed = [*RECORDED[:1], {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom", "session_id": "s"}]
    monkeypatch.setattr(hg, "claude_lines", lambda t: iter(json.dumps(ev) + "\n" for ev in failed))

    async def go():
        r = await client.responses.create(model="claude-code", input="x")
        assert r.status == "failed" and "boom" in r.error.message and r.output == []
    asyncio.run(go())


@pytest.mark.skipif(not (os.environ.get("DEEPSEEK_API_KEY") and shutil.which("claude")), reason="needs DEEPSEEK_API_KEY and the claude CLI")
def test_a_real_turn_on_deepseek(tmp_path, monkeypatch):
    monkeypatch.setattr(hg, "DATA", tmp_path)
    transport = httpx.ASGITransport(app=hg.app)
    client = openai.AsyncOpenAI(base_url="http://hg.test/v1", api_key="hg_test", http_client=httpx.AsyncClient(transport=transport, timeout=300))

    async def go():
        r = await client.responses.create(model="claude-code", input="Create hello.py that prints hello and run it, then reply with the one word: done")
        assert r.status == "completed", r.error
        assert "shell_call" in [i.type for i in r.output] and "done" in r.output_text.lower()
        assert (Path(r.metadata["workspace"]) / "hello.py").is_file()
        again = await client.responses.create(model="claude-code", previous_response_id=r.id, input="What file did you just create? Answer with its name only.")
        assert again.status == "completed" and "hello.py" in again.output_text
    asyncio.run(go())
