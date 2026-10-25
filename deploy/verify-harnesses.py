"""Confirm a deployment end to end, the way a pilot organization meets it: an invite, sign-up, providers, the built-in
harnesses installed, then eight checks per harness through the OpenAI SDK.

    HG_API=https://api.example.com HG_OPERATOR_KEY=... DEEPSEEK_API_KEY=... OPENAI_API_KEY=... \
        python deploy/verify-harnesses.py [claude-code codex hermes pi]

Checks: 1 create and run a file, 2 recall it on the next turn, 3 streaming, 4 instructions, 5 usage, 6 cancel,
7 a provider error, 8 traces (harness and provider exchanges). Run on a node host, check 6 also confirms the sandbox
has nothing left running. Prints one PASS/FAIL line per harness and exits non-zero on any failure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import httpx
from openai import OpenAI

API = os.environ.get("HG_API", "http://127.0.0.1:8000").rstrip("/")
OPERATOR = {"authorization": f"Bearer {os.environ['HG_OPERATOR_KEY']}"}
MODEL = {"claude-code": "deepseek/deepseek-flash", "codex": "openai/gpt-5.4-mini",
         "hermes": "deepseek/deepseek-flash", "pi": "deepseek/deepseek-flash"}
PROVIDERS = (("deepseek", "anthropic", "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY"),
             ("openai", "openai", "https://api.openai.com", "OPENAI_API_KEY"))
LOCAL_DOCKER = shutil.which("docker") is not None


def ok(r: httpx.Response) -> dict:
    assert r.status_code < 300, f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:400]}"
    return r.json()


def texts(r) -> str:
    return " ".join(c.text for i in r.output if i.type == "message" for c in i.content if c.type == "output_text")


def leftovers(session_id: str) -> list[str]:
    """Processes still running in the session's sandbox, besides its init and keeper (only on a node host)."""
    if not LOCAL_DOCKER:
        return []
    out = subprocess.run(["docker", "exec", "hg-s-" + session_id.split("_", 1)[1], "ps", "-eo", "args="],
                         capture_output=True, text=True).stdout
    return [x for x in out.splitlines() if x and not x.startswith(("ps ", "/usr/bin/tini", "sleep infinity"))]


def organization(http: httpx.Client) -> str:
    invite = ok(http.post("/admin/v1/invites", headers=OPERATOR,
                          json={"label": "verify-harnesses", "uses": 1, "limits": {"observability": True}}))
    signup = ok(http.post("/v1/signup", json={"invite_code": invite["code"], "organization": f"verify-{int(time.time())}",
                                              "email": "verify@example.com"}))
    key = signup["api_key"]
    for name, api, url, env in PROVIDERS:
        if os.environ.get(env):
            ok(http.put(f"/v1/providers/{name}", headers={"authorization": f"Bearer {key}"},
                        json={"api": api, "base_url": url, "key": os.environ[env]}))
    print("organization", signup["organization"]["id"])
    return key


def verify(client: OpenAI, http: httpx.Client, key: str, harness: str) -> dict[str, bool]:
    m = f"{harness}/{MODEL[harness]}"
    res: dict[str, bool] = {}
    print(f"\n=== {m}")
    r1 = client.responses.create(model=m, input=f"Create hello.py that prints 'hello from {harness}', run it with "
                                                "python3, and tell me exactly what it printed.")
    sid = r1.metadata["session_id"]
    files = [f.path for f in client.containers.files.list(sid)]
    res["create+run"] = r1.status == "completed" and "/workspace/hello.py" in files and f"hello from {harness}" in texts(r1)
    print(r1.id, r1.status, [i.type for i in r1.output], files)

    r2 = client.responses.create(model=m, previous_response_id=r1.id,
                                 input="Without running anything, what file did you create and what did it print?")
    res["recall"] = r2.status == "completed" and "hello.py" in texts(r2) and r2.metadata["session_id"] == sid
    print(r2.id, r2.status, repr(texts(r2)[:120]))

    deltas = 0
    with client.responses.stream(model=m, instructions="Always answer in French, in one sentence.",
                                 input="What is the capital of Japan?") as s:
        for ev in s:
            deltas += ev.type == "response.output_text.delta"
        r3 = s.get_final_response()
    res["streaming"] = deltas > 0
    res["instructions"] = r3.status == "completed" and "Tokyo" in texts(r3) and "capitale" in texts(r3).lower()
    print(r3.id, r3.status, f"{deltas} deltas", repr(texts(r3)[:120]))

    res["usage"] = all(x.usage and x.usage.input_tokens > 0 and x.usage.output_tokens > 0 for x in (r1, r2, r3))

    bg = client.responses.create(model=m, background=True, previous_response_id=r1.id,
                                 input="Run this exact shell command and wait for it: sleep 300; echo done")
    for _ in range(240):
        if client.responses.retrieve(bg.id).status == "in_progress":
            break
        time.sleep(0.5)
    time.sleep(10)                                            # let the harness start its tool
    started = time.time()
    client.responses.cancel(bg.id)
    while (cur := client.responses.retrieve(bg.id)).status in ("queued", "in_progress"):
        time.sleep(0.25)
    took = time.time() - started
    time.sleep(1)
    left = leftovers(sid)
    res["cancel"] = cur.status == "cancelled" and not left
    print(bg.id, cur.status, f"after {took:.1f}s", f"left running: {left}" if LOCAL_DOCKER else "")

    bad = client.responses.create(model=f"{harness}/{MODEL[harness].split('/')[0]}/no-such-model", input="hi")
    res["provider error"] = bad.status == "failed" and bad.error is not None
    print(bad.id, bad.status, bad.metadata.get("hg_error_code"))

    trace = ok(http.get(f"/v1/traces/{r1.id}", headers={"authorization": f"Bearer {key}"}))
    res["traces"] = bool(trace["harness"]["events"]) and bool(trace["provider"])
    print("trace:", len(trace["harness"]["events"]), "events,", len(trace["provider"]), "provider exchanges")
    return res


def main() -> int:
    harnesses = sys.argv[1:] or list(MODEL)
    http = httpx.Client(base_url=API, timeout=1200)
    key = organization(http)
    for h in harnesses:
        ok(http.post("/v1/harnesses", headers={"authorization": f"Bearer {key}"}, json={"builtin": h}))
    client = OpenAI(base_url=f"{API}/v1", api_key=key, timeout=1200)
    results = {h: verify(client, http, key, h) for h in harnesses}
    usage = ok(http.get("/v1/usage", headers={"authorization": f"Bearer {key}"}))
    print("\nusage:", json.dumps({k: usage[k] for k in ("markup", "cost_usd", "charge_usd")}))
    print()
    for h, res in results.items():
        print(f"{h:12s}", " ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in res.items()))
    return 0 if all(all(r.values()) for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
