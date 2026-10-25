# Verifying a package on the platform

## Install

```bash
API=https://<your platform>          # e.g. http://localhost:8000 when self-hosted
KEY=sk-hg-...                        # an admin API key of your organization
tar czf /tmp/my-harness.tgz -C ./my-harness .
curl -s -X POST "$API/v1/harnesses" -H "Authorization: Bearer $KEY" -F package=@/tmp/my-harness.tgz
# a provider, if the harness uses one (the key is encrypted and never enters a sandbox)
curl -s -X PUT "$API/v1/providers/anthropic" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"api": "anthropic", "base_url": "https://api.anthropic.com", "key": "sk-ant-..."}'
curl -s -X PATCH "$API/v1/harnesses/my-harness" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"default_provider": "anthropic", "default_model": "claude-sonnet-4-5"}'
```

Each upload is a new digest; existing sessions keep theirs, so test with new sessions. The first turn on a new
`install.sh` installs the CLI (minutes); later turns reuse it.

## The checks (all must pass)

```python
from openai import OpenAI
c = OpenAI(base_url=f"{API}/v1", api_key=KEY)
M = "my-harness"                     # or "my-harness/<provider>/<model>"

r1 = c.responses.create(model=M, input="Create hello.py that prints 'hello', run it, and tell me what it printed.")
assert r1.status == "completed" and "hello" in r1.output_text
sid = r1.metadata["session_id"]
assert any(f.path == "/workspace/hello.py" for f in c.containers.files.list(sid).data)

r2 = c.responses.create(model=M, previous_response_id=r1.id, input="What file did you create and what did it print?")
assert "hello.py" in r2.output_text                                   # the CLI's session was resumed

deltas = [e for e in c.responses.create(model=M, input="Count to five.", stream=True)
          if e.type == "response.output_text.delta"]
assert deltas                                                         # streamed text

r4 = c.responses.create(model=M, input="Say hello.", instructions="Always answer in French.")
assert r4.status == "completed"                                       # read it: French?

assert r1.usage is not None                                           # when the CLI reports tokens

bg = c.responses.create(model=M, input="Run `sleep 300` in the shell.", background=True)
import time; time.sleep(20); c.responses.cancel(bg.id)
# poll c.responses.retrieve(bg.id).status until it is "cancelled"

bad = c.responses.create(model=f"{M}/<provider>/no-such-model", input="hi")
assert bad.status == "failed" and bad.error.message                  # a clean, useful failure
```

## Debugging

- `response.metadata.hg_reason` and `response.error.message` say why the platform ended a turn.
- `GET /v1/traces/<response_id>` (organizations with observability) returns the request the package received, every
  event, full stdout and stderr, the exit code, and each provider exchange.
- An `invalid_output` failure names the first event that did not validate and why.
