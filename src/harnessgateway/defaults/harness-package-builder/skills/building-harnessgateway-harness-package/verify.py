#!/usr/bin/env python3
"""Try a harness package where it is being written, the way the platform would run it.

    trial.py install <folder>              lay the package out as an install finds it, and run its `install`
    trial.py turn <folder> "<prompt>"      run one turn of it, with this session's provider
        --fresh             a new session: an empty mind and an empty work folder
        --stop-after S      stop the turn after S seconds, the way a cancelled turn is stopped
        --set NAME=VALUE    a setting, or any other variable the turn should be given (repeatable)

Both stand in for /harness, /mind, /work and /turn with folders under $HG_WORK/.trial/<name>/, which stay between
turns of this session: a second `turn` continues the first. A turn's lines are held to what the platform takes of
a `run`; the platform itself also checks each event against the OpenAI SDK's types, which the kit's calls satisfy.

Python 3.9 or later, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# What every sandbox is given whatever runs in it, and what a turn is told about its provider: this session's own.
PASSED = ("PATH", "LANG", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy",
          "NODE_USE_ENV_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
          "NODE_EXTRA_CA_CERTS", "PIP_CERT", "HG_CONTRACT")
PROVIDED = ("HG_ENVIRONMENT", "HG_PROVIDER", "HG_PROVIDER_API", "HG_PROVIDER_URL", "HG_PROVIDER_KEY", "HG_MODEL")
TERMINAL = ("hg.done", "hg.incomplete", "hg.failed")
ENVELOPE = ("response.created", "response.queued", "response.in_progress", "response.completed", "response.failed",
            "response.incomplete")


def fail(message: str) -> None:
    sys.exit(f"trial: {message}")


def manifest_of(package: Path) -> dict[str, Any]:
    for program in ("install", "run"):
        if not os.access(package / program, os.X_OK):
            fail(f"{package} has no executable {program}")
    try:
        manifest: Any = json.loads((package / "harness.json").read_text())
    except (OSError, ValueError) as error:
        fail(f"{package}/harness.json cannot be read: {error}")
    if not isinstance(manifest, dict) or not all(name in manifest for name in ("contract", "name", "version", "apis")):
        fail("harness.json needs contract, name, version and apis")
    return dict(manifest)


def laid_out(package: Path, manifest: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    """The trial's folders for a package, with the package's files as they now are, and the variables both of
    its programs are given."""
    root = Path(os.environ["HG_WORK"]) / ".trial" / str(manifest["name"])
    harness = root / "harness"
    shutil.copytree(package, harness, dirs_exist_ok=True)              # what an install added stays beside them
    shutil.copytree(Path(os.environ["HG_HARNESS"]) / "kit", harness / "kit", dirs_exist_ok=True)
    env = {name: os.environ[name] for name in PASSED if name in os.environ}
    env.update(HG_HARNESS=str(harness), HG_HARNESS_NAME=str(manifest["name"]),
               HG_HARNESS_VERSION=str(manifest["version"]))
    return root, env


def install(package: Path) -> int:
    manifest = manifest_of(package)
    shutil.rmtree(Path(os.environ["HG_WORK"]) / ".trial" / str(manifest["name"]) / "harness", ignore_errors=True)
    root, env = laid_out(package, manifest)
    code = subprocess.run([str(root / "harness" / "install")], cwd=root / "harness",
                          env={**env, "HOME": "/tmp/home"}, check=False).returncode
    print(f"\ntrial: install exited with code {code}: " + ("it is ready." if code == 0 else "the install FAILED."))
    return code


def turn(package: Path, prompt: str, fresh: bool, stop_after: float | None, given: list[str]) -> int:
    manifest = manifest_of(package)
    root, env = laid_out(package, manifest)
    if not (root / "harness" / "tools").exists() and not fresh and not (root / "mind").exists():
        print("trial: note: this package has not been installed here (trial.py install); running it as it is")
    for folder in ("mind", "work"):
        if fresh:
            shutil.rmtree(root / folder, ignore_errors=True)
        (root / folder).mkdir(parents=True, exist_ok=True)
    apis, settings = manifest["apis"], manifest.get("settings") or {}
    if apis and os.environ.get("HG_PROVIDER_API") not in apis:
        print(f"trial: note: this session's provider speaks {os.environ.get('HG_PROVIDER_API') or 'no API'}, and the "
              f"package speaks {', '.join(map(str, apis))}: its CLI cannot reach a model here")
    env.update({name: str(declared["default"]) for name, declared in settings.items()
                if declared.get("default") is not None})
    env.update({name: os.environ[name] for name in PROVIDED if name in os.environ} if apis else {})
    env.update(HOME=str(root / "mind"), HG_MIND=str(root / "mind"), HG_WORK=str(root / "work"),
               HG_TURN=str(root / "turn"), HG_STATE=str(root / "mind" / ".harnessgateway" / str(manifest["name"])),
               HG_SESSION="sess_trial", HG_RESPONSE="resp_trial", HG_HANDOVER="")
    for pair in given:
        name, _, env[name] = pair.partition("=")
    (root / "turn").mkdir(exist_ok=True)
    model = f"trial/{manifest['name']}/{env.get('HG_PROVIDER') or 'none'}/{env.get('HG_MODEL') or 'none'}"
    for name, content in (("request.json", json.dumps({"model": model, "input": prompt})), ("history.json", "[]"),
                          ("handover.txt", "")):
        (root / "turn" / name).write_text(content)

    process = subprocess.Popen([str(root / "harness" / "run")], cwd=root / "work", env=env, stdout=subprocess.PIPE,
                               text=True)
    overdue = threading.Event()

    def end() -> None:
        overdue.set()
        process.kill()

    def stop() -> None:
        """Tell it to stop, as the platform does, and end it if it has not ended within its grace."""
        process.terminate()
        last = threading.Timer(float(manifest.get("run", {}).get("stop_grace_s", 10)), end)
        last.daemon = True
        last.start()
    stopper = threading.Timer(stop_after, stop) if stop_after is not None else None
    if stopper:
        stopper.daemon = True
        stopper.start()
    assert process.stdout is not None
    wrong: list[str] = []
    ends: list[dict[str, Any]] = []
    said = ""
    for number, line in enumerate(process.stdout, 1):
        try:
            event = json.loads(line)
            kind = event["type"] if isinstance(event, dict) else None
        except (ValueError, KeyError):
            kind = None
        if not isinstance(kind, str) or not (kind.startswith("response.") or kind == "hg.log" or kind in TERMINAL):
            wrong.append(f"line {number} is not a Responses event, a log line or a terminal line: {line.strip()[:160]}")
        elif kind in ENVELOPE or "sequence_number" in event:
            wrong.append(f"line {number} is the platform's to print, not a package's: {kind}")
        elif ends:
            wrong.append(f"line {number} comes after the terminal line: {kind}")
        elif kind in TERMINAL:
            ends.append(event)
        elif kind == "response.output_text.delta":
            said += str(event.get("delta", ""))
        elif kind == "response.output_item.done" and event.get("item", {}).get("type") != "message":
            item = event["item"]
            did = item.get("action") or item.get("operation") or item.get("name") or ""
            print(f"  [{item.get('type')}] {json.dumps(did)[:200]}")
        elif kind == "hg.log":
            print(f"  [log] {str(event.get('text'))[:300]}")
    code = process.wait()
    if stopper:
        stopper.cancel()
    stopped = stopper is not None and not ends
    if overdue.is_set():
        wrong.append("it did not end within its stop grace after it was told to stop")
    elif code != 0:
        wrong.append(f"run exited with code {code}: it exits 0 however the turn ended")
    if not ends and not stopped:
        wrong.append("it printed no terminal line")
    print(f"\nsaid: {said.strip()[:2000] or '(nothing)'}")
    print(f"end:  {json.dumps(ends[0]) if ends else 'stopped, with no terminal line, as a stopped turn is'}")
    print("trial: " + ("every line is one the platform takes." if not wrong else "PROBLEMS:\n  " + "\n  ".join(wrong)))
    return 1 if wrong else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install").add_argument("folder", type=Path)
    one = commands.add_parser("turn")
    one.add_argument("folder", type=Path)
    one.add_argument("prompt")
    one.add_argument("--fresh", action="store_true")
    one.add_argument("--stop-after", type=float)
    one.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    args = parser.parse_args()
    if args.command == "install":
        return install(args.folder.resolve())
    return turn(args.folder.resolve(), args.prompt, args.fresh, args.stop_after, args.set)


if __name__ == "__main__":
    sys.exit(main())
