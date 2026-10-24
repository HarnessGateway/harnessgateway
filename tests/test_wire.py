"""The wire, driven by the real openai client: a request becomes what the harness gets, the
harness's items come back typed, the stream is the standard events in order, and what the
manifest does not accept is refused with the API's error."""
import base64
import json
import sys
import tempfile
from pathlib import Path

import openai
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wire  # noqa: E402


@pytest.fixture(scope="module")
def platform():
    root = Path(tempfile.mkdtemp(prefix="foobarness-test-"))
    return wire.Platform(root)


@pytest.fixture(scope="module")
def client(platform):
    return openai.OpenAI(base_url=wire.serve(platform), api_key="hg_test")


def test_a_request_becomes_what_the_harness_gets_and_its_items_come_back_typed(platform, client):
    r = client.responses.create(model="foobarness", instructions="Answer in one sentence.", input="Create hello.py that prints hello and run it",
                                tools=[{"type": "mcp", "server_label": "search", "server_url": "https://mcp.example.com/search"}],
                                reasoning={"effort": "high"}, metadata={"ticket": "HG-12"})
    session = platform.turns[r.id][0]
    staged = json.loads((session.run / "request.json").read_text())
    assert staged == {"turn": 1, "resume": False, "model": "foobar-1", "instructions": "Answer in one sentence.", "reasoning_effort": "high",
                      "mcp_servers": [{"name": "search", "url": "https://mcp.example.com/search"}], "files": []}
    config = json.loads((session.home / ".foobar" / "config.json").read_text())
    assert [s["name"] for s in config["mcp"]] == ["docs", "search"] and config["skills"] == ["two-sentences"]
    assert r.status == "completed" and r.model == "foobarness/none/foobar-1" and r.metadata["ticket"] == "HG-12"
    assert [i.type for i in r.output] == ["reasoning", "apply_patch_call", "apply_patch_call_output", "shell_call", "shell_call_output", "mcp_call", "mcp_call", "message"]
    assert r.output[1].operation.type == "create_file" and r.output[1].operation.path == "hello.py"
    assert r.output[3].action.commands == ["python3 hello.py"] and r.output[4].output[0].stdout == "hello\n" and r.output[4].output[0].outcome.exit_code == 0
    assert r.output[5].server_label == "docs" and r.output[6].server_label == "search"
    assert r.output_text == "Chase, created hello.py and ran it; it printed 'hello'."
    assert (session.workspace / "hello.py").read_text() == 'print("hello")\n'


def test_a_streamed_continuation_with_a_file(platform, client):
    first = client.responses.create(model="foobarness", input="Create hello.py that prints hello and run it")
    stream = client.responses.create(model="foobarness", previous_response_id=first.id, stream=True, input=[
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Run it again and tell me what files are here."},
            {"type": "input_file", "filename": "notes.txt", "file_data": "data:text/plain;base64," + base64.b64encode(b"milk\n").decode()}]}])
    events = list(stream)
    kinds = [e.type for e in events]
    assert kinds[:2] == ["response.created", "response.in_progress"] and kinds[-1] == "response.completed"
    assert [e.sequence_number for e in events] == list(range(len(events)))
    added = [e.item.type for e in events if e.type == "response.output_item.added"]
    assert added == ["reasoning", "shell_call", "shell_call_output", "mcp_call", "message"]
    text = "".join(e.delta for e in events if e.type == "response.output_text.delta")
    final = events[-1].response
    assert text == final.output_text and "notes.txt" in text and final.previous_response_id == first.id
    assert final.metadata["turn"] == "2" and final.metadata["session_id"] == first.metadata["session_id"]


@pytest.mark.parametrize("kwargs, param", [
    ({"tools": [{"type": "function", "name": "run", "parameters": {}, "strict": False}]}, "tools"),
    ({"temperature": 0.2}, "temperature"),
    ({"reasoning": {"effort": "max"}}, "reasoning.effort"),
])
def test_what_the_manifest_does_not_accept_is_refused(client, kwargs, param):
    with pytest.raises(openai.BadRequestError) as e:
        client.responses.create(model="foobarness", input="x", **kwargs)
    assert e.value.status_code == 400 and e.value.body["param"] == param


def test_a_malformed_request_is_refused_by_the_schema(client):
    with pytest.raises(openai.BadRequestError) as e:
        client.responses.create(model="foobarness", input=[{"type": "input_file", "filename": "a", "file_data": 1}])
    assert e.value.body["code"] == "invalid_value"
