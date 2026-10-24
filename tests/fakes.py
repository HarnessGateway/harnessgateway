"""Stand-ins: a test package as a tar, and a runtime that runs the turn as a process on this machine."""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

from harnessgateway.execution import Execution, Spec
from harnessgateway.runtimes.process import ProcessExecution, host_env

HARNESS_JSON = {"contract": 7, "description": "a test harness", "cli": {"name": "echo-cli", "version": "1"},
                "defaults": {"provider": "prov", "model": "m1"},
                "provider_env": {"key": "FAKE_KEY", "url": "FAKE_URL"}, "interrupt": "SIGUSR1",
                "install": {"egress": ["registry.example"], "timeout_s": 60},
                "run": {"egress": ["git.example"], "timeout_s": 60, "stop_timeout_s": 2},
                "accepts": ["input", "instructions", "previous_response_id"]}
# request.sh: reads the request, prints a text delta, a shell_call item and a message item as
# Responses stream events, plus plain log lines; writes the prompt into the workspace.
REQUEST_SH = r"""#!/usr/bin/env bash
set -eu
prompt=$(jq -r 'if (.input|type)=="string" then .input else [.input[]|(.content // empty)|if type=="string" then . else (.[]|select(.type=="input_text")|.text) end]|join("\n") end' "$HG_REQUEST")
ins=$(jq -r '.instructions // ""' "$HG_REQUEST")
echo "log: turn=$HG_TURN model=$HG_MODEL key=${FAKE_KEY:-} url=${FAKE_URL:-} previous=${HG_PREVIOUS:-}"
[ -f "$HOME/home.txt" ] && echo "log: home=$(cat "$HOME/home.txt")"
[ -f README.md ] && echo "log: workspace=$(cat README.md)"
[ -d "$HG_FILES" ] && for f in "$HG_FILES"/*; do [ -f "$f" ] && echo "log: file=$(basename "$f"):$(cat "$f")"; done
echo "$prompt" > out.txt
echo "$HG_TURN" > "$HG_STATE/turns"
printf '{"type":"response.output_item.added","output_index":0,"item":{"type":"shell_call","id":"sh_%s","call_id":"call_1","status":"in_progress","action":{"commands":["echo hi"]}}}\n' "$HG_TURN"
printf '{"type":"response.output_item.done","output_index":0,"item":{"type":"shell_call","id":"sh_%s","call_id":"call_1","status":"completed","action":{"commands":["echo hi"]}}}\n' "$HG_TURN"
printf '{"type":"response.output_text.delta","item_id":"msg_%s","output_index":1,"content_index":0,"delta":"prompt=%s "}\n' "$HG_TURN" "$(printf '%s' "$prompt" | tr -d '\n"\\')"
printf '{"type":"response.output_text.delta","item_id":"msg_%s","output_index":1,"content_index":0,"delta":"ins=%s"}\n' "$HG_TURN" "$(printf '%s' "$ins" | tr -d '\n"\\')"
printf '{"type":"response.output_item.done","output_index":1,"item":{"type":"message","id":"msg_%s","role":"assistant","status":"completed","content":[{"type":"output_text","text":"prompt=%s ins=%s","annotations":[]}]}}\n' "$HG_TURN" "$(printf '%s' "$prompt" | tr -d '\n"\\')" "$(printf '%s' "$ins" | tr -d '\n"\\')"
printf '{"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":5}}}\n'
echo "stderr line" >&2
exit 0
"""
RESPONSE_SH = r"""#!/usr/bin/env bash
# shapes the response from the stream request.sh printed: one message carrying the outcome
text="shaped: outcome=$HG_OUTCOME exit=$HG_EXIT"
printf '{"output":[{"type":"message","id":"msg_shaped","role":"assistant","status":"completed","content":[{"type":"output_text","text":"%s","annotations":[]}]}],"usage":{"input_tokens":1,"output_tokens":2},"metadata":{"shaped":"yes"}}\n' "$text"
"""
INSTALL_SH = "#!/usr/bin/env bash\nset -eu\nmkdir -p \"$HG_TOOLS/bin\"\nprintf '#!/bin/sh\\necho tool-ran\\n' > \"$HG_TOOLS/bin/tool\"\nchmod +x \"$HG_TOOLS/bin/tool\"\n"


def members(files: dict[str, str | bytes | None] | None = None, **changes: str) -> dict[str, bytes]:
    config = {**HARNESS_JSON, **{k: json.loads(v) if v.startswith(("{", "[")) else v for k, v in changes.items()}}
    out: dict[str, str | bytes | None] = {"harness.json": json.dumps(config), "install.sh": INSTALL_SH, "request.sh": REQUEST_SH,
                                           "seeds/home/home.txt": "from-home", "seeds/workspace/README.md": "from-workspace"}
    if files is not None:
        out = {**out, **files}
    return {k: (v.encode() if isinstance(v, str) else v) for k, v in out.items() if v is not None}


def archive(files: dict[str, str | bytes | None] | None = None, root: str = "", **changes: str) -> bytes:
    """A package as a tar: the test package, with `files` replacing or adding members (None removes)
    and `changes` replacing harness.json's top-level keys (JSON text for objects). `root` wraps it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members(files, **changes).items():
            info = tarfile.TarInfo(f"{root}/{name}" if root else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def folder(into: Path, files: dict[str, str | bytes | None] | None = None, **changes: str) -> Path:
    for name, data in members(files, **changes).items():
        (into / name).parent.mkdir(parents=True, exist_ok=True)
        (into / name).write_bytes(data)
    return into


class LocalRuntime:
    """Runs a spec as a process here, with the spec's environment plus the host's PATH; the token
    file and provider URL a Docker runtime would give are faked."""

    def __init__(self, token: str = ""):
        self.token = token
        self.provider_url = "http://hg-provider:8080" if token else None
        self.token_path: Path | None = None

    async def start(self, spec: Spec) -> Execution:
        env = {**spec.env, "PATH": f"{spec.env.get('PATH', '')}:{host_env().get('PATH', os.defpath)}"}
        return await ProcessExecution.spawn(list(spec.command), env=env, cwd=spec.workspace, input=spec.input,
                                            interactive=spec.interactive, timeout_s=spec.timeout_s, stop_signal=15)

    async def recover(self) -> None:
        pass


class FakeNode:
    """A node without Docker: the turn runs as a process here; install.sh too."""
    image = "fake"

    def __init__(self):
        self.installs = 0

    async def prepare(self) -> str:
        return "fakeimage000"

    async def recover(self) -> None:
        pass

    def runtime_for(self, org, limits, *, read_only=(), writable=(), egress=(), gateway_url="", token=""):
        return LocalRuntime(token)

    async def tools_for(self, org, limits, folder, config, root, image_id) -> Path:
        """install.sh runs here as a process, so a package's tools layer is real."""
        import subprocess
        tools = root / f"{folder.name}-{image_id[:12]}"
        if not tools.is_dir():
            self.installs += 1
            tools.mkdir(parents=True)
            (tools / "bin").mkdir()
            env = {**host_env(), "HG_TOOLS": str(tools), "HG_HARNESS_DIR": str(folder), "HOME": str(tools)}
            subprocess.run(["bash", str(folder / "install.sh")], env=env, check=True, capture_output=True, timeout=120)
        return tools
