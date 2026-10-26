"""One Codex turn: the Responses request -> config.toml + `codex exec` arguments -> Codex's JSONL -> map.py.

  input text                -> the prompt (positional argument)
  input_image (file_id or data: URL) -> `-i FILE` (images Codex can see); http(s) image URLs are named in the prompt
  input_file (file_id or inline) -> copied into the workspace, their paths named in the prompt
  instructions              -> developer_instructions in config.toml (Codex puts it first in its developer message);
                               on a resumed thread new or changed instructions are prepended to the prompt instead
                               (a resumed thread keeps the developer instructions it started with)
  previous_response_id      -> `codex exec resume <thread id>` (thread id kept in $HG_STATE by map.py)
  model                     -> -m $HG_MODEL and model = ... in config.toml
  reasoning.effort          -> model_reasoning_effort      reasoning.summary -> model_reasoning_summary
  text.verbosity            -> model_verbosity             text.format json_schema -> --output-schema FILE
  tools type mcp            -> [mcp_servers.<label>] (streamable HTTP url, bearer token from authorization, headers,
                               allowed_tools -> enabled_tools)
  tools type web_search*    -> web_search = "live" (otherwise "disabled": the sandbox has no direct web access, and
                               Codex's web search runs on the provider side)
  store: false              -> --ephemeral
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import contextlib

from hgkit import Emitter, Request, State, input_files  # noqa: E402
from map import THREAD_KEY, CodexMapper  # noqa: E402

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
SUMMARIES = ("auto", "concise", "detailed", "none")
VERBOSITY = ("low", "medium", "high")
TMP = Path("/tmp/hg-codex")
INSTRUCTIONS_KEY = "codex_instructions"     # the instructions the thread last received


def toml_str(value: str) -> str:
    """A TOML basic string. JSON escapes are TOML escapes, except astral characters (kept raw here) and DEL."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else toml_str(value)


def env_name(label: str) -> str:
    return "HG_MCP_" + re.sub(r"[^A-Z0-9]", "_", label.upper())[:48]


def build_config(req: Request, model: str, env: dict[str, str]) -> str:
    body = req.body
    reasoning = body.get("reasoning") or {}
    text = body.get("text") or {}
    lines = [
        'model_provider = "hg"',
        f"model = {toml_str(model)}",
        'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"',
        "check_for_update_on_startup = false",
        "hide_agent_reasoning = false",
    ]
    catalog = Path(os.environ.get("HG_TOOLS", "/opt/hg/tools")) / "codex-models.json"
    if catalog.is_file():
        lines.append(f"model_catalog_json = {toml_str(str(catalog))}")
    effort = reasoning.get("effort")
    if effort in EFFORTS:
        lines.append(f"model_reasoning_effort = {toml_str(effort)}")
    summary = reasoning.get("summary") or reasoning.get("generate_summary") or "auto"
    if summary in SUMMARIES:
        lines.append(f"model_reasoning_summary = {toml_str(summary)}")
    if text.get("verbosity") in VERBOSITY:
        lines.append(f"model_verbosity = {toml_str(text['verbosity'])}")
    if req.instructions.strip():
        lines.append(f"developer_instructions = {toml_str(req.instructions.strip())}")
    tools = [t for t in body.get("tools") or [] if isinstance(t, dict)]
    web = any(str(t.get("type", "")).startswith("web_search") for t in tools)
    lines.append(f'web_search = "{"live" if web else "disabled"}"')
    lines += [
        "",
        "[model_providers.hg]",
        'name = "HarnessGateway"',
        f"base_url = {toml_str(os.environ.get('HG_PROVIDER_URL', '').rstrip('/') + '/v1')}",
        'env_key = "HG_CODEX_API_KEY"',
        'wire_api = "responses"',
        "request_max_retries = 2",
        "stream_max_retries = 2",
        "",
        "[analytics]",
        "enabled = false",
        "",
        "[feedback]",
        "enabled = false",
        "",
        "[features]",
        "unbounded_connection_retries = false",
        "plugins = false",
        "apps = false",
        "remote_plugin = false",
        "daemon_auto_start = false",
        "in_app_updates = false",
    ]
    env["HG_CODEX_API_KEY"] = os.environ.get("HG_PROVIDER_TOKEN", "")
    for t in req.mcp_servers:
        label, url = str(t.get("server_label") or ""), t.get("server_url")
        if not label or not url:
            continue
        lines += ["", f"[mcp_servers.{toml_key(label)}]", f"url = {toml_str(str(url))}", "startup_timeout_sec = 30"]
        if t.get("authorization"):
            env[env_name(label)] = str(t["authorization"]).removeprefix("Bearer ").strip()
            lines.append(f"bearer_token_env_var = {toml_str(env_name(label))}")
        headers = t.get("headers") if isinstance(t.get("headers"), dict) else {}
        if headers:
            pairs = ", ".join(f"{toml_str(str(k))} = {toml_str(str(v))}" for k, v in headers.items())
            lines.append(f"http_headers = {{ {pairs} }}")
        allowed = t.get("allowed_tools")
        names = allowed if isinstance(allowed, list) else (allowed or {}).get("tool_names") if isinstance(allowed, dict) else None
        if names:
            lines.append("enabled_tools = [" + ", ".join(toml_str(str(n)) for n in names) + "]")
    return "\n".join(lines) + "\n"


def collect_inputs(req: Request) -> tuple[list[str], list[str]]:
    """(image paths for -i, prompt notes). Non-image files are copied into the workspace."""
    images: list[str] = []
    notes: list[str] = []
    workspace = Path(os.environ.get("HG_WORKSPACE", "/workspace"))
    (TMP / "images").mkdir(parents=True, exist_ok=True)

    def place(name: str, src: Path | None = None, data: bytes | None = None, image: bool = False) -> None:
        name = os.path.basename(name) or "input"
        if image or name.lower().endswith(IMAGE_EXT):
            dest = TMP / "images" / f"{len(images)}-{name}"
            images.append(str(dest))
        else:
            dest = workspace / name
            notes.append(f"./{name}")
        if src is not None:
            shutil.copyfile(src, dest)
        else:
            dest.write_bytes(data or b"")

    index = input_files()
    parts = req._parts()
    for p in parts:
        ptype = p.get("type")
        if ptype not in ("input_file", "input_image"):
            continue
        fid = p.get("file_id")
        if isinstance(fid, str) and fid in index:
            place(index[fid]["filename"], src=Path(index[fid]["path"]), image=ptype == "input_image")
        elif p.get("file_data") or str(p.get("image_url", "")).startswith("data:"):
            data = p.get("file_data") or p.get("image_url")
            raw = base64.b64decode(data.split(",", 1)[1]) if data.startswith("data:") else base64.b64decode(data)
            ext = ".png"
            if data.startswith("data:image/"):
                ext = "." + data[len("data:image/"):].split(";", 1)[0].replace("jpeg", "jpg")
            place(p.get("filename") or f"input-{len(images) + len(notes) + 1}{ext if ptype == 'input_image' else ''}",
                  data=raw, image=ptype == "input_image")
        elif p.get("file_url") or p.get("image_url"):
            notes.append(str(p.get("file_url") or p.get("image_url")))
    return images, notes


def main() -> int:
    req = Request.load()
    state = State()
    out = Emitter()
    model = os.environ.get("HG_MODEL") or "gpt-5.4-mini"
    if os.environ.get("HG_PROVIDER_API") not in ("openai", None, ""):
        out.failed("unsupported_provider", "Codex speaks the OpenAI Responses API: use an openai-shaped provider")
        return 0
    TMP.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    codex_home = Path(os.environ.get("HOME", "/home/hg")) / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    env["CODEX_HOME"] = str(codex_home)
    (codex_home / "config.toml").write_text(build_config(req, model, env))
    try:
        images, notes = collect_inputs(req)
    except (OSError, ValueError) as e:
        out.failed("invalid_input_file", f"an input file could not be read: {e}")
        return 0

    prompt = req.prompt
    if notes:
        prompt = (prompt + "\n\n" if prompt else "") + "Attached files: " + ", ".join(notes)
    if not prompt:
        prompt = "See the attached image(s)." if images else ""
    if not prompt:
        out.failed("invalid_prompt", "the request has no input text")
        return 0

    args = ["--json", "--skip-git-repo-check", "-m", model]
    fmt = (req.body.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema" and isinstance(fmt.get("schema"), dict):
        schema = TMP / "schema.json"
        schema.write_text(json.dumps(fmt["schema"]))
        args += ["--output-schema", str(schema)]
    if req.body.get("store") is False:
        args.append("--ephemeral")
    for img in images:
        args += ["-i", img]
    thread = state.get(THREAD_KEY) if req.body.get("previous_response_id") else ""
    instructions = req.instructions.strip()
    if thread and instructions and instructions != state.get(INSTRUCTIONS_KEY):
        # A resumed thread keeps the developer_instructions it started with, so new or changed instructions ride
        # along with this turn's prompt.
        prompt = f"<developer_instructions>\n{instructions}\n</developer_instructions>\n\n{prompt}"
    if instructions or not thread:
        state.set(INSTRUCTIONS_KEY, instructions)
    cmd = ["codex", "exec", "resume", *args, thread, "--", prompt] if thread else ["codex", "exec", *args, "--", prompt]
    out.log(f"hg-codex: running codex exec{' resume ' + thread if thread else ''} model={model} images={len(images)}")

    mapper = CodexMapper(out=out, state=state)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            cwd=os.environ.get("HG_WORKSPACE", "/workspace"), text=True, bufsize=1,
                            start_new_session=False)

    def stop(signum: int, _frame: Any) -> None:     # cancel: let Codex go, then leave without a terminal event
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signum)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    tail: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            tail.append(line.rstrip("\n"))
            del tail[:-40]

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.strip():
            out.log("codex> " + line.rstrip("\n")[:65536])     # the raw stream, for the turn's log (never delivered)
        mapper.feed(line)
    code = proc.wait()
    t.join(timeout=5)
    noise = ("WARNING: proceeding", "Reading additional input")
    mapper.finish(code, "\n".join(x for x in tail if not x.startswith(noise)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
