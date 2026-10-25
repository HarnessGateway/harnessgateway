"""The minimal harness: the Responses request in, one streamed model call, Responses events out (via hgkit)."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from hgkit import Emitter, Request, State, settings

req, out, state = Request.load(), Emitter(), State()
history = json.loads(state.get("history", "[]"))                  # the conversation, kept with the session
history.append({"role": "user", "content": req.prompt})
agents_md = Path("AGENTS.md").read_text() if Path("AGENTS.md").exists() else ""
system = "\n".join(x for x in (settings().get("PERSONA", ""), agents_md, req.instructions) if x)
api, base, token, model = (os.environ[k] for k in ("HG_PROVIDER_API", "HG_PROVIDER_URL", "HG_PROVIDER_TOKEN", "HG_MODEL"))

if api == "anthropic":
    url, headers = f"{base}/v1/messages", {"x-api-key": token, "anthropic-version": "2023-06-01"}
    body = {"model": model, "max_tokens": 4096, "system": system, "messages": history, "stream": True}
else:
    url, headers = f"{base}/v1/chat/completions", {"authorization": f"Bearer {token}"}
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True},
            "messages": ([{"role": "system", "content": system}] if system else []) + history}

text, usage = "", Emitter.usage()
try:
    request = urllib.request.Request(url, json.dumps(body).encode(), {**headers, "content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as stream:
        for raw in stream:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ev = json.loads(line[5:])
            if api == "anthropic":
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    text += ev["delta"]["text"]
                    out.text(ev["delta"]["text"])
                elif ev.get("type") == "message_start":
                    u = ev["message"]["usage"]
                    cached = u.get("cache_read_input_tokens", 0)
                    usage = Emitter.usage(u["input_tokens"] + cached, u.get("output_tokens", 0), cached=cached)
                elif ev.get("type") == "message_delta":
                    usage = Emitter.usage(usage["input_tokens"], ev["usage"]["output_tokens"],
                                          cached=usage["input_tokens_details"]["cached_tokens"])
            else:
                for choice in ev.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        text += delta
                        out.text(delta)
                if ev.get("usage"):
                    usage = Emitter.usage(ev["usage"]["prompt_tokens"], ev["usage"]["completion_tokens"])
except urllib.error.HTTPError as e:
    out.failed("rate_limit_exceeded" if e.code == 429 else "provider_error", f"provider {e.code}: {e.read()[:500]!r}")
    raise SystemExit(0) from None
history.append({"role": "assistant", "content": text})
state.set("history", json.dumps(history))
out.completed(usage=usage)
