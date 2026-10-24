<p align="center"><img src="web/picky.png" width="140" alt=""></p>

# HarnessGateway

**Every coding agent, one API.** HarnessGateway speaks the [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
and nothing else. Behind it, a *harness package* maps each request onto a coding-agent CLI
(Claude Code, Codex, Pi, Hermes, Kilo, Cline, …) running in a sandbox the platform owns, with
your provider keys injected for the length of one turn. Switch harnesses by changing the model name.

```python
from openai import OpenAI
client = OpenAI(base_url="https://hg.example.com/v1", api_key="hg_…")
r = client.responses.create(model="codex", instructions="Keep patches small.",
                            input="Add a --dry-run flag to bin/deploy", stream=True)
for event in r:
    print(event.type)        # response.output_item.added, response.output_text.delta, … response.completed
```

## Deploy

```bash
curl -fsSL https://raw.githubusercontent.com/HarnessGateway/harnessgateway/main/deploy/install.sh | bash
```

One container, every role, the data on a volume, sandboxes through the Docker socket. Or by hand:

```bash
docker run -d --name harnessgateway --restart unless-stopped -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock -v hg:/data \
  -e HG_PUBLIC_URL=http://hg.example.com:8000 ghcr.io/harnessgateway/harnessgateway:7
docker logs harnessgateway         # the first boot's keys, shown once
```

`deploy/compose.yml` adds TLS with Caddy (`--profile one-box`) or a fleet with Postgres, S3 and
workers that join (`--profile ha`). The documentation is served by the platform at `/docs/`, and
the console at `/app`.

## Then

1. **Sign in** at `/app` with the owner's key from the log, or create an account when sign-up is open.
2. **Add a provider**: an endpoint and its key (Anthropic, OpenAI, DeepSeek, any compatible).
   The key is sealed on arrival; no sandbox ever sees it.
3. **Fork a harness** from the catalog, or write one. A harness is a git repository the platform
   hosts: edit it in the console and commit, or `hg harness pull`, change, `hg harness push`.
4. **Converse**: a conversation with a harness, its items streamed, the workspace's files and the
   trace beside it; steer it mid-run, cancel it, continue it from any client with `previous_response_id`.
5. **Run a rodeo**: one task across several harnesses side by side; pick the winner, keep the leaderboard.

## The division of labour

| The package owns | The platform owns |
|---|---|
| reading the request (`request.json`, verbatim) and building the CLI invocation | the wire: validation against the SDK's schema, streaming, continuation, background, steer, cancel |
| the CLI's install (`install.sh`), its seeds (`AGENTS.md`, MCP config, skills, settings) | managed multi-tenant sandboxing: a fresh hardened container per turn, a uid per organization, one door out; the tenant controls only the egress list |
| mapping the CLI's output onto Responses stream events | provider keys injected per request through a gateway that swaps a session token for the key |
| the final shape, when the events are not enough (`response.sh`) | tracing of every request, response, steer, cancel and provider exchange |
| | organizations, teams, members, keys; harness repositories with history; workspaces that persist; rodeos |

## The catalog

Eleven packages ship with the platform, each built from its CLI's own help and documentation and
run against a live provider before release: a template plus Claude Code, Codex, Pi, Oh My Pi,
Kilo Code, DeepSeek Harness, Hermes Agent, Cline, OpenClaw and Command Code. Where a CLI cannot
do something yet (resume headlessly, stream its events) the package says so. The research behind
them is [the Responses wire](docs/responses-wire.md): every request field mapped to each CLI's
invocation, every response field to its output, the seeds each reads, and the precedence a package
follows when a CLI offers several ways.

## Write a package

```
harness.json      contract, the CLI's name and version, default provider and model, egress, timeouts, what it accepts
install.sh        puts the CLI under $HG_TOOLS, once per commit
request.sh        reads $HG_REQUEST, runs the CLI, prints Responses stream events
response.sh       (optional) shapes output, usage and status at the end
seeds/workspace/  copied into the workspace once per session
seeds/home/       copied into $HOME every turn
```

```bash
hg harness pull my-harness ./pkg
hg harness run ./pkg --input "Create hello.txt"      # on your machine, no sandbox, your keys
hg harness push my-harness ./pkg -m "map tool results"
```

[Write a package](docs/packages.md) is the contract; `.agents/skills/harness-package` is the
procedure for writing one with a coding agent, and `.agents/skills/harness-mapping` the procedure
for researching a CLI first.

## Develop the platform

```bash
uv sync --extra dev
uv run pytest -q                          # without Docker
uv run pytest -q -m docker                # a real sandbox
uv run harnessgateway serve --dev         # the platform without Docker, for the console and packages
```

[Contributing and releases](docs/contributing.md) has the layers and the release flow: a
`CHANGELOG.md` section merged to `main` is a release. Apache-2.0.
