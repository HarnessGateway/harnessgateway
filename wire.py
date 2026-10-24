#!/usr/bin/env python3
"""The Responses wire end to end, in one file, around one template package: `foobarness/`. The
schema is the OpenAI SDK's own: the request is validated with its `ResponseCreateParams`, every
item a package reports is validated as its `ResponseOutputItem`, the response is its `Response`
model, the stream is its event classes, and the client is the real `openai` client pointed at
this file's endpoint. Nothing of the schema is written here.

    pip install -e .[dev]
    uvicorn app:app --port 8000      # then any OpenAI SDK, base_url=http://127.0.0.1:8000/v1
    pytest

What it shows, in order:

1. A client's request, as the `openai` client sends it. `parse_request` resolves it into the fixed
   subset a harness gets (prompt, files, instructions, continuation, model, reasoning effort, MCP
   tools) and refuses, with the API's error shape, anything the package's manifest does not accept.
2. `stage` writes that subset where the package reads it: the files into the workspace, the prompt
   and `request.json` into the run directory. This is the only thing a package sees of the request.
3. `run_turn` is the template: the common runner every package shares. It runs the adapter's
   configure.sh (seeds become the CLI's config), then run.sh (the CLI, its native events relayed
   live) piped through items.py (the package's filter: native events become Responses items).
4. `response_object` and `events` put the items back on the wire, as the SDK's `Response` and its
   stream events. `app.py` serves them at POST /v1/responses as a FastAPI app; the tests
   read them back with the `openai` client.

Nothing here is Docker, the gateway or the store: the turn runs as a process in a temporary
directory, with a fake provider URL and token, so the mapping is the whole subject.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from openai.types import responses as R
from pydantic import TypeAdapter, ValidationError

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / "foobarness"
PARAMS = TypeAdapter(R.ResponseCreateParams)         # the request schema, the SDK's
ITEM = TypeAdapter(R.ResponseOutputItem)             # the output item schema, the SDK's


class Refused(Exception):
    """A request the harness cannot take; `body` is the API's error shape, sent as HTTP 400."""

    def __init__(self, message: str, param: str, code: str = "unsupported_parameter"):
        super().__init__(message)
        self.body = {"error": {"type": "invalid_request_error", "code": code, "message": message, "param": param}}


# 1. the request, as a client sends it, resolved into what a harness gets
def parse_request(body: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        PARAMS.validate_python(body)
    except ValidationError as e:
        raise Refused(f"not a Responses request: {e.errors()[0]['msg']}", ".".join(str(p) for p in e.errors()[0]["loc"][1:]), "invalid_value") from None
    accepts = manifest["accepts"]
    parts = str(body.get("model", "")).split("/", 2)
    harness, provider, model = (parts[0], "", "") if len(parts) == 1 else (parts[0], parts[1], parts[2])
    if not harness:
        raise Refused("model is a harness name, or harness/provider/model", "model", "invalid_value")
    prompt, files = [], {}
    given = body.get("input", "")
    for item in [{"type": "input_text", "text": given}] if isinstance(given, str) else given:
        content = item.get("content", []) if item.get("type", "message") == "message" and not isinstance(item.get("content"), str) else [item]
        if isinstance(item.get("content"), str):
            content = [{"type": "input_text", "text": item["content"]}]
        for part in content:
            if part.get("type") == "input_text":
                prompt.append(part["text"])
            elif part.get("type") == "input_file":
                if not accepts["input_file"]:
                    raise Refused(f"{harness} takes no files", "input")
                data = part["file_data"]
                files[part["filename"]] = base64.b64decode(data.split(",", 1)[1]) if data.startswith("data:") else data.encode()
    if not "".join(prompt).strip():
        raise Refused("input carries no text", "input", "invalid_value")
    if body.get("instructions") and not accepts["instructions"]:
        raise Refused(f"{harness} takes no instructions", "instructions")
    if body.get("previous_response_id") and not accepts["previous_response_id"]:
        raise Refused(f"{harness} cannot continue a session", "previous_response_id")
    mcp = []
    for tool in body.get("tools") or []:
        if tool.get("type") != "mcp" or "mcp" not in accepts["tools"]:
            raise Refused(f"{harness} runs its own tools; a tool of type {tool.get('type')!r} cannot be given to it", "tools")
        mcp.append({"name": tool["server_label"], "url": tool["server_url"]})
    effort = (body.get("reasoning") or {}).get("effort")
    if effort and effort not in accepts["reasoning_effort"]:
        raise Refused(f"reasoning.effort is one of {', '.join(accepts['reasoning_effort'])}", "reasoning.effort", "invalid_value")
    for knob in ("temperature", "top_p", "max_output_tokens"):
        if body.get(knob) is not None and not accepts["sampling"]:
            raise Refused(f"{harness} does not take {knob}: its CLI decides", knob)
    if (body.get("text") or {}).get("format", {}).get("type", "text") != "text" and not accepts["text_format"]:
        raise Refused(f"{harness} returns text; it takes no output format", "text.format")
    return {"harness": harness, "provider": provider or manifest["defaults"]["provider"], "model": model or manifest["defaults"]["model"],
            "prompt": "\n".join(prompt), "files": files, "instructions": body.get("instructions") or "",
            "resume": bool(body.get("previous_response_id")), "reasoning_effort": effort or "medium", "mcp_servers": mcp,
            "metadata": body.get("metadata") or {}, "store": body.get("store", True) is not False}


# 2. staged: what the package finds on disk
def stage(session: Session, ask: dict[str, Any]) -> dict[str, Any]:
    for rel, data in ask["files"].items():
        (session.workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (session.workspace / rel).write_bytes(data)
    request = {"turn": session.turn + 1, "resume": ask["resume"], "model": ask["model"], "instructions": ask["instructions"],
               "reasoning_effort": ask["reasoning_effort"], "mcp_servers": ask["mcp_servers"], "files": sorted(ask["files"])}
    (session.run / "prompt").write_text(ask["prompt"])
    (session.run / "request.json").write_text(json.dumps(request, indent=1))
    return request


@dataclass
class Session:
    root: Path
    id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex}")
    turn: int = 0

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def home(self) -> Path:
        return self.root / "home"


@dataclass
class Turn:
    """A turn as the platform keeps it: the items the package reported, typed, in order; the
    message assembled from the deltas; how it ended."""
    id: str
    session: str
    harness: str
    provider: str
    model: str
    turn: int
    previous: str | None
    instructions: str
    metadata: dict[str, str]
    store: bool
    created: float = field(default_factory=time.time)
    items: list[Any] = field(default_factory=list)   # ResponseOutputItem instances
    text: str = ""
    status: str = "in_progress"
    exit_code: int | None = None
    error: str = ""

    @property
    def message_id(self) -> str:
        return f"msg_{self.id[5:29]}"

    def message(self, done: bool) -> R.ResponseOutputMessage:
        return R.ResponseOutputMessage(type="message", id=self.message_id, role="assistant", status="completed" if done else "in_progress",
                                       content=[R.ResponseOutputText(type="output_text", text=self.text, annotations=[])])


# 3. the template: install once, then configure -> run | items, relayed as it happens
def install(tools: Path) -> None:
    env = {**os.environ, "HG_TOOLS": str(tools), "HG_HARNESS_DIR": str(PACKAGE)}
    subprocess.run(["bash", str(PACKAGE / "install.sh")], env=env, check=True, capture_output=True)


def run_turn(session: Session, turn: Turn, ask: dict[str, Any], tools: Path) -> Iterator[tuple[str, Any]]:
    """Yields ("item", item) and ("delta", text) as the filter reports them, then ("end", code).
    Every item is validated against the schema; one that does not fit fails the turn."""
    for d in (session.workspace, session.run, session.home):
        d.mkdir(parents=True, exist_ok=True)
    stage(session, ask)
    env = {"PATH": f"{tools}/bin:{os.environ['PATH']}", "HOME": str(session.home), "HG_WORKSPACE": str(session.workspace),
           "HG_DIR": str(session.run), "HG_REQUEST": str(session.run / "request.json"), "HG_HARNESS_DIR": str(PACKAGE),
           "HG_TOOLS": str(tools), "HG_SESSION": session.id, "HG_RESPONSE": turn.id, "HG_TURN": str(turn.turn),
           "HG_MODEL": ask["model"], "HG_INSTRUCTIONS": ask["instructions"], "HG_PROVIDER_URL": "http://hg-provider:8080",
           "FOOBAR_API_KEY": "sk-hg-session-token", "FOOBAR_BASE_URL": "http://hg-provider:8080"}
    subprocess.run(["bash", str(PACKAGE / "adapter" / "configure.sh")], env=env, check=True)
    native = session.run / "native.jsonl"            # the CLI's own events, kept whole as the capture
    pipeline = f'bash "$HG_HARNESS_DIR/adapter/run.sh" | tee "{native}" | python3 "$HG_HARNESS_DIR/adapter/items.py"'
    proc = subprocess.Popen(["bash", "-o", "pipefail", "-c", pipeline], env=env, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        report = json.loads(line)
        if "item" in report:
            try:
                item = ITEM.validate_python(report["item"])
            except ValidationError as e:
                proc.kill()
                turn.error = f"the package reported an item that is not a Responses output item: {e.errors()[0]['msg']}"
                break
            turn.items.append(item)
            yield "item", item
        else:
            turn.text += report["delta"]
            yield "delta", report["delta"]
    code = proc.wait()
    turn.exit_code = code
    turn.status = "failed" if turn.error or code != 0 else "completed"
    turn.error = turn.error or ("" if code == 0 else f"the package exited {code}")
    session.turn = turn.turn
    yield "end", code


# 4. back on the wire: the SDK's Response, and its stream events
def response_object(t: Turn) -> R.Response:
    done = t.status != "in_progress"
    return R.Response(
        id=t.id, object="response", created_at=int(t.created), status=t.status, model=f"{t.harness}/{t.provider}/{t.model}",
        output=[*t.items, *([t.message(True)] if done or t.text else [])], parallel_tool_calls=False, tool_choice="none", tools=[],
        error=R.ResponseError(code="server_error", message=t.error) if t.status == "failed" else None,
        incomplete_details=None, instructions=t.instructions or None, previous_response_id=t.previous, store=t.store,
        truncation="disabled", text={"format": {"type": "text"}}, reasoning=None, usage=None,
        metadata={**t.metadata, "session_id": t.session, "harness": t.harness, "turn": str(t.turn)})


def events(t: Turn, relay: Iterator[tuple[str, Any]]) -> Iterator[R.ResponseStreamEvent]:
    """The stream: each item added and done as it arrives, with the argument events the schema has
    for that item; the message opened on its first delta, closed before the next item or at the
    end; the final event with the whole response."""
    n = -1

    def seq() -> int:
        nonlocal n
        n += 1
        return n

    index, message_open = 0, False

    def close_message():
        nonlocal index, message_open
        part = R.ResponseOutputText(type="output_text", text=t.text, annotations=[])
        yield R.ResponseTextDoneEvent(type="response.output_text.done", item_id=t.message_id, output_index=index, content_index=0, text=t.text, logprobs=[], sequence_number=seq())
        yield R.ResponseContentPartDoneEvent(type="response.content_part.done", item_id=t.message_id, output_index=index, content_index=0, part=part, sequence_number=seq())
        yield R.ResponseOutputItemDoneEvent(type="response.output_item.done", output_index=index, item=t.message(True), sequence_number=seq())
        index, message_open = index + 1, False

    yield R.ResponseCreatedEvent(type="response.created", response=response_object(t), sequence_number=seq())
    yield R.ResponseInProgressEvent(type="response.in_progress", response=response_object(t), sequence_number=seq())
    for kind, data in relay:
        if kind == "item":
            if message_open:
                yield from close_message()
            opening = data.model_copy(update={"status": "in_progress"} | ({"arguments": ""} if hasattr(data, "arguments") else {}))
            yield R.ResponseOutputItemAddedEvent(type="response.output_item.added", output_index=index, item=opening, sequence_number=seq())
            if data.type == "function_call":
                yield R.ResponseFunctionCallArgumentsDeltaEvent(type="response.function_call_arguments.delta", item_id=data.id, output_index=index, delta=data.arguments, sequence_number=seq())
                yield R.ResponseFunctionCallArgumentsDoneEvent(type="response.function_call_arguments.done", item_id=data.id, output_index=index, arguments=data.arguments, sequence_number=seq())
            elif data.type == "mcp_call":
                yield R.ResponseMcpCallArgumentsDeltaEvent(type="response.mcp_call_arguments.delta", item_id=data.id, output_index=index, delta=data.arguments, sequence_number=seq())
                yield R.ResponseMcpCallArgumentsDoneEvent(type="response.mcp_call_arguments.done", item_id=data.id, output_index=index, arguments=data.arguments, sequence_number=seq())
            yield R.ResponseOutputItemDoneEvent(type="response.output_item.done", output_index=index, item=data, sequence_number=seq())
            index += 1
        elif kind == "delta":
            if not message_open:
                message_open = True
                yield R.ResponseOutputItemAddedEvent(type="response.output_item.added", output_index=index, item=t.message(False).model_copy(update={"content": []}), sequence_number=seq())
                yield R.ResponseContentPartAddedEvent(type="response.content_part.added", item_id=t.message_id, output_index=index, content_index=0,
                                                      part=R.ResponseOutputText(type="output_text", text="", annotations=[]), sequence_number=seq())
            yield R.ResponseTextDeltaEvent(type="response.output_text.delta", item_id=t.message_id, output_index=index, content_index=0, delta=data, logprobs=[], sequence_number=seq())
        elif kind == "end":
            if message_open:
                yield from close_message()
            final = response_object(t)
            if t.status == "completed":
                yield R.ResponseCompletedEvent(type="response.completed", response=final, sequence_number=seq())
            else:
                yield R.ResponseFailedEvent(type="response.failed", response=final, sequence_number=seq())


# the endpoint: POST /v1/responses, the way the platform serves it
class Platform:
    def __init__(self, root: Path):
        self.manifest = json.loads((PACKAGE / "harness.json").read_text())
        self.root, self.tools = root, root / "tools"
        self.turns: dict[str, tuple[Session, Turn]] = {}
        install(self.tools)

    def create(self, body: dict[str, Any]) -> tuple[Turn, Iterator[tuple[str, Any]]]:
        ask = parse_request(body, self.manifest)
        prev = self.turns.get(body.get("previous_response_id") or "")
        if body.get("previous_response_id") and not prev:
            raise Refused(f"no response {body['previous_response_id']}", "previous_response_id", "not_found")
        session = prev[0] if prev else Session(self.root / "sessions" / uuid.uuid4().hex[:8])
        t = Turn(f"resp_{uuid.uuid4().hex}", session.id, ask["harness"], ask["provider"], ask["model"], session.turn + 1,
                 body.get("previous_response_id"), ask["instructions"], ask["metadata"], ask["store"])
        self.turns[t.id] = (session, t)
        return t, run_turn(session, t, ask, self.tools)
