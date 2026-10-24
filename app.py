"""HarnessGateway: the OpenAI Responses API, backed by Claude Code running on DeepSeek.

    export DEEPSEEK_API_KEY=…
    uvicorn app:app --port 8000

`POST /v1/responses` takes a Responses request and returns a Responses response, streamed or not;
`GET /v1/responses/{id}` returns one made earlier. Any OpenAI SDK with `base_url=http://host:8000/v1`
works unchanged. Nothing of the schema is written here: the request is validated with the SDK's
`ResponseCreateParams`, the reply is the SDK's `Response`, the stream is its event classes.

Each request runs one turn of `claude -p` in a workspace directory; `previous_response_id` continues
that conversation (`claude --resume`) in the same workspace. Claude Code's stream-json events become
the schema's own output items:

    thinking            -> reasoning
    Bash                -> shell_call, then shell_call_output (stdout, exit code)
    Write / Edit        -> apply_patch_call (create_file / update_file), then apply_patch_call_output
    any other tool      -> function_call, then function_call_output
    text                -> message (output_text)

Environment: DEEPSEEK_API_KEY (required), DEEPSEEK_BASE_URL (default https://api.deepseek.com/anthropic),
HG_MODEL (default deepseek-chat; a request's model is "claude-code" or "claude-code/<deepseek model>"),
HG_DATA_DIR (the workspaces and Claude Code's home; default a temporary directory).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai.types import responses as R
from pydantic import TypeAdapter, ValidationError

PARAMS = TypeAdapter(R.ResponseCreateParams)
HARNESS = "claude-code"
DATA = Path(os.environ.get("HG_DATA_DIR") or tempfile.mkdtemp(prefix="harnessgateway-"))
PASS_THROUGH = ("PATH", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")


class Refused(Exception):
    """A request this harness cannot take; `body` is the API's error shape."""

    def __init__(self, message: str, param: str | None, code: str = "unsupported_parameter", status: int = 400):
        super().__init__(message)
        self.status = status
        self.body = {"error": {"type": "invalid_request_error", "code": code, "message": message, "param": param}}


@dataclass
class Turn:
    """One response: what was asked, the items Claude Code produced, how it ended."""
    id: str
    model: str
    prompt: str
    instructions: str
    previous: str | None
    metadata: dict[str, str]
    workspace: Path
    claude_session: str = ""                        # Claude Code's own session id, for --resume
    created: float = field(default_factory=time.time)
    items: list[Any] = field(default_factory=list)   # the SDK's output items, in order
    messages: list[R.ResponseOutputMessage] = field(default_factory=list)
    text: str = ""                                   # the message being written, if one is open
    status: str = "in_progress"
    error: str = ""
    usage: R.ResponseUsage | None = None

    @property
    def message_id(self) -> str:
        return f"msg_{self.id[5:25]}_{len(self.messages)}"

    def message(self, done: bool) -> R.ResponseOutputMessage:
        return R.ResponseOutputMessage(type="message", id=self.message_id, role="assistant", status="completed" if done else "in_progress",
                                       content=[R.ResponseOutputText(type="output_text", text=self.text, annotations=[])])

    def output(self) -> list[Any]:
        return [*self.items, *([self.message(True)] if self.text else [])]


TURNS: dict[str, Turn] = {}


# the request, as the SDK sends it, resolved into a prompt, instructions, files and a continuation
def parse(body: dict[str, Any]) -> dict[str, Any]:
    try:
        PARAMS.validate_python(body)
    except ValidationError as e:
        first = e.errors()[0]
        raise Refused(f"not a Responses request: {first['msg']}", ".".join(str(p) for p in first["loc"][1:]) or None, "invalid_value") from None
    harness, _, model = str(body.get("model", "")).partition("/")
    if harness != HARNESS:
        raise Refused(f"model is {HARNESS} or {HARNESS}/<deepseek model>, not {body.get('model')!r}", "model", "invalid_value")
    prompt, files = [], {}
    given = body.get("input", "")
    for item in [{"type": "input_text", "text": given}] if isinstance(given, str) else given:
        content = item.get("content") if item.get("type", "message") == "message" else [item]
        for part in [{"type": "input_text", "text": content}] if isinstance(content, str) else content:
            if part.get("type") == "input_text":
                prompt.append(part["text"])
            elif part.get("type") == "input_file":
                data = part.get("file_data", "")
                files[part["filename"]] = base64.b64decode(data.split(",", 1)[1]) if data.startswith("data:") else data.encode()
            else:
                raise Refused(f"{HARNESS} takes text and files as input, not {part.get('type')!r}", "input")
    if not "".join(prompt).strip():
        raise Refused("input carries no text", "input", "invalid_value")
    if body.get("tools"):
        raise Refused(f"{HARNESS} runs its own tools; none can be given to it", "tools")
    for knob in ("temperature", "top_p", "max_output_tokens"):
        if body.get(knob) is not None:
            raise Refused(f"{HARNESS} does not take {knob}: the CLI decides", knob)
    if (body.get("text") or {}).get("format", {}).get("type", "text") != "text":
        raise Refused(f"{HARNESS} returns text; it takes no output format", "text.format")
    previous = body.get("previous_response_id") or None
    if previous and previous not in TURNS:
        raise Refused(f"no response {previous}", "previous_response_id", "not_found", 404)
    return {"model": model or os.environ.get("HG_MODEL", "deepseek-chat"), "prompt": "\n".join(prompt), "files": files,
            "instructions": body.get("instructions") or "", "previous": previous, "metadata": dict(body.get("metadata") or {})}


def new_turn(ask: dict[str, Any]) -> Turn:
    prev = TURNS.get(ask["previous"] or "")
    workspace = prev.workspace if prev else DATA / "workspaces" / uuid.uuid4().hex[:12]
    workspace.mkdir(parents=True, exist_ok=True)
    for rel, data in ask["files"].items():
        target = (workspace / rel).resolve()
        if workspace.resolve() not in target.parents:
            raise Refused(f"a file's path is inside the workspace: {rel!r}", "input", "invalid_value")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    t = Turn(f"resp_{uuid.uuid4().hex}", ask["model"], ask["prompt"], ask["instructions"], ask["previous"], ask["metadata"], workspace,
             claude_session=prev.claude_session if prev else "")
    TURNS[t.id] = t
    return t


# Claude Code, one turn, its stream-json events as lines
def claude_lines(t: Turn) -> Iterator[str]:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise Refused("DEEPSEEK_API_KEY is not set on the server", None, "server_error", 500)
    home = DATA / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in PASS_THROUGH}
    env.update({"HOME": str(home), "ANTHROPIC_BASE_URL": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"),
                "ANTHROPIC_API_KEY": key, "ANTHROPIC_DEFAULT_HAIKU_MODEL": t.model, "ANTHROPIC_SMALL_FAST_MODEL": t.model,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "IS_SANDBOX": "1"})
    args = [shutil.which("claude") or "claude", "-p", t.prompt, "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions", "--model", t.model]
    if t.instructions:
        args += ["--append-system-prompt", t.instructions]
    if t.claude_session:
        args += ["--resume", t.claude_session]
    proc = subprocess.Popen(args, cwd=t.workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with proc.stdout, proc.stderr:
        yield from proc.stdout
        code = proc.wait()
        if code != 0 and t.status == "in_progress":
            t.error = f"claude exited {code}: {proc.stderr.read()[-500:].strip()}"


# the events become the schema's items: ("item", item), ("delta", text) and ("end", None)
def items(t: Turn, lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    kinds: dict[str, tuple[str, str]] = {}              # tool_use id -> (item kind, item id)
    n = 0
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            t.claude_session = ev.get("session_id", t.claude_session)
        elif ev.get("type") in ("assistant", "user"):
            for block in ev.get("message", {}).get("content", []):
                n += 1
                kind = block.get("type")
                if kind == "text" and ev["type"] == "assistant" and block["text"]:
                    t.text += block["text"]
                    yield "delta", block["text"]
                    continue
                if t.text:                                 # an item follows the text: the message closes
                    t.messages.append(t.message(True))
                    t.items.append(t.messages[-1])
                    t.text = ""
                    yield "close", None
                item = None
                if kind == "thinking" and block.get("thinking"):
                    item = R.ResponseReasoningItem(type="reasoning", id=f"rs_{n}", status="completed",
                                                   summary=[{"type": "summary_text", "text": block["thinking"]}])
                elif kind == "tool_use":
                    name, args, cid = block["name"], block.get("input") or {}, block["id"]
                    if name == "Bash":
                        item = R.ResponseFunctionShellToolCall(type="shell_call", id=f"sh_{n}", call_id=cid, status="completed",
                                                               action={"commands": [args.get("command", "")]})
                    elif name in ("Write", "Edit"):
                        op = ({"type": "create_file", "path": args.get("file_path", ""), "diff": args.get("content", "")} if name == "Write" else
                              {"type": "update_file", "path": args.get("file_path", ""), "diff": f"-{args.get('old_string', '')}\n+{args.get('new_string', '')}"})
                        item = R.ResponseApplyPatchToolCall(type="apply_patch_call", id=f"ap_{n}", call_id=cid, status="completed", operation=op)
                    else:
                        item = R.ResponseFunctionToolCall(type="function_call", id=f"fc_{n}", call_id=cid, name=name, arguments=json.dumps(args), status="completed")
                    kinds[cid] = (item.type, item.id)
                elif kind == "tool_result":
                    cid, content = block.get("tool_use_id", ""), block.get("content")
                    text = content if isinstance(content, str) else "".join(c.get("text", "") for c in content or [])
                    failed, call = bool(block.get("is_error")), kinds.get(cid, ("function_call", ""))
                    if call[0] == "shell_call":
                        item = R.ResponseFunctionShellToolCallOutput(type="shell_call_output", id=f"sho_{n}", call_id=cid, status="completed",
                                                                     output=[{"stdout": text, "stderr": "", "outcome": {"type": "exit", "exit_code": 1 if failed else 0}}])
                    elif call[0] == "apply_patch_call":
                        item = R.ResponseApplyPatchToolCallOutput(type="apply_patch_call_output", id=f"apo_{n}", call_id=cid, status="failed" if failed else "completed", output=text)
                    else:
                        item = R.ResponseFunctionToolCallOutputItem(type="function_call_output", id=f"fco_{n}", call_id=cid, output=text, status="incomplete" if failed else "completed")
                if item is not None:
                    t.items.append(item)
                    yield "item", item
        elif ev.get("type") == "result":
            t.claude_session = ev.get("session_id", t.claude_session)
            if ev.get("is_error") or ev.get("subtype") != "success":
                t.error = t.error or f"claude ended with {ev.get('subtype')}: {str(ev.get('result') or '')[:300]}"
            if usage := ev.get("usage"):
                inp, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
                t.usage = R.ResponseUsage(input_tokens=inp, output_tokens=out, total_tokens=inp + out,
                                          input_tokens_details={"cached_tokens": int(usage.get("cache_read_input_tokens", 0)),
                                                                "cache_write_tokens": int(usage.get("cache_creation_input_tokens", 0))},
                                          output_tokens_details={"reasoning_tokens": 0})
    t.status = "failed" if t.error else "completed"
    yield "end", None


# back on the wire: the SDK's Response, and its stream events
def response(t: Turn) -> R.Response:
    return R.Response(
        id=t.id, object="response", created_at=int(t.created), status=t.status, model=f"{HARNESS}/{t.model}", output=t.output(),
        parallel_tool_calls=False, tool_choice="none", tools=[], usage=t.usage,
        error=R.ResponseError(code="server_error", message=t.error) if t.status == "failed" else None, incomplete_details=None,
        instructions=t.instructions or None, previous_response_id=t.previous, store=True, truncation="disabled",
        text={"format": {"type": "text"}}, metadata={**t.metadata, "session_id": t.claude_session, "workspace": str(t.workspace)})


def events(t: Turn, relay: Iterator[tuple[str, Any]]) -> Iterator[R.ResponseStreamEvent]:
    n = -1

    def seq() -> int:
        nonlocal n
        n += 1
        return n

    index, message_open = 0, False

    def close_message():
        nonlocal index, message_open
        m = t.messages[-1] if t.messages and not t.text else t.message(True)
        part = m.content[0]
        yield R.ResponseTextDoneEvent(type="response.output_text.done", item_id=m.id, output_index=index, content_index=0, text=part.text, logprobs=[], sequence_number=seq())
        yield R.ResponseContentPartDoneEvent(type="response.content_part.done", item_id=m.id, output_index=index, content_index=0, part=part, sequence_number=seq())
        yield R.ResponseOutputItemDoneEvent(type="response.output_item.done", output_index=index, item=m, sequence_number=seq())
        index, message_open = index + 1, False

    yield R.ResponseCreatedEvent(type="response.created", response=response(t), sequence_number=seq())
    yield R.ResponseInProgressEvent(type="response.in_progress", response=response(t), sequence_number=seq())
    for kind, data in relay:
        if kind == "close":
            yield from close_message()
        elif kind == "item":
            opening = data.model_copy(update={"status": "in_progress"} | ({"arguments": ""} if data.type == "function_call" else {}))
            yield R.ResponseOutputItemAddedEvent(type="response.output_item.added", output_index=index, item=opening, sequence_number=seq())
            if data.type == "function_call":
                yield R.ResponseFunctionCallArgumentsDeltaEvent(type="response.function_call_arguments.delta", item_id=data.id, output_index=index, delta=data.arguments, sequence_number=seq())
                yield R.ResponseFunctionCallArgumentsDoneEvent(type="response.function_call_arguments.done", item_id=data.id, output_index=index, arguments=data.arguments, sequence_number=seq())
            yield R.ResponseOutputItemDoneEvent(type="response.output_item.done", output_index=index, item=data, sequence_number=seq())
            index += 1
        elif kind == "delta":
            if not message_open:
                message_open = True
                mid = t.message_id
                yield R.ResponseOutputItemAddedEvent(type="response.output_item.added", output_index=index, sequence_number=seq(),
                                                     item=R.ResponseOutputMessage(type="message", id=mid, role="assistant", status="in_progress", content=[]))
                yield R.ResponseContentPartAddedEvent(type="response.content_part.added", item_id=mid, output_index=index, content_index=0,
                                                      part=R.ResponseOutputText(type="output_text", text="", annotations=[]), sequence_number=seq())
            yield R.ResponseTextDeltaEvent(type="response.output_text.delta", item_id=t.message_id, output_index=index, content_index=0, delta=data, logprobs=[], sequence_number=seq())
        elif kind == "end":
            if message_open:
                yield from close_message()
            final = response(t)
            if t.status == "completed":
                yield R.ResponseCompletedEvent(type="response.completed", response=final, sequence_number=seq())
            else:
                yield R.ResponseFailedEvent(type="response.failed", response=final, sequence_number=seq())


# the endpoint
app = FastAPI(title="HarnessGateway", version="0.1.0", docs_url="/reference", redoc_url=None)


def refused(e: Refused) -> JSONResponse:
    return JSONResponse(e.body, status_code=e.status)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "harness": HARNESS, "claude": shutil.which("claude") is not None, "key": bool(os.environ.get("DEEPSEEK_API_KEY"))}


@app.post("/v1/responses")
async def create_response(request: Request) -> Any:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return refused(Refused("the body is a JSON object", None, "invalid_value"))
    try:
        t = new_turn(parse(body))
    except Refused as e:
        return refused(e)

    def relay() -> Iterator[tuple[str, Any]]:
        try:
            yield from items(t, claude_lines(t))
        except Refused as e:
            t.status, t.error = "failed", e.body["error"]["message"]
            yield "end", None

    if body.get("stream"):
        def sse() -> Iterator[str]:                      # a sync generator: Starlette iterates it in a thread
            for ev in events(t, relay()):               # warnings off: the SDK's event unions lag its item union
                yield f"event: {ev.type}\ndata: {ev.model_dump_json(warnings=False)}\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
    await asyncio.to_thread(lambda: all(True for _ in relay()))
    if t.text:
        t.messages.append(t.message(True))
        t.items.append(t.messages[-1])
        t.text = ""
    return JSONResponse(response(t).model_dump(mode="json"))


@app.get("/v1/responses/{rid}")
def get_response(rid: str) -> Any:
    t = TURNS.get(rid)
    if t is None:
        return refused(Refused(f"no response {rid}", "id", "not_found", 404))
    return JSONResponse(response(t).model_dump(mode="json"))
