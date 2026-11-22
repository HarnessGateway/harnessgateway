#!/usr/bin/env python3
"""Verify a harness package where it is being written, the way the platform would run it.

    verify.py <folder>                      the check every package must pass: its `install`, then a hello-world
                                            turn in a new session, on this session's provider
    verify.py <folder> --say "<prompt>"     one more turn in the same session: to try resuming, a command, a file
        --request FILE      the Responses request to send, in place of one made from the prompt: any field, any input
        --new               in a new session: nothing a turn before it left (a cache, a config, a session) is there
        --stop-after S      stop that turn after S seconds, the way a cancelled turn is stopped
        --set NAME=VALUE    a setting, or any other variable the turn should be given (repeatable)
    verify.py --shape <type>                what a line of that type holds: an output item, or a stream event

It stands in for /harness, /mind, /work and /turn with folders under $HG_WORK/.trial/<name>/, which stay between
turns of this session. Every line a turn prints is held to the schema the platform checks a `run` by
($HG_CONTRACT/lines.schema.json), and to the order the platform takes lines in. Each turn is given, as
`previous.json`, the response the trial's turn before it would have been.

Python 3.9 or later, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
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
CONTRACT = Path(os.environ["HG_CONTRACT"])         # the contract, in words and as schemas: every sandbox is given it
ENDINGS = ("response.completed", "response.incomplete", "response.failed")
PLATFORMS = ("response.created", "response.queued", "response.in_progress", "error")
ADDED, DONE = "response.output_item.added", "response.output_item.done"
HELLO = "Say hello in one short sentence."
TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict,
    "null": type(None)}


def fail(message: str) -> None:
    sys.exit(f"verify: {message}")


class Schema:
    """The schema of a line a `run` may print, as the platform wrote it."""

    def __init__(self) -> None:
        self._all = json.loads((CONTRACT / "lines.schema.json").read_text())
        self.lines = {self._kind(option): option for option in self._all["anyOf"]}

    def _plain(self, schema: dict[str, Any]) -> dict[str, Any]:
        return self._all["$defs"][schema["$ref"].rpartition("/")[2]] if "$ref" in schema else schema

    def _kind(self, schema: dict[str, Any]) -> Any:
        """The `type` a schema is of, when it is of one."""
        return self._plain(schema).get("properties", {}).get("type", {}).get("const")

    def shape(self, kind: str) -> dict[str, Any]:
        return self._plain(self.lines[kind])

    def wrong(self, value: Any, schema: dict[str, Any], at: str = "") -> str:
        """What is wrong with a value, by a schema; nothing when it fits. Of several things it may be, the fault
        told is that of the one it says it is."""
        schema = self._plain(schema)
        if "anyOf" in schema:
            faults = [self.wrong(value, option, at) for option in schema["anyOf"]]
            its = [faults[place] for place, option in enumerate(schema["anyOf"])
                   if isinstance(value, dict) and value.get("type") is not None and self._kind(option) == value["type"]]
            return "" if "" in faults else (its or faults)[0]
        kind = schema.get("type")
        if "const" in schema and value != schema["const"]:
            return f"{at or 'it'} is {json.dumps(schema['const'])}"
        if "enum" in schema and value not in schema["enum"]:
            return f"{at or 'it'} is one of {', '.join(map(json.dumps, schema['enum']))}"
        if kind and (not isinstance(value, TYPES[kind]) or (isinstance(value, bool) and kind != "boolean")):
            return f"{at or 'it'} is {'an' if kind[0] in 'aio' else 'a'} {kind}"
        if isinstance(value, dict):
            for name in schema.get("required", ()):
                if name not in value:
                    return f"{at + '.' if at else ''}{name} is required"
            for name, inner in schema.get("properties", {}).items():
                if name in value and (fault := self.wrong(value[name], inner, f"{at + '.' if at else ''}{name}")):
                    return fault
        if isinstance(value, list) and "items" in schema:
            for place, each in enumerate(value):
                if fault := self.wrong(each, schema["items"], f"{at}[{place}]"):
                    return fault
        return ""


class Read:
    """What a turn printed, held to what the platform takes of a `run`: each line by the schema, and the lines
    by their order."""

    def __init__(self, schema: Schema) -> None:
        self._schema = schema
        self.wrong: list[str] = []
        self.ending: dict[str, Any] | None = None
        self.said = ""                              # the answer's text
        self.items: list[dict[str, Any]] = []       # the output items it printed whole
        self._added: dict[str, bool] = {}           # the items a stream event added, and whether each is done

    @property
    def finished(self) -> bool:
        return self.ending is not None and self.ending["type"] == ENDINGS[0]

    def line(self, number: int, text: str) -> None:
        try:
            line = json.loads(text)
            kind = line["type"] if isinstance(line, dict) else None
        except (ValueError, KeyError):
            kind = None
        if self.ending is not None:
            return self.wrong.append(f"line {number} comes after the last event: nothing is printed after it")
        if not isinstance(kind, str):
            return self.wrong.append(f"line {number} is not a JSON object with a type: {text.strip()[:160]}")
        if kind in PLATFORMS:
            return self.wrong.append(f"line {number} is the platform's to print, not a package's: {kind}")
        if kind not in self._schema.lines:
            return self.wrong.append(f"line {number}: {kind!r} is neither an output item nor a stream event of the "
                                     "Responses API")
        if fault := self._schema.wrong(line, self._schema.lines[kind]) or self._order(kind, line):
            return self.wrong.append(f"line {number}, {kind}: {fault}")
        if kind in ENDINGS:
            self.ending = line
        elif kind == "response.output_text.delta":
            self.said += line["delta"]
        elif not kind.startswith("response."):
            self.items.append(line)
            if kind == "message":
                self.said += "".join(part.get("text", "") for part in line["content"])
            elif kind != "reasoning":
                did = line.get("action") or line.get("operation") or line.get("name") or ""
                print(f"  [{kind}] {json.dumps(did)[:200]}")

    def _order(self, kind: str, line: dict[str, Any]) -> str:
        """What is wrong with where a stream event comes: it is about an item that was added, and is not done."""
        if kind in (ADDED, DONE):
            item_id = line["item"].get("id")
            if not isinstance(item_id, str):
                return "the item of a stream event has an id"
            if (item_id in self._added) == (kind == ADDED) or self._added.get(item_id):
                return f"item {item_id} is added once, and then done once"
            self._added[item_id] = kind == DONE
        elif "item_id" in line and line["item_id"] not in self._added:
            return f"it names an item that has not been added: {line['item_id']!r}"
        return ""

    def response(self, stopped: bool) -> dict[str, Any]:
        """The response this turn would have been, as far as the next turn is told of it: how it ended, and its
        output. The platform says a stopped turn was cancelled, whatever it printed."""
        said = [{"type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": self.said, "annotations": []}]}] if self.said else []
        status = "cancelled" if stopped else self.ending["type"].rpartition(".")[2] if self.ending else "failed"
        output = [{"id": f"item_{place}", **item} for place, item in enumerate([*self.items, *said])]
        return {"id": "resp_trial", "object": "response", "status": status, "output": output}


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
    if manifest["name"] != package.name:
        fail(f"the folder is {package.name} and harness.json names {manifest['name']}: a package's folder is its name")
    return dict(manifest)


def laid_out(package: Path, manifest: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    """The trial's folders for a package, with the package's files as they now are, and the variables both of its
    programs are given."""
    root = Path(os.environ["HG_WORK"]) / ".trial" / str(manifest["name"])
    harness = root / "harness"
    shutil.copytree(package, harness, dirs_exist_ok=True)              # what an install added stays beside them
    env = {name: os.environ[name] for name in PASSED if name in os.environ}
    env.update(HG_HARNESS=str(harness), HG_HARNESS_NAME=str(manifest["name"]),
               HG_HARNESS_VERSION=str(manifest["version"]))
    return root, env


def install(package: Path, manifest: dict[str, Any]) -> bool:
    shutil.rmtree(Path(os.environ["HG_WORK"]) / ".trial" / str(manifest["name"]), ignore_errors=True)
    root, env = laid_out(package, manifest)
    code = subprocess.run([str(root / "harness" / "install")], cwd=root / "harness",
                          env={**env, "HOME": "/tmp/home"}, check=False).returncode
    print(f"\nverify: install exited with code {code}" + ("." if code == 0 else ": the install FAILED."))
    return code == 0


def turn(package: Path, manifest: dict[str, Any], request: dict[str, Any], stop_after: float | None = None,
         given: tuple[str, ...] = (), new: bool = False) -> Read:
    """Run one turn. What it printed, with what is wrong with it (nothing, when the platform would take it)."""
    root, env = laid_out(package, manifest)
    for folder in ("mind", "work", "turn"):
        if new:
            shutil.rmtree(root / folder, ignore_errors=True)
        (root / folder).mkdir(parents=True, exist_ok=True)
    last = root / "last.json"                       # the response the turn before this one would have been
    settings = manifest.get("settings") or {}
    env.update({name: str(declared["default"]) for name, declared in settings.items()
                if declared.get("default") is not None})
    env.update({name: os.environ[name] for name in PROVIDED if name in os.environ} if manifest["apis"] else {})
    env.update(HOME=str(root / "mind"), HG_MIND=str(root / "mind"), HG_WORK=str(root / "work"),
               HG_TURN=str(root / "turn"), HG_STATE=str(root / "mind" / ".harnessgateway" / str(manifest["name"])),
               HG_SESSION="sess_trial", HG_RESPONSE="resp_trial")
    for pair in given:
        name, _, env[name] = pair.partition("=")
    model = f"trial/{manifest['name']}/{env.get('HG_PROVIDER') or 'none'}/{env.get('HG_MODEL') or 'none'}"
    previous = last.read_text() if last.is_file() and not new else "null"
    for name, content in (("request.json", json.dumps({"model": model, **request})), ("previous.json", previous),
                          ("history.json", "[]"), ("handover.txt", "")):
        (root / "turn" / name).write_text(content)

    # A group of its own, as a sandbox's program has: a stop is sent to it and to everything it started.
    process = subprocess.Popen([str(root / "harness" / "run")], cwd=root / "work", env=env, stdout=subprocess.PIPE,
                               text=True, start_new_session=True)
    overdue, stopped = threading.Event(), threading.Event()

    def end() -> None:
        overdue.set()
        os.killpg(process.pid, signal.SIGKILL)

    def stop() -> None:
        """Tell it to stop, as the platform does, and end it if it has not ended within its grace."""
        stopped.set()
        os.killpg(process.pid, signal.SIGTERM)
        ender = threading.Timer(float(manifest.get("run", {}).get("stop_grace_s", 10)), end)
        ender.daemon = True
        ender.start()
    stopper = threading.Timer(stop_after, stop) if stop_after is not None else None
    if stopper:
        stopper.daemon = True
        stopper.start()
    assert process.stdout is not None
    read = Read(Schema())
    for number, line in enumerate(process.stdout, 1):
        read.line(number, line)
    code = process.wait()
    if stopper:
        stopper.cancel()
    if overdue.is_set():
        read.wrong.append("it did not end within its stop grace after it was told to stop")
    elif not stopped.is_set() and code != 0:
        read.wrong.append(f"run exited with code {code}: it exits 0 however the turn ended")
    if read.ending is None and not stopped.is_set():
        read.wrong.append("it did not print the stream's last event: response.completed, .incomplete or .failed")
    last.write_text(json.dumps(read.response(stopped.is_set())))
    print(f"\nsaid: {read.said.strip()[:2000] or '(nothing)'}")
    print("end:  " + (json.dumps(read.ending) if read.ending else "none"))
    if stopped.is_set():
        print(f"      (stopped after {stop_after:g} seconds: the platform says how a stopped turn ended, whatever it "
              "printed)")
    elif read.ending and not read.finished:
        print("      (the turn did not finish: read its message, and what it printed to standard error above)")
    return read


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, nargs="?")
    parser.add_argument("--say", metavar="PROMPT")
    parser.add_argument("--request", type=Path, metavar="FILE")
    parser.add_argument("--new", action="store_true")
    parser.add_argument("--stop-after", type=float)
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--shape", metavar="TYPE")
    args = parser.parse_args()
    if args.shape is not None:
        schema = Schema()
        if args.shape not in schema.lines:
            fail(f"{args.shape!r} is not a line a run prints. It is one of: {', '.join(sorted(schema.lines))}")
        print(json.dumps(schema.shape(args.shape), indent=1))
        return 0
    if args.folder is None:
        parser.error("the package's folder is needed")
    package, given = args.folder.resolve(), tuple(args.set)
    manifest = manifest_of(package)
    label = f"{manifest['name']} {manifest['version']}"
    if args.say is not None or args.request is not None:
        request = json.loads(args.request.read_text()) if args.request else {"input": args.say}
        read = turn(package, manifest, request, args.stop_after, given, args.new)
        print("verify: " + ("every line is one the platform takes." if not read.wrong
                            else "PROBLEMS:\n  " + "\n  ".join(read.wrong)))
        return 1 if read.wrong else 0

    if not install(package, manifest):
        print(f"verify: NOT VERIFIED: {label} does not install.")
        return 1
    speaks, spoken = manifest["apis"], os.environ.get("HG_PROVIDER_API") or ""
    if speaks and spoken not in speaks:              # its CLI cannot reach a model here: say so, and guess nothing
        print(f"verify: INSTALL VERIFIED ONLY: {label} installs. This session's provider speaks {spoken or 'no API'}, "
              f"and the package speaks {', '.join(speaks)}: its hello world cannot run here, and must be run after it "
              f"is added, with a provider that speaks one of those.")
        return 2
    read = turn(package, manifest, {"input": HELLO}, given=given)
    answered = read.finished and read.said.strip()
    wrong = read.wrong or ([] if answered else ["its hello world did not finish with an answer"])
    if wrong:
        print(f"verify: NOT VERIFIED: {label}:\n  " + "\n  ".join(wrong))
        return 1
    print(f"verify: VERIFIED: {label} installs, and answered a hello world.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
