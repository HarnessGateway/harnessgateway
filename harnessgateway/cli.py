"""`hg`: the command line for a member of an organization. It talks to the platform's HTTP API with an
API key, and runs a harness package on this machine while you develop one.

    hg login URL KEY                 remember where the platform is and who you are (~/.config/hg/config.json)
    hg me                            who you are, your organization, your teams
    hg providers [put NAME --url U --api A --key K | rm NAME]
    hg catalog [NAME]                the shipped packages
    hg harnesses                     the harnesses you can use
    hg harness new NAME --catalog P | --fork H | --dir DIR [--team T]
    hg harness pull NAME DIR         the package's files into DIR
    hg harness push NAME DIR [-m MSG]   DIR as the next commit (a new harness when none)
    hg harness log NAME | rm NAME
    hg harness run DIR [--input TEXT | --request FILE] [--state DIR] [--env K=V]
                                     run request.sh and response.sh here, as the platform would, and
                                     print the response; no sandbox, your own environment and keys
    hg ask MODEL TEXT [--stream] [--previous ID] [--file PATH]...   POST /v1/responses
    hg response ID [--detail | --request]
    hg sessions | hg session ID [--files | --cat PATH | --trace | --archive OUT]
    hg steer SESSION TEXT | hg cancel RESPONSE
    hg rodeo TASK MODEL MODEL... [--pick HARNESS]
    hg keys | hg key new [--label L]
    hg teams | hg team put NAME MEMBER... | hg members
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

CONFIG = Path(os.environ.get("HG_CONFIG_FILE") or Path.home() / ".config" / "hg" / "config.json")


def load_config() -> dict[str, str]:
    url, key = os.environ.get("HG_URL", ""), os.environ.get("HG_KEY", "")
    saved = json.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
    return {"url": (url or saved.get("url", "")).rstrip("/"), "key": key or saved.get("key", "")}


def client() -> httpx.Client:
    c = load_config()
    if not c["url"] or not c["key"]:
        sys.exit("run `hg login URL KEY` first, or set HG_URL and HG_KEY")
    return httpx.Client(base_url=c["url"], headers={"Authorization": f"Bearer {c['key']}"}, timeout=900)


def show(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def check(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        sys.exit(f"{r.status_code}: {detail}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.content


def pack_dir(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(Path(folder).rglob("*")):
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


def table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(columns, widths)))


# commands
def login(a: argparse.Namespace) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"url": a.url.rstrip("/"), "key": a.key}))
    CONFIG.chmod(0o600)
    with httpx.Client(base_url=a.url.rstrip("/"), headers={"Authorization": f"Bearer {a.key}"}, timeout=30) as c:
        me = check(c.get("/v1/me"))
    print(f"signed in as {me['name']} ({me['role']}) of {me['org']}")


def me(a: argparse.Namespace) -> None:
    with client() as c:
        show(check(c.get("/v1/me")))


def providers(a: argparse.Namespace) -> None:
    with client() as c:
        if a.action == "put":
            show(check(c.put(f"/v1/providers/{a.name}", json={"url": a.url, "api": a.api, "key": a.key or ""})))
        elif a.action == "rm":
            show(check(c.delete(f"/v1/providers/{a.name}")))
        else:
            table(check(c.get("/v1/providers"))["data"], ["name", "api", "url"])


def catalog(a: argparse.Namespace) -> None:
    with client() as c:
        if a.name:
            show(check(c.get(f"/v1/catalog/{a.name}")))
        else:
            rows = check(c.get("/v1/catalog"))["data"]
            table([{"name": r["name"], "cli": f"{r['cli']['name']} {r['cli']['version']}".strip(), "provider": r["defaults"]["provider"],
                    "verified": ", ".join(k for k, v in r.get("verified", {}).get("checks", {}).items() if v) or "-"} for r in rows],
                  ["name", "cli", "provider", "verified"])


def harnesses(a: argparse.Namespace) -> None:
    with client() as c:
        rows = check(c.get("/v1/harnesses"))["data"]
        table([{"name": r["name"], "team": r["team"] or "(org)", "head": r["head"][:8], "cli": f"{r['cli']['name']} {r['cli']['version']}".strip(),
                "editable": "yes" if r["editable"] else "no"} for r in rows], ["name", "team", "head", "cli", "editable"])


def harness(a: argparse.Namespace) -> None:
    with client() as c:
        if a.action == "new":
            body: dict[str, Any] = {"team": a.team or ""}
            if a.catalog:
                body["catalog"] = a.catalog
            elif a.fork:
                body["fork"] = a.fork
            elif a.dir:
                body["archive"] = base64.b64encode(pack_dir(Path(a.dir))).decode()
                body["message"] = a.message or f"import of {Path(a.dir).name}"
            else:
                sys.exit("name a source: --catalog, --fork or --dir")
            h = check(c.post(f"/v1/harnesses/{a.name}", json=body))
            print(f"{h['name']} at {h['head'][:8]}: {len(h['files'])} files")
        elif a.action == "pull":
            data = check(c.get(f"/v1/harnesses/{a.name}/archive", params={"at": a.at or ""}))
            out = Path(a.dir)
            out.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar.extractall(out, filter="data")
            print(f"{a.name} -> {out}")
        elif a.action == "push":
            r = c.put(f"/v1/harnesses/{a.name}", content=pack_dir(Path(a.dir)), params={"team": a.team or ""},
                      headers={"content-type": "application/gzip", "x-commit-message": a.message or f"push from {Path(a.dir).name}"})
            h = check(r)
            print(f"{h['name']} at {h['head'][:8]}: {len(h['files'])} files")
        elif a.action == "log":
            for x in check(c.get(f"/v1/harnesses/{a.name}/commits"))["data"]:
                print(f"{x['sha'][:8]}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(x['at']))}  {x['author']:<16} {x['message'].splitlines()[0]}")
        elif a.action == "rm":
            show(check(c.delete(f"/v1/harnesses/{a.name}")))
        elif a.action == "show":
            show(check(c.get(f"/v1/harnesses/{a.name}")))


def run_local(a: argparse.Namespace) -> None:
    """request.sh then response.sh, here, as the platform's runner would, against this machine's
    environment: the package author's inner loop."""
    from . import harness as contract, wire
    from .execution import Spec
    from .runtimes.docker import TURN_SCRIPT
    from .runtimes.process import ProcessExecution, host_env

    folder = Path(a.dir).resolve()
    config = contract.validate(folder)
    if a.request:
        body = json.loads(Path(a.request).read_text())
    else:
        body = {"model": folder.name, "input": a.input or sys.stdin.read()}
    req = wire.Request.parse(body)
    state = Path(a.state).resolve() if a.state else Path(tempfile.mkdtemp(prefix="hg-run-"))
    workspace, home, rundir, st = state / "workspace", state / "home", state / "run", state / "state"
    turn_file = state / "turn"
    turn = int(turn_file.read_text()) + 1 if turn_file.is_file() else 1
    for p in (workspace, home, rundir, st, rundir / "files"):
        p.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(rundir / "files", ignore_errors=True)
    (rundir / "files").mkdir()
    for name, data in req.files.items():
        (rundir / "files" / name).write_bytes(data)
    if turn == 1 and (seed := contract.seed(folder, "workspace")):
        contract.copy_seed(seed, workspace, overwrite=False)
    if seed := contract.seed(folder, "home"):
        contract.copy_seed(seed, home, overwrite=True)
    (rundir / "request.json").write_text(json.dumps(body))
    tools = state / "tools"
    env = {**host_env(), "HG_WORKSPACE": str(workspace), "HOME": str(home) if not a.real_home else os.environ.get("HOME", str(home)),
           "HG_DIR": str(rundir), "HG_STATE": str(st), "HG_FILES": str(rundir / "files"), "HG_REQUEST": str(rundir / "request.json"),
           "HG_SESSION": f"sess_{state.name}", "HG_RESPONSE": f"resp_{turn:032d}", "HG_TURN": str(turn),
           "HG_PREVIOUS": req.previous or "", "HG_MODEL": req.model or config.model, "HG_HARNESS_DIR": str(folder),
           "HG_TOOLS": str(tools), "HG_INTERRUPT": config.interrupt, "HG_CONTRACT": str(contract.CONTRACT),
           "HG_PROVIDER_URL": os.environ.get("HG_PROVIDER_URL", ""), "HG_PROVIDER_API": os.environ.get("HG_PROVIDER_API", ""),
           "HG_PROVIDER_KEY_FILE": ""}
    env["PATH"] = f"{tools}/bin:{env.get('PATH', os.defpath)}"
    for kv in a.env or []:
        k, _, v = kv.partition("=")
        env[k] = v
    for name, var in ((config.key_env, "HG_PROVIDER_KEY"), (config.url_env, "HG_PROVIDER_URL")):
        if name and os.environ.get(var):
            env[name] = os.environ[var]
    if a.install and not (tools / "bin").is_dir():
        tools.mkdir(parents=True, exist_ok=True)
        print(f"install.sh -> {tools}", file=sys.stderr)
        code = os.spawnvpe(os.P_WAIT, "bash", ["bash", str(folder / "install.sh")], {**env, "HG_TOOLS": str(tools)})
        if code != 0:
            sys.exit(f"install.sh exited {code}")

    async def go() -> int:
        ex = await ProcessExecution.spawn(["bash", str(TURN_SCRIPT)], env=env, cwd=workspace, input=json.dumps(body).encode(),
                                          interactive=False, timeout_s=a.timeout, stop_signal=15)
        out = b""
        async for e in ex.events():
            if e.stream == "stdout":
                out += e.data
                if a.verbose:
                    sys.stderr.write(e.data.decode(errors="replace"))
            else:
                sys.stderr.write(e.data.decode(errors="replace"))
        outcome = await ex.wait()
        turn_file.write_text(str(turn))
        stdout = (rundir / "stdout").read_text(errors="replace") if (rundir / "stdout").is_file() else out.decode(errors="replace")
        events, log = wire.events_of(stdout)
        try:
            shape = wire.from_response_sh((rundir / "response").read_text()) if (folder / "response.sh").is_file() else wire.from_events(events)
        except Exception as e:
            print(f"the package's output did not validate: {e}", file=sys.stderr)
            return 2
        status = shape.status or ("completed" if outcome.ok else "failed")
        resp = wire.response(rid=env["HG_RESPONSE"], created=time.time(), status=status, model=body["model"], previous=req.previous,
                             store=True, instructions=body.get("instructions"), metadata={**req.metadata, **shape.metadata, "turn": str(turn)},
                             output=shape.output, usage=shape.usage, error=shape.error or ({"code": "server_error", "message": f"exited {outcome.code}"} if status == "failed" and not shape.error else None),
                             incomplete=shape.incomplete, completed=time.time())
        show(resp)
        print(f"-- {len(events)} events, {len(log.splitlines())} log lines, exit {outcome.code}; state in {state}", file=sys.stderr)
        return 0 if status == "completed" else 1

    sys.exit(asyncio.run(go()))


def ask(a: argparse.Namespace) -> None:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": a.text}]
    for path in a.file or []:
        p = Path(path)
        content.append({"type": "input_file", "filename": p.name, "file_data": "data:application/octet-stream;base64," + base64.b64encode(p.read_bytes()).decode()})
    body: dict[str, Any] = {"model": a.model, "input": [{"role": "user", "content": content}] if len(content) > 1 else a.text}
    if a.instructions:
        body["instructions"] = a.instructions
    if a.previous:
        body["previous_response_id"] = a.previous
    if a.effort:
        body["reasoning"] = {"effort": a.effort}
    if a.no_store:
        body["store"] = False
    with client() as c:
        if not a.stream:
            show(check(c.post("/v1/responses", json=body, timeout=900)))
            return
        with c.stream("POST", "/v1/responses", json={**body, "stream": True}, timeout=900) as r:
            if r.status_code >= 400:
                sys.exit(f"{r.status_code}: {r.read().decode()}")
            print(f"session {r.headers.get('x-session-id', '')}", file=sys.stderr)
            final = None
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                e = json.loads(line[6:])
                if e["type"] == "response.output_text.delta":
                    sys.stdout.write(e["delta"])
                    sys.stdout.flush()
                elif e["type"] == "response.output_item.done" and e["item"]["type"] != "message":
                    item = e["item"]
                    detail = item.get("action", {}).get("commands") or item.get("name") or item.get("operation", {}).get("path") or ""
                    print(f"\n[{item['type']}] {detail}", file=sys.stderr)
                elif e["type"].startswith("response.") and e.get("response", {}).get("status") in ("completed", "failed", "incomplete", "cancelled"):
                    final = e["response"]
            print()
            if final:
                print(f"-- {final['status']} {final['id']}" + (f": {final['error']['message']}" if final.get("error") else ""), file=sys.stderr)


def response(a: argparse.Namespace) -> None:
    with client() as c:
        if a.request:
            show(check(c.get(f"/v1/responses/{a.id}/request")))
        elif a.detail:
            show(check(c.get(f"/v1/responses/{a.id}/detail")))
        else:
            show(check(c.get(f"/v1/responses/{a.id}")))


def sessions(a: argparse.Namespace) -> None:
    with client() as c:
        rows = check(c.get("/v1/sessions"))["data"]
        table([{"id": s["id"], "harness": s["harness"], "status": s["status"], "turns": len(s["responses"]), "title": s["title"][:50]} for s in rows],
              ["id", "harness", "status", "turns", "title"])


def session(a: argparse.Namespace) -> None:
    with client() as c:
        if a.files:
            table(check(c.get(f"/v1/sessions/{a.id}/files"))["data"], ["path", "size"])
        elif a.cat:
            sys.stdout.buffer.write(check(c.get(f"/v1/sessions/{a.id}/files/{a.cat}")))
        elif a.trace:
            for t in check(c.get(f"/v1/sessions/{a.id}/trace"))["data"]:
                print(f"{time.strftime('%H:%M:%S', time.localtime(t['at']))}  {t['kind']:<9} {t['response'][:20]}  {json.dumps(t['body'])[:120]}")
        elif a.archive:
            Path(a.archive).write_bytes(check(c.get(f"/v1/sessions/{a.id}/archive")))
            print(a.archive)
        else:
            show(check(c.get(f"/v1/sessions/{a.id}")))


def steer(a: argparse.Namespace) -> None:
    with client() as c:
        show(check(c.post(f"/v1/sessions/{a.session}/interrupt", json={"input": a.text})))


def cancel(a: argparse.Namespace) -> None:
    with client() as c:
        show(check(c.post(f"/v1/responses/{a.response}/cancel")))


def rodeo(a: argparse.Namespace) -> None:
    with client() as c:
        if a.list:
            d = check(c.get("/v1/rodeos"))
            table(d["leaderboard"], ["harness", "runs", "wins"])
            return
        ids = []
        for model in a.models:
            r = check(c.post("/v1/responses", json={"model": model, "input": a.task, "background": True}))
            ids.append(r["id"])
        print("running " + ", ".join(ids), file=sys.stderr)
        results = []
        for rid in ids:
            while (r := check(c.get(f"/v1/responses/{rid}")))["status"] in ("queued", "in_progress"):
                time.sleep(2)
            results.append(r)
        for model, r in zip(a.models, results):
            text = "".join(p.get("text", "") for i in r["output"] if i["type"] == "message" for p in i["content"])
            print(f"== {model}: {r['status']}\n{text.strip()}\n")
        d = check(c.post("/v1/rodeos", json={"task": a.task, "entries": [{"response": rid} for rid in ids]}))
        if a.pick:
            d = check(c.patch(f"/v1/rodeos/{d['id']}", json={"winner": a.pick}))
        print(f"rodeo {d['id']} winner={d['winner'] or '(pick one: hg rodeo --pick HARNESS --id ID)'}")


def pick(a: argparse.Namespace) -> None:
    with client() as c:
        show(check(c.patch(f"/v1/rodeos/{a.id}", json={"winner": a.harness})))


def keys(a: argparse.Namespace) -> None:
    with client() as c:
        if a.action == "new":
            me_ = check(c.get("/v1/me"))
            k = check(c.post(f"/v1/members/{a.member or me_['name']}/keys", json={"label": a.label or ""}))
            print(k["secret"])
        else:
            table(check(c.get("/v1/keys"))["data"], ["id", "member", "label", "revoked"])


def members(a: argparse.Namespace) -> None:
    with client() as c:
        if a.action == "add":
            show(check(c.post("/v1/members", json={"name": a.name, "role": a.role})))
        elif a.action == "invite":
            show(check(c.post("/v1/invites", json={"email": a.name, "role": a.role})))
        else:
            table(check(c.get("/v1/members"))["data"], ["name", "role"])


def teams(a: argparse.Namespace) -> None:
    with client() as c:
        if a.action == "put":
            show(check(c.put(f"/v1/teams/{a.name}", json={"members": a.members, "title": a.title or ""})))
        elif a.action == "rm":
            show(check(c.delete(f"/v1/teams/{a.name}")))
        else:
            table([{"name": t["name"], "members": ", ".join(t["members"])} for t in check(c.get("/v1/teams"))["data"]], ["name", "members"])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="hg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("login"); s.add_argument("url"); s.add_argument("key"); s.set_defaults(run=login)
    sub.add_parser("me").set_defaults(run=me)
    s = sub.add_parser("providers"); s.add_argument("action", nargs="?", default="list", choices=["list", "put", "rm"]); s.add_argument("name", nargs="?")
    s.add_argument("--url"); s.add_argument("--api", choices=["anthropic-messages", "openai-responses", "openai-chat", "none"]); s.add_argument("--key"); s.set_defaults(run=providers)
    s = sub.add_parser("catalog"); s.add_argument("name", nargs="?"); s.set_defaults(run=catalog)
    sub.add_parser("harnesses").set_defaults(run=harnesses)
    s = sub.add_parser("harness"); s.add_argument("action", choices=["new", "pull", "push", "log", "rm", "show", "run"]); s.add_argument("name", nargs="?")
    s.add_argument("dir", nargs="?"); s.add_argument("--catalog"); s.add_argument("--fork"); s.add_argument("--dir", dest="dir_opt"); s.add_argument("--team")
    s.add_argument("-m", "--message"); s.add_argument("--at")
    s.add_argument("--input"); s.add_argument("--request"); s.add_argument("--state"); s.add_argument("--env", action="append")
    s.add_argument("--install", action="store_true", help="run install.sh into the state's tools directory first")
    s.add_argument("--real-home", action="store_true", help="run with your own HOME instead of a fresh one")
    s.add_argument("--timeout", type=float, default=1800); s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(run=lambda a: run_local(argparse.Namespace(**{**vars(a), "dir": a.name})) if a.action == "run"
                   else harness(argparse.Namespace(**{**vars(a), "dir": a.dir or a.dir_opt})))
    s = sub.add_parser("ask"); s.add_argument("model"); s.add_argument("text"); s.add_argument("--stream", action="store_true"); s.add_argument("--previous")
    s.add_argument("--file", action="append"); s.add_argument("--instructions"); s.add_argument("--effort"); s.add_argument("--no-store", action="store_true"); s.set_defaults(run=ask)
    s = sub.add_parser("response"); s.add_argument("id"); s.add_argument("--detail", action="store_true"); s.add_argument("--request", action="store_true"); s.set_defaults(run=response)
    sub.add_parser("sessions").set_defaults(run=sessions)
    s = sub.add_parser("session"); s.add_argument("id"); s.add_argument("--files", action="store_true"); s.add_argument("--cat"); s.add_argument("--trace", action="store_true")
    s.add_argument("--archive"); s.set_defaults(run=session)
    s = sub.add_parser("steer"); s.add_argument("session"); s.add_argument("text"); s.set_defaults(run=steer)
    s = sub.add_parser("cancel"); s.add_argument("response"); s.set_defaults(run=cancel)
    s = sub.add_parser("rodeo"); s.add_argument("task", nargs="?"); s.add_argument("models", nargs="*"); s.add_argument("--pick"); s.add_argument("--list", action="store_true"); s.set_defaults(run=rodeo)
    s = sub.add_parser("pick"); s.add_argument("id"); s.add_argument("harness"); s.set_defaults(run=pick)
    s = sub.add_parser("keys"); s.add_argument("action", nargs="?", default="list", choices=["list", "new"]); s.add_argument("--label"); s.add_argument("--member"); s.set_defaults(run=keys)
    s = sub.add_parser("members"); s.add_argument("action", nargs="?", default="list", choices=["list", "add", "invite"]); s.add_argument("name", nargs="?"); s.add_argument("--role", default="member"); s.set_defaults(run=members)
    s = sub.add_parser("teams"); s.add_argument("action", nargs="?", default="list", choices=["list", "put", "rm"]); s.add_argument("name", nargs="?"); s.add_argument("members", nargs="*"); s.add_argument("--title"); s.set_defaults(run=teams)
    a = p.parse_args(argv)
    a.run(a)


if __name__ == "__main__":
    main(sys.argv[1:])
