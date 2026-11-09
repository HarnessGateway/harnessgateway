"""sandbox/runner.py against an in-process fake api: the box routes as the api serves them, with the shell package.
The runner runs as a subprocess with a temporary directory as its box root and no door (direct api), so what the
real api would see (ready, next, events, finish, blobs, manifest, bye; or archive and installed) is asserted here."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from hgsnapshot import Manifest

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "packages" / "shell"
RUNNER = ROOT / "sandbox" / "runner.py"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def archive_of(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


def empty_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


class FakeApi:
    """The box routes with one box, one session and a queue of turns; everything it was told is kept."""

    def __init__(self, kind: str = "session", standby_s: int = 1, package: Path = SHELL):
        self.kind, self.standby_s = kind, standby_s
        self.objects: dict[str, bytes] = {"packages/shell.tar.gz": archive_of(package),
                                          "orgs/o/installs/x.tar.zst": empty_tar()}
        self.blobs: dict[str, bytes] = {}
        self.turns: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, Any]] = []
        self.events: dict[str, list[str]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.manifests: dict[str, Manifest] = {}
        self.saved: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, bytes] = {}
        self.cancel: set[str] = set()
        self.snapshot: Manifest | None = None
        self.archive: bytes | None = None
        self.installed: dict[str, Any] | None = None
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def queue(self, prompt: str, **extra: Any) -> str:
        rid = f"resp_{len(self.turns) + 1:032d}"
        key = f"orgs/o/turns/{rid}/request.json"
        self.objects[key] = json.dumps({"model": "shell", "input": prompt}).encode()
        self.objects[f"orgs/o/turns/{rid}/history.json"] = b"[]"
        self.turns.append({"turn_id": rid, "seq": len(self.turns) + 1, "harness": "shell", "model": "", "store": True,
                           "timeout_s": extra.pop("timeout_s", None), "stop_grace_s": 1, "observability": "metadata",
                           "request_key": key, "history_key": f"orgs/o/turns/{rid}/history.json", "env": {},
                           "mcp": [], "provider": None, "files": [], "ops": [],
                           "roots": ["workspace", "state", "home/hg"], **extra})
        return rid

    def info(self) -> dict[str, Any]:
        manifest = json.loads((SHELL / "harness.json").read_text())
        package = {"digest": "d" * 64, "key": "packages/shell.tar.gz", "manifest": {**manifest, "persist": ["."]}}
        if self.kind == "install":
            return {"id": "box_1", "kind": "install", "org_id": "o", "package": package,
                    "install": {"id": "inst_1", "timeout_s": 30}, "ca_pem": CA, "tmp_mb": 100}
        snap = {"key": "orgs/o/sessions/s/snapshots/prev.jsonl"} if self.snapshot else None
        if self.snapshot:
            self.objects[snap["key"]] = self.snapshot.dumps().encode()
        return {"id": "box_1", "kind": "session", "org_id": "o", "session_id": "sess_1", "harness": "shell",
                "new_session": self.snapshot is None, "package": package, "install": {"id": "inst_1",
                "key": "orgs/o/installs/x.tar.zst"}, "snapshot": snap, "roots": ["workspace", "state", "home/hg"],
                "standby_s": self.standby_s, "tmp_mb": 100, "ca_pem": CA}

    def handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def reply(self, status: int, doc: Any = None, raw: bytes | None = None) -> None:
                data = raw if raw is not None else json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def route(self) -> str:
                assert self.headers.get("Authorization") == "Bearer hgb_test"
                return self.path.removeprefix("/internal/boxes/box_1")

            def do_GET(self) -> None:
                path = self.route()
                if path == "":
                    return self.reply(200, api.info())
                if path.startswith("/objects/"):
                    key = path[len("/objects/"):]
                    return self.reply(200, raw=api.objects[key]) if key in api.objects else self.reply(404, {})
                if path.startswith("/blobs/"):
                    return self.reply(200, raw=api.blobs[path[len("/blobs/"):]])
                if path.startswith("/next"):
                    with api.lock:
                        api.calls.append(("GET", "/next", None))
                        if api.turns:
                            return self.reply(200, api.turns.pop(0))
                    time.sleep(0.2)
                    return self.reply(204)
                self.reply(404, {})

            def do_POST(self) -> None:
                path = self.route()
                doc = json.loads(self.body() or b"{}")
                with api.lock:
                    api.calls.append(("POST", path, doc))
                if path.endswith("/events"):
                    rid = path.split("/")[2]
                    api.events.setdefault(rid, []).extend(doc["lines"])
                    concluded = any('"response.completed"' in line or '"response.failed"' in line for line in doc["lines"])
                    return self.reply(200, {"concluded": concluded, "stop": False})
                if path.endswith("/heartbeat"):
                    return self.reply(200, {"cancel": path.split("/")[2] in api.cancel, "stop": False})
                if path.endswith("/finish"):
                    api.reports[path.split("/")[2]] = doc
                    return self.reply(200, {"persist": True})
                if path.endswith("/saved"):
                    api.saved[path.split("/")[2]] = doc
                    return self.reply(200, {"ok": True})
                if path == "/blobs":
                    return self.reply(200, {"missing": [h for h in doc["hashes"] if h not in api.blobs]})
                if path == "/installed":
                    api.installed = doc
                    api.done.set()
                    return self.reply(200, {"ok": True})
                if path == "/bye":
                    api.done.set()
                return self.reply(200, {"ok": True})

            def do_PUT(self) -> None:
                path = self.route()
                data = self.body()
                if path.startswith("/blobs/"):
                    h = path[len("/blobs/"):]
                    assert hashlib.sha256(data).hexdigest() == h
                    api.blobs[h] = data
                    return self.reply(200, {"hash": h, "bytes": len(data)})
                if path.endswith("/manifest"):
                    m = Manifest.loads(data.decode())
                    api.manifests[path.split("/")[2]] = m
                    return self.reply(200, {"snapshot_id": "snap_x", "bytes": m.size(), "files": m.files()})
                if "/logs/" in path:
                    api.logs[path.rsplit("/", 1)[1]] = data
                    return self.reply(200, {"ok": True})
                if path == "/archive":
                    api.archive = data
                    return self.reply(200, {"bytes": len(data)})
                self.reply(404, {})

        return Handler


def run_runner(api: FakeApi, root: Path, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HG_API_URL": api.url, "HG_BOX_ID": "box_1", "HG_BOX_TOKEN": "hgb_test", "HG_DOOR": "",
           "HG_ROOT": str(root), "PYTHONPATH": str(ROOT / "sandbox")}
    env.pop("HG_RUN_UID", None)
    return subprocess.run([sys.executable, str(RUNNER)], env=env, capture_output=True, text=True, timeout=timeout)


def test_a_session_box_runs_turns_saves_what_changed_and_leaves_at_the_end_of_standby(tmp_path):
    api = FakeApi()
    first = api.queue("echo hello > hello.txt && echo done")
    second = api.queue("echo more >> hello.txt && mkdir -p sub && echo x > sub/y && cat hello.txt")
    out = run_runner(api, tmp_path)
    assert out.returncode == 0, out.stderr
    assert ("POST", "/ready", {}) in api.calls and ("POST", "/bye", {}) in api.calls
    assert (tmp_path / "opt/hg/package/run").is_file() and (tmp_path / "opt/hg/runtime/ca.pem").read_text().endswith(CA)
    assert (tmp_path / "workspace/README.md").is_file()                        # the package's workspace seed
    assert any("response.completed" in line for line in api.events[first])
    assert api.reports[first]["outcome"] == "exited" and api.reports[first]["exit_code"] == 0
    assert api.reports[first]["applied_ops"] == [] and api.reports[first]["cpu_seconds"] >= 0
    m1, m2 = api.manifests[first], api.manifests[second]
    assert m1.entries["workspace/hello.txt"].size == 6 and "workspace/sub/y" not in m1.entries
    assert m2.entries["workspace/hello.txt"].size == 11 and m2.entries["workspace/sub/y"].kind == "f"
    assert {"workspace", "state", "home/hg"} <= set(m2.entries)
    assert set(api.blobs) >= {hashlib.sha256(b"hello\n").hexdigest(), hashlib.sha256(b"hello\nmore\n").hexdigest(),
                              hashlib.sha256(b"x\n").hexdigest()}
    assert api.saved == {}                                                     # both turns changed state
    assert any('"text":"hello\\nmore\\n"' in line for line in api.events[second])


def test_a_cancel_stops_the_process_and_a_timeout_ends_a_turn(tmp_path):
    api = FakeApi(standby_s=2)
    slow = api.queue("sleep 30")
    api.cancel.add(slow)
    late = api.queue("sleep 30", timeout_s=2)
    started = time.monotonic()
    out = run_runner(api, tmp_path)
    assert out.returncode == 0, out.stderr
    assert api.reports[slow]["outcome"] == "stopped" and api.reports[slow]["exit_code"] != 0
    assert api.reports[late]["outcome"] == "timed_out"
    assert slow in api.manifests and late in api.saved                         # the first save; then nothing changed
    assert time.monotonic() - started < 25


def test_a_restored_snapshot_round_trips_as_unchanged(tmp_path):
    api = FakeApi()
    first = api.queue("echo keep > keep.txt")
    out = run_runner(api, tmp_path / "one")
    assert out.returncode == 0, out.stderr
    api.snapshot = api.manifests[first]
    api.turns.clear()
    quiet = api.queue("cat keep.txt")
    out = run_runner(api, tmp_path / "two")
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "two/workspace/keep.txt").read_bytes() == b"keep\n"
    assert any('"text":"keep\\n"' in line for line in api.events[quiet])
    assert quiet in api.saved and api.saved[quiet] == {}                       # restored and walked: identical


def test_an_install_box_runs_install_and_uploads_the_archive(tmp_path):
    api = FakeApi(kind="install")
    out = run_runner(api, tmp_path)
    assert out.returncode == 0, out.stderr
    assert api.installed and api.installed["ok"] is True and "installed: bash" in api.installed["log_tail"]
    assert api.archive and api.installed["bytes"] == len(api.archive)
    archive = tmp_path / "tools.tar"                      # zstd where the host has it, gzip otherwise: tar tells them apart
    archive.write_bytes(api.archive)
    listing = subprocess.run(["tar", "-tf", str(archive)], check=True, capture_output=True, text=True).stdout
    assert listing.splitlines()[0] == "./"


def test_a_line_the_api_refuses_stops_the_turn(tmp_path):
    package = tmp_path / "bad"
    package.mkdir()
    (package / "harness.json").write_text(json.dumps({"contract": 1, "name": "bad", "version": "1-1", "description": "x"}))
    (package / "install").write_text("#!/bin/sh\n")
    (package / "run").write_text("#!/usr/bin/env python3\nimport sys, time\n"
                                 "print('{\"type\": \"response.output_item.added\", \"item\": 5}', flush=True)\n"
                                 "time.sleep(30)\n")
    api = FakeApi(package=package)
    rid = api.queue("x")
    base = api.handler()

    class Refusing(base):  # type: ignore[valid-type, misc]
        def do_POST(self) -> None:
            if self.path.endswith("/events"):
                self.body()
                return self.reply(200, {"concluded": True, "stop": True})
            super().do_POST()
    api.server.RequestHandlerClass = Refusing
    started = time.monotonic()
    out = run_runner(api, tmp_path / "root")
    assert out.returncode == 0, out.stderr
    assert api.reports[rid]["outcome"] == "stopped" and time.monotonic() - started < 20


def test_a_terminated_box_says_bye_at_once(tmp_path):
    """Its pod deleted or drained: SIGTERM, and the api hears bye before the box goes."""
    import signal
    api = FakeApi(standby_s=60)
    env = {**os.environ, "HG_API_URL": api.url, "HG_BOX_ID": "box_1", "HG_BOX_TOKEN": "hgb_test", "HG_DOOR": "",
           "HG_ROOT": str(tmp_path), "PYTHONPATH": str(ROOT / "sandbox")}
    env.pop("HG_RUN_UID", None)
    proc = subprocess.Popen([sys.executable, str(RUNNER)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 20
    while ("POST", "/ready", {}) not in api.calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ("POST", "/ready", {}) in api.calls
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=10)
    assert proc.returncode == 143, err
    assert ("POST", "/bye", {}) in api.calls and "terminated: bye" in err
