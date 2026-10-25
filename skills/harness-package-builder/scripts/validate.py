#!/usr/bin/env python3
"""Validate a HarnessGateway harness package, and optionally run one turn of it locally (no sandbox).

    python3 validate.py ./my-harness                                   # static checks only
    python3 validate.py ./my-harness --prompt "hi" --provider-fixture recorded.sse
    python3 validate.py ./my-harness --prompt "hi" --provider-url https://api.anthropic.com \\
        --provider-api anthropic --provider-key "$ANTHROPIC_API_KEY" --model claude-sonnet-4-5 [--install]

Static checks: required files, the manifest (contract 1), bash syntax. A local run sets up the turn environment the
platform provides (HG_* variables, a workspace, home and state seeded from the package), runs request.sh with the
request on stdin and validates every printed event with the OpenAI SDK's models, as the platform does. A provider
fixture is a recorded SSE stream served to the package for any provider call. A local run executes the package on
this machine, without the sandbox: run only packages you trust. Requires: pip install openai.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import typing
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+(:[0-9]{1,5})?")
KEYS = {"contract", "name", "version", "description", "provider", "defaults", "install", "run", "settings"}
TERMINAL = {"response.completed", "response.incomplete", "response.failed"}
ENVELOPE = {"response.created", "response.in_progress", "response.queued"}


def fail(msg: str) -> typing.NoReturn:
    print(f"FAIL  {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"ok    {msg}")


def check_static(pkg: Path) -> dict:
    for name in ("harness.json", "install.sh", "request.sh"):
        if not (pkg / name).is_file():
            fail(f"{name} is missing")
    if any(p.is_symlink() for p in pkg.rglob("*")):
        fail("a package holds only regular files and directories (no symlinks)")
    try:
        m = json.loads((pkg / "harness.json").read_text())
    except ValueError as e:
        fail(f"harness.json is not JSON: {e}")
    if not isinstance(m, dict) or m.get("contract") != 1:
        fail('harness.json must be an object with "contract": 1')
    if set(m) - KEYS:
        fail(f"harness.json: unknown keys {sorted(set(m) - KEYS)}")
    if not isinstance(m.get("name"), str) or not NAME.fullmatch(m["name"]):
        fail("name: lowercase letters, digits, . _ -; at most 63; starts with a letter or digit")
    if not isinstance(m.get("version"), str) or not 0 < len(m["version"]) <= 64:
        fail("version: 1-64 characters")
    prov = m.get("provider")
    if prov is not None and (set(prov) != {"apis"} or not prov["apis"] or set(prov["apis"]) - {"anthropic", "openai"}):
        fail('provider: {"apis": [...]} with "anthropic" and/or "openai"')
    for section, limit in (("install", 3600), ("run", 86_400)):
        s = m.get(section, {})
        allowed = {"egress", "timeout_s"} | ({"stop_grace_s"} if section == "run" else set())
        if set(s) - allowed:
            fail(f"{section}: only {sorted(allowed)}")
        if not all(isinstance(h, str) and HOST.fullmatch(h.lower()) for h in s.get("egress", [])):
            fail(f"{section}.egress: hostnames with an optional :port (no scheme)")
        if not 1 <= s.get("timeout_s", 1) <= limit:
            fail(f"{section}.timeout_s: 1-{limit}")
    for k, v in (m.get("settings") or {}).items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", k) or set(v) - {"secret", "required", "description"}:
            fail(f"settings.{k}: an A-Z0-9_ name with secret/required/description")
    ok(f"harness.json: {m['name']} {m['version']}")
    for script in ("install.sh", "request.sh"):
        r = subprocess.run(["bash", "-n", str(pkg / script)], capture_output=True, text=True)
        if r.returncode:
            fail(f"{script} is not valid bash: {r.stderr.strip()}")
    ok("install.sh and request.sh parse")
    seeds = pkg / "seeds"
    if seeds.exists() and {p.name for p in seeds.iterdir()} - {"workspace", "home"}:
        fail("seeds holds only workspace/ and home/")
    return m


def event_models() -> dict:
    from openai.types import responses as R
    union = typing.get_args(R.ResponseStreamEvent)[0]
    return {typing.get_args(m.model_fields["type"].annotation)[0]: m for m in typing.get_args(union)}


def check_output(lines: list[str]) -> None:
    from openai.types import responses as R
    from pydantic import TypeAdapter
    models, items, terminal, events = event_models(), [], None, 0
    for n, text in enumerate(lines, 1):
        s = text.strip()
        try:
            ev = json.loads(s) if s.startswith("{") else None
        except ValueError:
            ev = None
        kind = ev.get("type") if isinstance(ev, dict) else None
        if not (isinstance(kind, str) and kind.startswith("response.")):
            continue                                       # a log line
        if terminal:
            fail(f"line {n}: {kind} after the terminal event")
        if kind in ENVELOPE:
            print(f"note  line {n}: {kind} is dropped (the platform sends the envelope)")
            continue
        if kind in TERMINAL:
            terminal = ev.get("response") or {}
            continue
        if kind not in models:
            fail(f"line {n}: {kind} is not a Responses stream event")
        try:
            models[kind].model_validate({**ev, "sequence_number": 0})
        except Exception as e:
            fail(f"line {n}: {kind}: {str(e).splitlines()[0:3]}")
        events += 1
        if kind == "response.output_item.done":
            items.append(ev["item"])
    if terminal is None:
        fail("no terminal event (response.completed | response.incomplete | response.failed)")
    output = terminal.get("output") or items
    TypeAdapter(list[R.ResponseOutputItem]).validate_python(output)
    if terminal.get("usage") is not None:
        TypeAdapter(R.ResponseUsage).validate_python(terminal["usage"])
    ok(f"{events} events and the terminal event validate; output: {[i.get('type') for i in output]}")


class Fixture(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


def run_turn(pkg: Path, manifest: dict, args: argparse.Namespace) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hg-local-"))
    dirs = {k: tmp / k for k in ("workspace", "home", "state", "run", "tools")}
    for d in dirs.values():
        d.mkdir()
    for seed, dest in (("workspace", "workspace"), ("home", "home")):
        if (pkg / "seeds" / seed).is_dir():
            shutil.copytree(pkg / "seeds" / seed, dirs[dest], dirs_exist_ok=True)
    request = json.loads(Path(args.request).read_text()) if args.request else {"model": manifest["name"], "input": args.prompt}
    (dirs["run"] / "request.json").write_text(json.dumps(request))
    (dirs["run"] / "settings.json").write_text(json.dumps(dict(s.split("=", 1) for s in args.setting)))
    (dirs["run"] / "files").mkdir()
    (dirs["run"] / "files" / "index.json").write_text("{}")
    url, server = args.provider_url, None
    if args.provider_fixture:
        Fixture.body = Path(args.provider_fixture).read_bytes()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
    env = {**os.environ, "HG_CONTRACT": "1", "HG_REQUEST": str(dirs["run"] / "request.json"),
           "HG_SETTINGS": str(dirs["run"] / "settings.json"), "HG_FILES": str(dirs["run"] / "files"),
           "HG_WORKSPACE": str(dirs["workspace"]), "HOME": str(dirs["home"]), "HG_STATE": str(dirs["state"]),
           "HG_PACKAGE_DIR": str(pkg.resolve()), "HG_TOOLS": str(dirs["tools"]), "HG_SESSION": "cntr_local",
           "HG_RESPONSE": "resp_local", "HG_TURN": "1", "HG_MODEL": args.model or manifest.get("defaults", {}).get("model", ""),
           "HG_PROVIDER_API": args.provider_api, "HG_PROVIDER_URL": url or "", "HG_PROVIDER_TOKEN": args.provider_key,
           "PATH": f"{dirs['tools']}/bin:{dirs['tools']}/node_modules/.bin:{os.environ.get('PATH', '')}",
           "PYTHONPATH": f"{HERE}:{os.environ.get('PYTHONPATH', '')}"}
    if args.install:
        r = subprocess.run(["bash", str(pkg / "install.sh")], env=env, cwd=tmp)
        if r.returncode:
            fail("install.sh failed")
        ok("install.sh ran")
    proc = subprocess.run(["bash", str(pkg / "request.sh")], env=env, cwd=dirs["workspace"],
                          input=(dirs["run"] / "request.json").read_bytes(), capture_output=True, timeout=args.timeout)
    if server:
        server.shutdown()
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    print(f"----- stdout ({len(lines)} lines), exit {proc.returncode}")
    for line in lines[:40]:
        print(f"  {line[:200]}")
    if proc.stderr:
        print("----- stderr (tail)\n  " + proc.stderr.decode("utf-8", "replace")[-1500:].replace("\n", "\n  "))
    check_output(lines)
    print(f"workspace left in {dirs['workspace']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", type=Path)
    ap.add_argument("--prompt", help="run one turn with this input text")
    ap.add_argument("--request", help="run one turn with this Responses request (JSON file)")
    ap.add_argument("--provider-fixture", help="a recorded SSE stream to answer every provider call with")
    ap.add_argument("--provider-url", default="")
    ap.add_argument("--provider-api", default="anthropic", choices=["anthropic", "openai"])
    ap.add_argument("--provider-key", default="local-token")
    ap.add_argument("--model", default="")
    ap.add_argument("--setting", action="append", default=[], help="NAME=value for $HG_SETTINGS")
    ap.add_argument("--install", action="store_true", help="run install.sh first (on this machine)")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    package = args.package.resolve()
    manifest = check_static(package)
    if args.prompt or args.request:
        run_turn(package, manifest, args)
    print("PASS")


if __name__ == "__main__":
    main()
