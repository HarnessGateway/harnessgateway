"""Layer 1 under Docker: the hardened container, the turn runner, the door and the forwarder.
A real base image with nothing of ours in it; the tenant's uid; no capabilities; the token in the
container and only the token; provider calls relayed to the gateway through the door; private
egress refused; response.sh deciding the response; a stop."""
import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from harnessgateway.kernel.execution import Spec
from harnessgateway.backends.sandboxes.docker import TURN_SCRIPT, DockerRuntime, Resources, docker, owner_of

pytestmark = pytest.mark.docker
IMAGE = "python:3.12-slim"
CA = Path(os.environ["HG_CA_FILE"]) if os.environ.get("HG_CA_FILE") else None
REQUEST = """#!/usr/bin/env bash
set -u
prompt=$(cat)
echo "prompt=$prompt uid=$(id -u) caps=$(grep CapEff /proc/self/status | awk '{print $2}')"
echo "user=$(id -un) home=$(python3 -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
echo "token=$(cat "$HG_PROVIDER_KEY_FILE") keyenv=${FAKE_KEY:-} url=${FAKE_URL:-}"
fetch() { python3 -c "import sys,urllib.request as u
r=u.Request(sys.argv[1], headers={'Authorization': 'Bearer ' + open(sys.argv[2]).read().strip()} if len(sys.argv) > 2 else {})
try: print(u.urlopen(r, timeout=8).read().decode())
except Exception as e: print('refused' if '403' in str(e) else 'FAIL ' + str(e)[:80])" "$@"; }
echo "gateway=$(fetch "$HG_PROVIDER_URL/v1/models" "$HG_PROVIDER_KEY_FILE")"
echo "private=$(fetch https://169.254.169.254/)"
echo "rootfs=$(touch /x 2>/dev/null && echo writable || echo readonly)"
echo "a warning" >&2
if [ "$prompt" = "wait" ]; then sleep 60 & wait $!; fi
exit 4
"""
RESPONSE = """#!/usr/bin/env bash
echo "outcome=$HG_OUTCOME exit=$HG_EXIT lines=$(wc -l < "$HG_STDOUT") err=$(cat "$HG_STDERR")"
exit 0
"""


class Gateway(BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def do_GET(self):
        Gateway.seen.append((self.path, self.headers.get("Authorization", "")))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def gateway():
    server = HTTPServer(("0.0.0.0", 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    code, out = asyncio.run(docker("network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"))
    assert code == 0, out
    yield f"http://{out.strip()}:{server.server_port}"
    server.shutdown()


def folder(tmp_path: Path, response: bool = True) -> Path:
    f = tmp_path / "folder"
    f.mkdir()
    (f / "request.sh").write_text(REQUEST)
    if response:
        (f / "response.sh").write_text(RESPONSE)
    return f


def runtime(tmp_path: Path, gateway: str, **kw) -> DockerRuntime:
    return DockerRuntime(IMAGE, uid=100123, limits=Resources(cpus=1, memory="1g", pids=128), read_only=(tmp_path / "folder",),
                         writable=(tmp_path / "home", tmp_path / "run"), gateway_url=gateway, token="sk-hg-testtoken",
                         ca_file=CA, owner=owner_of(tmp_path), **kw)


def spec(tmp_path: Path, prompt: str, rt: DockerRuntime, timeout: float = 120) -> Spec:
    for d in ("workspace", "home", "run"):
        (tmp_path / d).mkdir(exist_ok=True)
    (tmp_path / "run" / "request.json").write_text(prompt)
    env = {"HG_REQUEST": str(tmp_path / "run" / "request.json"), "HG_WORKSPACE": str(tmp_path / "workspace"), "HOME": str(tmp_path / "home"), "HG_DIR": str(tmp_path / "run"),
           "HG_HARNESS_DIR": str(tmp_path / "folder"), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "HG_PROVIDER_URL": rt.provider_url + "/x", "HG_PROVIDER_KEY_FILE": str(rt.token_path),
           "FAKE_KEY": "sk-hg-testtoken", "FAKE_URL": rt.provider_url}
    return Spec(("bash", str(TURN_SCRIPT)), input=prompt.encode(), env=env, workspace=tmp_path / "workspace", timeout_s=timeout)


async def run(rt: DockerRuntime, sp: Spec):
    ex = await rt.start(sp)
    out: dict[str, bytes] = {"stdout": b"", "stderr": b"", "door": b""}
    async for e in ex.events():
        out[e.stream] += e.data
    return ex, out, await ex.wait()


def test_a_turn(tmp_path, gateway):
    folder(tmp_path)
    rt = runtime(tmp_path, gateway)
    ex, out, outcome = asyncio.run(run(rt, spec(tmp_path, "hello", rt)))
    lines = dict(line.split("=", 1) for line in out["stdout"].decode().splitlines() if "=" in line)
    assert outcome.kind == "exited" and outcome.code == 0, (outcome, out)  # response.sh's exit code, not request.sh's 4
    uid = 100123 if os.geteuid() == 0 else os.getuid()                           # unprivileged servers run as themselves
    assert lines["prompt"].startswith(f"hello uid={uid} caps=0000000000000000")
    assert lines["user"] == f"sandbox home={tmp_path / 'home'}"                 # a uid with a name and a home
    assert lines["token"] == "sk-hg-testtoken keyenv=sk-hg-testtoken url=http://hg-provider:8080"
    assert lines["gateway"] == '{"ok": true}' and Gateway.seen[-1] == ("/x/v1/models", "Bearer sk-hg-testtoken")
    assert lines["private"] == "refused" and lines["rootfs"] == "readonly"
    assert out["stderr"].decode() == "a warning\n"
    response = (tmp_path / "run" / "response").read_text()
    assert response.startswith("outcome=exited exit=4 lines=6 err=a warning")
    assert (tmp_path / "run" / "stdout").read_bytes() == out["stdout"]
    assert b"door deny 169.254.169.254:443" in out["door"] and b"gateway forward" in out["door"]
    assert (tmp_path / "workspace").stat().st_uid == uid


def test_without_response_sh(tmp_path, gateway):
    folder(tmp_path, response=False)
    rt = runtime(tmp_path, gateway)
    _, out, outcome = asyncio.run(run(rt, spec(tmp_path, "hello", rt)))
    assert outcome.kind == "exited" and outcome.code == 4
    assert not (tmp_path / "run" / "response").exists()                       # no response.sh: the stream events are the response


def test_stop(tmp_path, gateway):
    folder(tmp_path)
    rt = runtime(tmp_path, gateway)

    async def go():
        ex2 = await rt.start(spec(tmp_path, "wait", rt))
        out2 = b""
        async for e in ex2.events():
            if e.stream == "stdout":
                out2 += e.data
                if b"rootfs=" in out2:
                    ex2.stop(10)
        return out2, await ex2.wait()
    out2, stopped = asyncio.run(go())
    assert (tmp_path / "run" / "response").read_text().startswith("outcome=cancelled")
    assert stopped.kind == "stopped" and stopped.code == 0 and b"rootfs=" in out2
