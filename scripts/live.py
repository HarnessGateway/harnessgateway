"""The three verification checks, against a running platform, for every catalog package (or the
ones named): make, recall, stream. Records the results into each package's catalog.json.

    HG_URL=https://hg.example.com HG_KEY=hg_… python scripts/live.py [--record] [--provider NAME] [NAME...]

The platform needs a provider for each package's default (or --provider names one for all). Each
package is forked as `live-<name>`, run through three turns, and the fork is deleted. --record
writes the `verified` block; without it the results are printed only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog"
MAKE = "Create a file named hello.txt containing exactly the word hello, then reply with the one word: done"
RECALL = "What is in hello.txt? Reply with its exact content and nothing else."
STREAM = "Reply with the words one two three four five, one per line."


def turn(c: httpx.Client, model: str, text: str, previous: str | None = None, stream: bool = False) -> tuple[dict, int]:
    body = {"model": model, "input": text, **({"previous_response_id": previous} if previous else {})}
    if not stream:
        r = c.post("/v1/responses", json=body, timeout=900)
        r.raise_for_status()
        return r.json(), 0
    deltas, final = 0, {}
    with c.stream("POST", "/v1/responses", json={**body, "stream": True}, timeout=900) as r:
        r.raise_for_status()
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                event, buf = buf.split("\n\n", 1)
                line = next((l for l in event.split("\n") if l.startswith("data: ")), None)
                if not line:
                    continue
                e = json.loads(line[6:])
                if e["type"] == "response.output_text.delta":
                    deltas += 1
                if e.get("response", {}).get("status") in ("completed", "failed", "incomplete", "cancelled"):
                    final = e["response"]
    return final, deltas


def text_of(r: dict) -> str:
    return "".join(c.get("text", "") for i in r.get("output", []) if i.get("type") == "message" for c in i.get("content", []))


def check(c: httpx.Client, name: str, provider: str | None) -> dict:
    fork = f"live-{name}"
    c.delete(f"/v1/harnesses/{fork}")
    r = c.post(f"/v1/harnesses/{fork}", json={"catalog": name})
    r.raise_for_status()
    model = f"{fork}/{provider}" if provider else fork
    results = {"make": False, "recall": False, "stream": False}
    try:
        first, _ = turn(c, model, MAKE)
        sid = first["metadata"]["session_id"]
        files = {f["path"] for f in c.get(f"/v1/sessions/{sid}/files").json()["data"]}
        results["make"] = first["status"] == "completed" and "hello.txt" in files
        print(f"  make: {first['status']} files={sorted(files)} text={text_of(first)[:60]!r}")
        second, _ = turn(c, model, RECALL, previous=first["id"])
        results["recall"] = second["status"] == "completed" and "hello" in text_of(second).lower()
        print(f"  recall: {second['status']} text={text_of(second)[:60]!r}")
        third, deltas = turn(c, model, STREAM, stream=True)
        results["stream"] = third.get("status") == "completed" and deltas >= 2
        print(f"  stream: {third.get('status')} deltas={deltas}")
    except Exception as e:                                             # noqa: BLE001 - a failed check is a result
        print(f"  error: {e}")
    finally:
        c.delete(f"/v1/harnesses/{fork}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--provider")
    a = ap.parse_args()
    url, key = os.environ.get("HG_URL", ""), os.environ.get("HG_KEY", "")
    if not url or not key:
        sys.exit("set HG_URL and HG_KEY")
    c = httpx.Client(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, timeout=120)
    names = a.names or sorted(p.name for p in CATALOG.iterdir() if (p / "catalog.json").is_file())
    for name in names:
        print(name)
        meta_path = CATALOG / name / "catalog.json"
        meta = json.loads(meta_path.read_text())
        cfg = json.loads((CATALOG / name / "harness.json").read_text())
        provider = a.provider or cfg["defaults"].get("provider") or ""
        results = check(c, name, a.provider)
        if a.record:
            meta["verified"] = {"on": time.strftime("%Y-%m-%d"), "contract": 7, "provider": provider, "model": cfg["defaults"].get("model", ""),
                                "checks": results, "how": "scripts/live.py through the platform: three turns against the named provider"}
            meta_path.write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")
    print("done; run scripts/catalog_docs.py to refresh the pages" if a.record else "done")


if __name__ == "__main__":
    main()
