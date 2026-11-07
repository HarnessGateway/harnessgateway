#!/usr/bin/env python3
"""The runner: what runs inside a box (SPEC §9.2, §9.5, §9.6). Standard library only, plus hgsnapshot.

A session box: fetch what the box is, set up the package, the install's tools, the trust bundle, the seeds and the
session's last snapshot; say ready; then, until no turn arrives within the standby, take the session's next turn,
run the package's `run` on it, stream its output, report how it ended, save what changed, and kill everything else.
An install box: run the package's `install` into the tools directory, archive it and report.

Environment: HG_BOX_ID, HG_BOX_TOKEN, HG_API_URL, HG_DOOR (host:port of the proxy every connection goes through; empty
for a direct api, as in tests), HG_ROOT (the box root, "/" in a box; a directory in tests), HG_RUN_UID (the user
`run` and `install` run as when the runner is root; 10001 in the image)."""
from __future__ import annotations

import base64
import contextlib
import http.client
import json
import os
import resource
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hgsnapshot import CHUNK, Manifest, restore, walk

HEARTBEAT_S = 3.0
BATCH_S = 0.2
BATCH_BYTES = 1 * 1024 * 1024
LINE_MAX = 64 * 1024 * 1024
TAIL = 16 * 1024
EXIT_WAIT_S = 10
NEXT_WAIT_S = 25
FETCH_THREADS = 4
SYSTEM_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
TRUST_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS", "PIP_CERT")


def log(message: str) -> None:
    print(f"runner: {message}", file=sys.stderr, flush=True)


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"api {status}: {body[:300]}")
        self.status = status


class Api:
    """The api as a box reaches it: every request through the door (an HTTP CONNECT tunnel with the box token as
    proxy credential) when a door is configured, else direct."""

    def __init__(self, url: str, box_id: str, token: str, door: str):
        u = urlsplit(url)
        assert u.hostname
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)
        self.prefix = f"/internal/boxes/{box_id}"
        self.token, self.door = token, door
        self.proxy_auth = "Basic " + base64.b64encode(f"box:{token}".encode()).decode()
        self.ca: str | None = None             # the trust bundle, once written (the api behind TLS through the door)

    def connect(self) -> http.client.HTTPConnection:
        context = ssl.create_default_context(cafile=self.ca) if self.scheme == "https" else None
        if self.door:
            host, _, port = self.door.rpartition(":")
            cx: http.client.HTTPConnection = http.client.HTTPSConnection(host, int(port), context=context, timeout=600) \
                if context else http.client.HTTPConnection(host, int(port), timeout=600)
            cx.set_tunnel(self.host, self.port, headers={"Proxy-Authorization": self.proxy_auth})
            return cx
        if context:
            return http.client.HTTPSConnection(self.host, self.port, context=context, timeout=600)
        return http.client.HTTPConnection(self.host, self.port, timeout=600)

    def request(self, method: str, path: str, body: Any = None, *, raw: bytes | None = None, stream: Any = None,
                length: int | None = None) -> tuple[int, http.client.HTTPResponse]:
        headers = {"Authorization": f"Bearer {self.token}"}
        data: Any = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif stream is not None:
            data = stream
            if length is not None:
                headers["Content-Length"] = str(length)
        cx = self.connect()
        cx.request(method, self.prefix + path, body=data, headers=headers)
        r = cx.getresponse()
        return r.status, r

    def call(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        """A JSON reply, or None on 204."""
        status, r = self.request(method, path, body, **kw)
        text = r.read().decode("utf-8", "replace")
        if status == 204:
            return None
        if status >= 300:
            raise ApiError(status, text)
        return json.loads(text) if text else {}

    def download(self, path: str, into: Path) -> int:
        status, r = self.request("GET", path)
        if status >= 300:
            raise ApiError(status, r.read().decode("utf-8", "replace"))
        size = 0
        into.parent.mkdir(parents=True, exist_ok=True)
        with into.open("wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                size += len(chunk)
        return size

    def upload(self, method: str, path: str, file: Path) -> Any:
        with file.open("rb") as f:
            return self.call(method, path, stream=f, length=file.stat().st_size)


class Box:
    """The directories of a box, under a root ("/" in a real box)."""

    def __init__(self, root: Path, run_uid: int | None):
        self.root, self.run_uid = root, run_uid
        self.workspace, self.state, self.home = root / "workspace", root / "state", root / "home/hg"
        self.run_dir, self.package, self.tools = root / "run/hg", root / "opt/hg/package", root / "opt/hg/tools"
        self.runtime, self.blobs, self.tmp = root / "opt/hg/runtime", root / "tmp/hg/blobs", root / "tmp"
        for d in (self.workspace, self.state, self.home, self.run_dir, self.package, self.tools, self.runtime, self.blobs):
            d.mkdir(parents=True, exist_ok=True)
        self.bundle = self.runtime / "ca.pem"

    def own(self, *paths: Path) -> None:
        """What `run` writes belongs to the run user (when the runner is root)."""
        if self.run_uid is None:
            return
        for p in paths:
            for q in (p, *p.rglob("*")):
                with contextlib.suppress(OSError):
                    os.lchown(q, self.run_uid, self.run_uid)

    def demote(self) -> Callable[[], None] | None:
        """A preexec function that becomes the run user, for a runner that is root."""
        uid = self.run_uid
        if uid is None or os.geteuid() != 0:
            return None

        def become() -> None:
            os.setgid(uid)
            os.setuid(uid)
        return become

    def trust(self, ca_pem: str) -> dict[str, str]:
        """The trust bundle every runtime in the box reads: the image's CAs and the door's."""
        system = Path(SYSTEM_BUNDLE).read_bytes().rstrip(b"\n") + b"\n" if Path(SYSTEM_BUNDLE).is_file() else b""
        self.bundle.write_bytes(system + ca_pem.encode())
        self.bundle.chmod(0o644)
        return {v: str(self.bundle) for v in TRUST_VARS}

    def kill_others(self) -> None:
        """SIGKILL every process in the box but the runner: what standby holds."""
        me, parent = os.getpid(), os.getppid()
        for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in (1, me, parent):
                continue
            if self.run_uid is not None:
                try:
                    if entry.stat().st_uid != self.run_uid:
                        continue
                except OSError:
                    continue
            elif self.root != Path("/"):
                continue                                   # a test root: only the groups it started are its own
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def extract(archive: Path, into: Path) -> None:
    """A tar (gzip, zstd or plain, detected by tar) into a directory, one wrapping directory stripped."""
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(into)], check=True, capture_output=True)
    children = [c for c in into.iterdir() if c.name != "pax_global_header"]
    if len(children) == 1 and children[0].is_dir() and not (into / "harness.json").exists():
        wrapper = children[0]
        for c in wrapper.iterdir():
            shutil.move(str(c), str(into / c.name))
        wrapper.rmdir()


def copy_tree(src: Path, dst: Path, *, overwrite: bool) -> None:
    if not src.is_dir():
        return
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif overwrite or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def cgroup_usage() -> tuple[float | None, float | None]:
    """The box's CPU seconds and memory high-water mark (MB) from cgroup v2, when the box is one."""
    cpu = memory = None
    try:
        for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                cpu = int(line.split()[1]) / 1_000_000
        memory = int(Path("/sys/fs/cgroup/memory.peak").read_text()) / (1024 * 1024)
    except (OSError, ValueError):
        pass
    return cpu, memory


class Pump(threading.Thread):
    """A turn's stdout into numbered batches of whole lines, sent as they fill or every BATCH_S."""

    def __init__(self, api: Api, rid: str, out: Any, log_file: Any, on_stop: Callable[[str], None]):
        super().__init__(daemon=True)
        self.api, self.rid, self.out, self.log_file, self.on_stop = api, rid, out, log_file, on_stop
        self.partial = bytearray()
        self.pending: list[str] = []
        self.pending_bytes = 0
        self.batch = 0
        self.total = 0
        self.concluded = threading.Event()
        self.error: str | None = None
        self.lock = threading.Lock()
        self.done = threading.Event()

    def run(self) -> None:
        ticker = threading.Thread(target=self._tick, daemon=True)
        ticker.start()
        try:
            while chunk := os.read(self.out.fileno(), 1 << 16):
                self.total += len(chunk)
                if self.log_file:
                    self.log_file.write(chunk)
                self.partial += chunk
                cut = self.partial.rfind(b"\n")
                if cut >= 0:
                    self._take(cut)
                if len(self.partial) > LINE_MAX:
                    raise ValueError(f"a stdout line is over {LINE_MAX >> 20} MiB")
                if self.pending_bytes >= BATCH_BYTES:
                    self.flush()
            if self.partial:
                self.partial += b"\n"
                self._take(len(self.partial) - 1)
            self.flush()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.on_stop("invalid_output" if isinstance(e, ValueError) else "sandbox_failed")
        finally:
            self.done.set()

    def _take(self, cut: int) -> None:
        for line in bytes(self.partial[:cut]).split(b"\n"):
            if line.strip():
                self.pending.append(line.decode("utf-8", "replace"))
                self.pending_bytes += len(line)
        del self.partial[:cut + 1]

    def _tick(self) -> None:
        while not self.done.is_set():
            time.sleep(BATCH_S)
            try:
                self.flush()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.on_stop("sandbox_failed")
                return

    def flush(self) -> None:
        with self.lock:
            if not self.pending:
                return
            lines, number = self.pending, self.batch
            self.pending, self.pending_bytes = [], 0
            self.batch += 1
            reply = self.api.call("POST", f"/turns/{self.rid}/events", {"batch": number, "lines": lines})
        if reply and reply.get("concluded"):
            self.concluded.set()
        if reply and reply.get("stop"):
            self.on_stop("invalid_output")


class Runner:
    def __init__(self, api: Api, box: Box):
        self.api, self.box = api, box
        self.info: dict[str, Any] = {}
        self.previous: Manifest | None = None           # the manifest the box's state was restored from
        self.roots: list[str] = []
        self.env_base: dict[str, str] = {}

    # ── boot ────────────────────────────────────────────────────────────────────────────────────────────────────────
    def boot(self) -> None:
        info = self.info = self.api.call("GET", "")
        trust = self.box.trust(info["ca_pem"])
        self.api.ca = str(self.box.bundle)
        # the door authenticates every connection by box token: processes in the box carry it as the proxy credential
        door = f"http://box:{self.api.token}@{self.api.door}"
        proxy = {v: door for v in PROXY_VARS} if self.api.door else {}
        self.env_base = {**trust, **proxy, "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
                         "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8",
                         "PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                         "HG_CONTRACT": "1", "HG_PACKAGE_DIR": str(self.box.package), "HG_TOOLS": str(self.box.tools),
                         "HG_PACKAGE_VERSION": str(info["package"]["manifest"]["version"])}
        with tempfile.NamedTemporaryFile(dir=self.box.tmp, suffix=".tar", delete=False) as tmp:
            archive = Path(tmp.name)
        self.api.download(f"/objects/{info['package']['key']}", archive)
        extract(archive, self.box.package)
        archive.unlink()
        for name in ("install", "run"):
            if (self.box.package / name).is_file():
                (self.box.package / name).chmod(0o755)
        if info["kind"] == "install":
            return
        self.api.download(f"/objects/{info['install']['key']}", archive)
        extract(archive, self.box.tools)
        archive.unlink()
        self.roots = list(info["roots"])
        copy_tree(self.box.package / "seeds" / "home", self.box.home, overwrite=True)
        if info["new_session"]:
            copy_tree(self.box.package / "seeds" / "workspace", self.box.workspace, overwrite=False)
        if info["snapshot"]:
            self.restore(info["snapshot"]["key"])
        self.box.own(self.box.workspace, self.box.state, self.box.home)
        self.api.call("POST", "/ready")
        log(f"ready: session {info['session_id']} on {info['harness']}")

    def restore(self, key: str) -> None:
        with tempfile.NamedTemporaryFile(dir=self.box.tmp, delete=False) as tmp:
            path = Path(tmp.name)
        self.api.download(f"/objects/{key}", path)
        manifest = Manifest.loads(path.read_text())
        path.unlink()
        self.fetch_blobs(manifest.hashes() - {p.name for p in self.box.blobs.iterdir()})
        restore(self.box.root, manifest, lambda h: (self.box.blobs / h).read_bytes())
        self.previous = manifest

    def fetch_blobs(self, hashes: Iterable[str]) -> None:
        def one(h: str) -> None:
            part = self.box.blobs / f"{h}.part"
            self.api.download(f"/blobs/{h}", part)
            part.rename(self.box.blobs / h)
        with ThreadPoolExecutor(FETCH_THREADS) as pool:
            list(pool.map(one, hashes))

    # ── the session loop ────────────────────────────────────────────────────────────────────────────────────────────
    def serve(self) -> None:
        standby = float(self.info["standby_s"])
        idle_from = time.monotonic()
        while True:
            left = standby - (time.monotonic() - idle_from)
            if left <= 0:
                break
            job = self.api.call("GET", f"/next?wait={min(left, NEXT_WAIT_S):.0f}")
            if job is None:
                continue
            try:
                self.turn(job)
            finally:
                self.box.kill_others()
            idle_from = time.monotonic()
        self.api.call("POST", "/bye")
        log("standby over: bye")

    def turn(self, job: dict[str, Any]) -> None:
        rid = job["turn_id"]
        log(f"turn {rid} ({job['seq']}) starts")
        applied: list[str] = []
        try:
            applied = self.prepare(job)
            report = self.execute(job)
        except Exception as e:
            log(f"turn {rid} failed in the box: {traceback.format_exc()}")
            report = {"outcome": "failed", "reason": "sandbox_failed", "error": f"{type(e).__name__}: {e}"}
        report["applied_ops"] = applied
        cpu, memory = cgroup_usage()
        if cpu is None:
            cpu = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime + resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime
        report.update(cpu_seconds=cpu, memory_peak_mb=memory)
        reply = self.api.call("POST", f"/turns/{rid}/finish", report)
        log(f"turn {rid} {report['outcome']} (exit {report.get('exit_code')})")
        if reply.get("persist"):
            self.save(rid)

    def prepare(self, job: dict[str, Any]) -> list[str]:
        """/run/hg for this turn, the staged file operations applied to the workspace."""
        run = self.box.run_dir
        shutil.rmtree(run, ignore_errors=True)
        (run / "files").mkdir(parents=True)
        self.api.download(f"/objects/{job['request_key']}", run / "request.json")
        self.api.download(f"/objects/{job['history_key']}", run / "history.json")
        (run / "mcp.json").write_text(json.dumps(job["mcp"]))
        index = {}
        for f in job["files"]:
            target = run / "files" / f["id"] / f["filename"]
            size = self.api.download(f"/objects/{f['key']}", target)
            index[f["id"]] = {"filename": f["filename"], "path": str(target), "bytes": size}
        (run / "files" / "index.json").write_text(json.dumps(index))
        applied = []
        for op in job["ops"]:
            target = self.box.workspace / op["path"]
            if op["op"] == "put":
                self.api.download(f"/objects/{op['key']}", target)
            elif target.is_file() or target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            applied.append(op["id"])
        self.box.own(run, self.box.workspace)
        return applied

    def env(self, job: dict[str, Any]) -> dict[str, str]:
        provider = job["provider"] or {}
        run = self.box.run_dir
        return {**self.env_base, **job["env"], "HG_REQUEST": str(run / "request.json"), "HG_HISTORY": str(run / "history.json"),
                "HG_FILES": str(run / "files"), "HG_MCP": str(run / "mcp.json"), "HG_WORKSPACE": str(self.box.workspace),
                "HG_STATE": str(self.box.state), "HOME": str(self.box.home), "HG_SESSION": self.info["session_id"],
                "HG_RESPONSE": job["turn_id"], "HG_TURN": str(job["seq"]), "HG_MODEL": job["model"] or "",
                "HG_PROVIDER_API": provider.get("api", ""), "HG_PROVIDER_URL": provider.get("url", ""),
                "HG_PROVIDER_TOKEN": provider.get("token", "")}

    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        rid, grace = job["turn_id"], float(job["stop_grace_s"])
        full = job["observability"] == "full"
        out_log = (self.box.tmp / f"{rid}.stdout").open("wb") if full else None
        err_log = (self.box.tmp / f"{rid}.stderr").open("wb") if full else None
        stop = threading.Event()
        stop_reason: list[str] = []

        def on_stop(reason: str) -> None:
            stop_reason.append(reason)
            stop.set()

        with (self.box.run_dir / "request.json").open("rb") as stdin:
            proc = subprocess.Popen([str(self.box.package / "run")], stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=self.box.workspace, env=self.env(job),
                                    start_new_session=True, preexec_fn=self.box.demote())
        assert proc.stdout and proc.stderr
        pump = Pump(self.api, rid, proc.stdout, out_log, on_stop)
        err_tail, err_bytes = bytearray(), [0]

        def read_stderr() -> None:
            assert proc.stderr
            while chunk := os.read(proc.stderr.fileno(), 1 << 16):
                err_bytes[0] += len(chunk)
                if err_log:
                    err_log.write(chunk)
                err_tail.extend(chunk)
                del err_tail[:-TAIL]

        def heartbeat() -> None:
            while not stop.is_set() and proc.poll() is None:
                time.sleep(HEARTBEAT_S)
                try:
                    reply = self.api.call("POST", f"/turns/{rid}/heartbeat")
                except Exception as e:
                    log(f"heartbeat failed: {e}")
                    continue
                if reply.get("stop"):
                    on_stop("invalid_output")
                elif reply.get("cancel"):
                    on_stop("cancelled")

        def linger() -> None:
            if pump.concluded.wait(timeout=None) and proc.poll() is None and not stop.wait(grace):
                on_stop("lingered")

        threads = [pump, threading.Thread(target=read_stderr, daemon=True), threading.Thread(target=heartbeat, daemon=True),
                   threading.Thread(target=linger, daemon=True)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + float(job["timeout_s"]) if job["timeout_s"] else None
        outcome = "exited"
        while proc.poll() is None:
            if stop.is_set():
                outcome = "stopped"
                self.stop(proc, grace)
                break
            if deadline and time.monotonic() > deadline:
                outcome = "timed_out"
                self.stop(proc, grace)
                break
            time.sleep(0.05)
        pump.done.wait(EXIT_WAIT_S)
        pump.concluded.set()                                     # frees the lingering thread
        stop.set()
        code = proc.wait()
        threads[1].join(EXIT_WAIT_S)
        for f in (out_log, err_log):
            if f:
                f.close()
        report: dict[str, Any] = {"outcome": outcome, "exit_code": code, "stderr_tail": err_tail.decode("utf-8", "replace"),
                                  "stdout_bytes": pump.total, "stderr_bytes": err_bytes[0], "logs": False}
        if pump.error and pump.error.startswith("ValueError"):
            report.update(outcome="failed", reason="invalid_output", error=pump.error)
        elif pump.error:
            report.update(outcome="failed", reason="sandbox_failed", error=f"delivering the output failed: {pump.error}")
        if full:
            uploaded = True
            for name in ("stdout", "stderr"):
                path = self.box.tmp / f"{rid}.{name}"
                try:
                    self.api.upload("PUT", f"/turns/{rid}/logs/{name}", path)
                except Exception as e:
                    uploaded = False
                    log(f"uploading the {name} of {rid} failed: {e}")
                path.unlink(missing_ok=True)
            report["logs"] = uploaded
        return report

    def stop(self, proc: subprocess.Popen[bytes], grace: float) -> None:
        """SIGTERM to the turn's process group; after it exits or the grace runs out, SIGKILL to everything."""
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(grace)
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        self.box.kill_others()

    # ── saving ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def save(self, rid: str) -> None:
        try:
            manifest = walk(self.box.root, self.roots, self.previous)
            if self.previous is not None and manifest == self.previous:
                self.api.call("POST", f"/turns/{rid}/saved", {})
                return
            new = sorted(manifest.hashes() - (self.previous.hashes() if self.previous else set()))
            missing = set(self.api.call("POST", "/blobs", {"hashes": new})["missing"]) if new else set()
            for entry in manifest.entries.values():
                for i, h in enumerate(entry.hashes):
                    if h in missing:
                        self.put_blob(h, self.box.root / entry.path, i)
                        missing.discard(h)
            made = self.api.call("PUT", f"/turns/{rid}/manifest", raw=manifest.dumps().encode())
            self.previous = manifest
            log(f"saved {made['files']} files, {made['bytes']} bytes as {made['snapshot_id']}")
        except Exception as e:
            log(f"saving after {rid} failed: {traceback.format_exc()}")
            self.api.call("POST", f"/turns/{rid}/saved", {"error": f"{type(e).__name__}: {e}"})

    def put_blob(self, digest: str, path: Path, index: int) -> None:
        with path.open("rb") as f:
            f.seek(index * CHUNK)
            piece = f.read(CHUNK)
        self.api.call("PUT", f"/blobs/{digest}", raw=piece)
        cached = self.box.blobs / digest
        if not cached.exists():
            cached.write_bytes(piece)

    # ── an install box ──────────────────────────────────────────────────────────────────────────────────────────────
    def install(self) -> None:
        self.api.call("POST", "/ready")
        info = self.info
        home = self.box.tmp / "home"
        home.mkdir(exist_ok=True)
        self.box.own(self.box.tools, home)
        env = {**self.env_base, "HOME": str(home)}
        log(f"install of {info['package']['digest'][:12]} starts")
        tail = bytearray()
        ok = False
        try:
            proc = subprocess.Popen([str(self.box.package / "install")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    cwd=self.box.tools, env=env, start_new_session=True, preexec_fn=self.box.demote())
            assert proc.stdout
            deadline = time.monotonic() + float(info["install"]["timeout_s"])
            fd = proc.stdout.fileno()

            def read_output() -> None:
                while chunk := os.read(fd, 1 << 16):
                    tail.extend(chunk)
                    del tail[:-TAIL]
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                tail.extend(b"\ninstall timed out")
                self.stop(proc, 5)
            reader.join(EXIT_WAIT_S)
            ok = proc.returncode == 0
        except Exception as e:
            tail.extend(f"\n{type(e).__name__}: {e}".encode())
        size = 0
        if ok:
            archive = self.box.tmp / "tools.tar"
            compress = ["--zstd"] if shutil.which("zstd") else ["-z"]
            subprocess.run(["tar", *compress, "-cf", str(archive), "-C", str(self.box.tools), "."], check=True)
            size = self.api.upload("PUT", "/archive", archive)["bytes"]
        cpu, _ = cgroup_usage()
        self.api.call("POST", "/installed", {"ok": ok, "bytes": size, "log_tail": tail.decode("utf-8", "replace"),
                                             "cpu_seconds": cpu})
        log(f"install {'ok' if ok else 'failed'}: {size} bytes")


def main() -> int:
    env = os.environ
    api = Api(env["HG_API_URL"], env["HG_BOX_ID"], env["HG_BOX_TOKEN"], env.get("HG_DOOR", ""))
    run_uid = int(env["HG_RUN_UID"]) if env.get("HG_RUN_UID") and os.geteuid() == 0 else None
    box = Box(Path(env.get("HG_ROOT", "/")), run_uid)
    runner = Runner(api, box)
    try:
        runner.boot()
        if runner.info["kind"] == "install":
            runner.install()
        else:
            runner.serve()
    except Exception:
        log(f"fatal: {traceback.format_exc()}")
        with contextlib.suppress(Exception):
            api.call("POST", "/bye")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
