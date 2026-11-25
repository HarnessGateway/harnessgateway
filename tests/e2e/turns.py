"""Live checks of a running installation, in real containers: what only they show (isolation, the network, the
limits, secrets on the wire). Standard library only.

    HG_URL=http://127.0.0.1:8000 HG_KEY=hg_... python3 tests/e2e/turns.py

It adds a provider, a few environments named live-… and a second copy of the `shell` package named `live-other` to
the installation, runs about thirty short turns, and removes what it added and the sessions it made.
"""
import io
import json
import os
import pathlib
import sys
import tarfile
import time
import urllib.error
import urllib.request

KEY, URL = os.environ["HG_KEY"], os.environ.get("HG_URL", "http://127.0.0.1:8000").rstrip("/")
SHELL = pathlib.Path(__file__).parent.parent.parent / "packages" / "shell"
results, sessions_made = [], set()


def call(method, path, body=None, raw=False):
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(URL + path, data=data, method=method, headers={"Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(request, timeout=900) as answer:
            status, content = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        status, content = error.code, error.read()
    return (status, content) if raw else (status, json.loads(content))


def turn(line, text, **more):
    response = call("POST", "/v1/responses", {"model": line, "input": text, **more})[1]
    sessions_made.add(response.get("metadata", {}).get("hg_session"))
    return response


def said(response):
    return "".join(part["text"] for item in response.get("output", []) if item["type"] == "message" for part in item["content"])


def reason(response):
    return response["status"], response["metadata"].get("hg_reason")


def check(name, passed, detail=""):
    results.append(bool(passed))
    print(("PASS  " if passed else "FAIL  ") + name + (f"   {detail}" if detail else ""), flush=True)


def wait(response_id, seconds=60):
    for _ in range(seconds):
        response = call("GET", f"/v1/responses/{response_id}")[1]
        if response["status"] not in ("queued", "in_progress"):
            break
        time.sleep(1)
    return response


def turns_of(response):
    return call("GET", f"/v1/sessions/{response['metadata']['hg_session']}/turns")[1]["data"]


def files_of(response):
    container = "cntr_" + response["metadata"]["hg_session"][5:]
    return container, {file["path"]: file for file in call("GET", f"/v1/containers/{container}/files?limit=100")[1]["data"]}


def content(response, path):
    container, files = files_of(response)
    return call("GET", f"/v1/containers/{container}/files/{files[path]['id']}/content", raw=True)[1] if path in files else None


print("platform:", call("GET", "/v1/platform")[1])
status, made = call("PUT", "/v1/providers/live-check", {"apis": {"anthropic": "https://api.example.com"}, "key": "sk-not-a-real-key"})
check("a provider is added and its key is never shown", status == 200 and made["secrets_set"] == ["key"] and "sk-not" not in json.dumps(made))
archive = io.BytesIO()
with tarfile.open(fileobj=archive, mode="w:gz") as tar:
    tar.add(SHELL, arcname=".")
status, made = call("PUT", "/v1/harnesses/live-other", archive.getvalue())        # the shell package, as a harness of the user's own
check("a package a user brings becomes a harness", status == 200 and made["name"] == "live-other" and len(made["digest"]) == 64, made.get("digest", made)[:12] if status == 200 else made)
status, kept = call("PUT", "/v1/harnesses/shell", archive.getvalue())
check("a package that ships is the platform's, and is not changed in place", status == 400 and "ships with the platform" in kept["error"]["message"], kept["error"]["message"])
base = {"image": "node:22", "harnesses": ["shell"]}
for name, more in (("live", {"harnesses": ["shell", "live-other"]}), ("live-py", {"image": "python:3.13-slim"}), ("live-tiny", {"disk_mb": 64}), ("live-quick", {"timeout_s": 5}),
                   ("live-small", {"image": "python:3.13-slim", "memory_mb": 256, "tmp_mb": 64}), ("live-closed", {"network": {"mode": "closed"}}),
                   ("live-scripted", {"variables": {"TEAM": "demo"}, "scripts": {"startup": "echo \"ready for $TEAM\" > READY.txt"}}),
                   ("live-broken", {"scripts": {"startup": "echo it broke; exit 3"}})):
    status, made = call("PUT", f"/v1/environments/{name}", {**base, **more})
    assert status == 200, made
status, refused = call("PUT", "/v1/environments/none", {"harnesses": ["shell"]})
check("an environment without an image is refused", status == 400 and refused["error"]["param"] == "image", refused["error"]["message"])

a = turn("live/shell/none/none", "id -u; echo hello > hello.txt; touch /x; echo $HOME $PWD; ls /etc/hg; ls /harness; ls /")
out = said(a)
check("a turn runs as user 10000 on a read-only root, with home /mind, in /work, and sees only its own folders",
      a["status"] == "completed" and out.startswith("10000") and "Read-only file system" in out and "/mind /work" in out
      and "ca.pem" in out.split() and "contract" not in out.split() and "data" not in out.split(), repr(out[:80]) + str(a.get("error") or ""))
if a["status"] != "completed":
    sys.exit(1)
session = a["metadata"]["hg_session"]
b = turn("live-py/shell/none/none", "cat hello.txt; python3 --version; node --version || echo no-node", previous_response_id=a["id"])
check("the next turn, on another image, continues the same folders", b["status"] == "completed" and said(b).startswith("hello")
      and "no-node" in said(b) and b["metadata"]["hg_session"] == session, repr(said(b)))
c = turn("live/shell/none/none", "cat hello.txt; echo fork > fork.txt; ls", previous_response_id=a["id"])
check("continuing from an earlier turn forks a new session from its snapshot", c["status"] == "completed"
      and c["metadata"]["hg_session"] != session and "hello" in said(c))
check("a session's work is read as an OpenAI container", content(b, "/work/hello.txt") == b"hello\n"
      and "/work/fork.txt" not in files_of(b)[1] and content(c, "/work/fork.txt") == b"fork\n", sorted(files_of(b)[1]))
kept = turns_of(b)
check("a session is the requests and responses it is made of", [each["response"]["id"] for each in kept] == [a["id"], b["id"]]
      and kept[1]["request"]["previous_response_id"] == a["id"] and kept[1]["response"]["output"] == b["output"])
d = turn("live-scripted/shell/none/none", "cat READY.txt")
check("an environment's startup script runs before the harness, with its variables", said(d) == "ready for demo\n", repr(said(d)))
e = turn("live-broken/shell/none/none", "echo never")
check("a startup script that fails fails the turn, and the log says how", reason(e) == ("failed", "startup_failed")
      and "the startup script exited with code 3" in turns_of(e)[0]["log"], turns_of(e)[0]["log"][-80:])

f = turn("live/shell/none/none", "for u in https://example.com https://pypi.org/simple/ ; do curl -s -o /dev/null -w \"%{http_code} \" $u; done; echo; "
         "curl -s https://example.com 2>&1 | head -c 0; curl -s -m 5 --noproxy '*' -o /dev/null https://pypi.org/simple/ || echo direct-failed")
check("the network is the proxy's: an unlisted host is refused, a registry answers, and nothing goes direct",
      said(f).startswith("000 200") and "direct-failed" in said(f), repr(said(f)))
g = turn("live-closed/shell/none/none", "curl -s -m 5 -o /dev/null https://pypi.org/simple/ || echo no-network; env | grep -c -i proxy || true")
check("a closed environment with no model to call has no network at all", "no-network" in said(g) and said(g).strip().endswith("0"), repr(said(g)))
status, _ = call("PUT", "/v1/environments/live-secret", {**base, "image": "python:3.13-slim", "network": {"allow": ["httpbin.org"]}, "secrets": {
    "DEMO_TOKEN": {"hosts": ["httpbin.org"], "value": "real-value-for-httpbin"},
    "DEMO_HEADER": {"hosts": ["httpbin.org"], "header": "X-Demo", "prefix": "Key ", "value": "injected-value"}}})
h = turn("live-secret/shell/none/none", "echo in-sandbox=$DEMO_TOKEN; python3 -c \"import urllib.request, os, ssl; "
         "print('strict', bool(ssl.create_default_context().verify_flags & ssl.VERIFY_X509_STRICT)); "
         "print(urllib.request.urlopen(urllib.request.Request('https://httpbin.org/headers', headers={'Authorization': 'Bearer ' + os.environ['DEMO_TOKEN']})).read().decode())\"")
check("a secret is a placeholder inside the sandbox and the real value on the wire, for a client that verifies strictly",
      "in-sandbox=hgs_" in said(h) and "strict True" in said(h) and "Bearer real-value-for-httpbin" in said(h) and "Key injected-value" in said(h),
      repr(said(h)[:120]) + turns_of(h)[0]["log"][-200:] if "Bearer real" not in said(h) else "")

request = urllib.request.Request(URL + "/v1/responses", headers={"Authorization": f"Bearer {KEY}"}, data=json.dumps(
    {"model": "live/shell/none/none", "input": "for i in 1 2 3; do echo $i; sleep 1; done", "stream": True}).encode())
kinds, stamps = [], []
with urllib.request.urlopen(request, timeout=120) as stream:
    for line in stream:
        if line.startswith(b"event: "):
            kinds.append(line[7:].strip().decode())
            stamps.append(time.time())
        elif line.startswith(b"data: ") and kinds[-1] == "response.created":
            sessions_made.add(json.loads(line[6:])["response"]["metadata"]["hg_session"])
deltas = [stamp for kind, stamp in zip(kinds, stamps, strict=True) if kind == "response.output_text.delta"]
check("a stream gives events as they happen", kinds[0] == "response.created" and kinds[-1] == "response.completed"
      and len(deltas) == 3 and deltas[-1] - deltas[0] > 1.5, f"{len(kinds)} events, text over {deltas[-1] - deltas[0]:.1f} s")
i = turn("live/shell/none/none", "echo started > partial.txt; echo begun; sleep 120", background=True)
time.sleep(6)
call("POST", f"/v1/responses/{i['id']}/cancel")
i = wait(i["id"])
check("a cancelled turn stops and keeps what it did", i["status"] == "cancelled" and content(i, "/work/partial.txt") == b"started\n", i["status"])
busy = turn("live/shell/none/none", "sleep 8", background=True)
status, second = call("POST", "/v1/responses", {"model": "live/shell/none/none", "input": "echo x", "previous_response_id": busy["id"]})
check("a session with a turn in progress refuses another", status == 409, second.get("error", {}).get("message"))
j = turn("live-quick/shell/none/none", "echo before > t.txt; sleep 60")
check("a turn past its time limit is stopped and saved", reason(j) == ("failed", "timed_out") and content(j, "/work/t.txt") == b"before\n", j["error"]["message"])
k = turn("live-tiny/shell/none/none", "dd if=/dev/zero of=big bs=1M count=300 2>&1 | tail -1; sleep 60")
check("a session past its disk limit is stopped", reason(k) == ("failed", "out_of_disk"), k["error"]["message"])
m = turn("live-small/shell/none/none", "python3 -c \"x = bytearray(1 << 30); print(len(x))\"; sleep 1")
check("a turn past its memory limit says so", reason(m) == ("failed", "out_of_memory"), m["error"]["message"])
n = turn("live/shell/none/none", "exit 3")
check("a harness that reports failure is a failed response with its code", (n["status"], n["metadata"].get("hg_code")) == ("failed", "command_failed"), n["error"]["message"])
status, o = call("POST", "/v1/responses", {"model": "live/shell/no-such-provider/a-model", "input": "x"})
check("a line that names what is not there is refused before anything starts", status == 400 and o["error"]["param"] == "model", o["error"]["message"])
o = turn("live/shell/live-check/a-model", "echo $HG_PROVIDER $HG_MODEL $HG_PROVIDER_ANTHROPIC; echo ${HG_PROVIDER_OPENAI_CHAT:-unset}")
check("a harness is told its provider's base URL for each API the provider speaks, and which to use is its own to say",
      said(o) == "live-check a-model https://api.example.com\nunset\n", repr(said(o)))
p = turn("live/shell/none/none", "echo gone", store=False)
check("a response that is not to be stored is gone once it has answered", p["status"] == "completed" and call("GET", f"/v1/responses/{p['id']}")[0] == 404)
head = call("GET", f"/v1/sessions/{session}")[1]["head_response_id"]
p = turn("live/shell/none/none", "ls; echo more > more.txt", store=False, previous_response_id=a["id"])
check("a response that is not stored starts from the response it names, and leaves that session as it was",
      "hello.txt" in said(p) and call("GET", f"/v1/responses/{p['id']}")[0] == 404
      and call("GET", f"/v1/sessions/{session}")[1]["head_response_id"] == head and "/work/more.txt" not in files_of(a)[1], repr(said(p)))
q = turn("live/shell/none/none", "echo x", temperature=0.2)
check("a request a harness cannot honour fails, naming the field: nothing is ignored unsaid",
      (q["status"], q["metadata"].get("hg_code")) == ("failed", "unsupported_parameter") and "temperature" in q["error"]["message"], q.get("error"))
r = turn("live/shell/none/none", "echo the code is HERON > /dev/null; echo noted")
taken = turn("live/live-other/none/none", "cat $HG_TURN/handover.txt; echo ----; cat /mind/.harnessgateway/shell/commands.jsonl", previous_response_id=r["id"])
told, _, read = said(taken).partition("----")
check("a harness that takes a session over is told who worked on it and where their memory is, and nothing that was said",
      told.startswith("Handover. ") and f"1. shell, as live/shell/none/none: {r['id']}\n" in told
      and "Its memory: /mind/.harnessgateway/shell/commands.jsonl: one JSON line" in told and "HERON" not in told, repr(told[-200:]))
check("and what was said is there for it to read, in the mind the two share", "the code is HERON" in read, repr(read[:120]))
same = turn("live/live-other/none/none", "wc -c < $HG_TURN/handover.txt", previous_response_id=taken["id"])
back = turn("live/shell/none/none", "tail -2 $HG_TURN/handover.txt", previous_response_id=same["id"])
check("there is no handover when the turn before was the harness's own, and one when a harness returns to a session",
      said(same).strip() == "0" and f"2. live-other, as live/live-other/none/none: {taken['id']}, {same['id']}" in said(back), repr(said(back)))
status, items = call("GET", f"/v1/responses/{a['id']}/input_items")
check("a response's input is listed, and a response can be deleted", status == 200 and items["data"][0]["content"][0]["type"] == "input_text"
      and call("DELETE", f"/v1/responses/{n['id']}")[1].get("deleted") and call("GET", f"/v1/responses/{n['id']}")[0] == 404)
time.sleep(3)
usage = call("GET", "/v1/usage")[1]
check("usage is the organization's processor time, memory, network and storage", usage["cpu_seconds"] > 0 and usage["memory_byte_seconds"] > 0
      and usage["network_out_bytes"] > 0 and usage["network_in_bytes"] > 0 and usage["storage_bytes"] > 0, usage)
sessions = call("GET", "/v1/sessions?limit=5")[1]
check("sessions are listed, most recent first, in pages", len(sessions["data"]) == 5 and sessions["has_more"] and "/shell/" in sessions["data"][0]["model"])
for session in sessions_made - {None}:
    call("DELETE", f"/v1/sessions/{session}")
for environment in call("GET", "/v1/environments?limit=100")[1]["data"]:
    if environment["name"].startswith("live"):
        call("DELETE", f"/v1/environments/{environment['name']}")
call("DELETE", "/v1/harnesses/live-other")
call("DELETE", "/v1/providers/live-check")
print(f"{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
