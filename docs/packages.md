# Building a harness package

A harness package is what turns a coding-agent CLI into an agent HarnessGateway can run: how to install the CLI, how
to run one turn, and what the agent *is* (its standing instructions, skills, hooks, MCP catalog). The platform knows
no CLI; everything CLI-specific lives in the package. The contract is [`SPEC.md`](../SPEC.md) §4–§5; this page is the
walkthrough. `packages/shell` is the smallest complete package; `packages/claude-code` a full one.

## The split

| | Comes from |
|---|---|
| The task: `input` text and files, `instructions`, `reasoning.effort`, `text.format`, a selection of MCP servers | the request |
| The agent: the CLI and its version, its standing system prompt, instruction files, skills, hooks, subagents, permissions, what persists, the MCP catalog, which hosts it may reach | the package |
| MCP servers | the union: the package's catalog, narrowed or added to by the request (added servers must be on hosts egress allows) |

## Layout

```
harness.json          the manifest
install.sh            installs the CLI into $HG_TOOLS, pinned (base packages only)
run.sh                one turn: `exec python3 "$HG_PACKAGE_DIR/map.py"` (base packages only)
map.py                request → CLI command; CLI output → Responses events (with hgkit)
seeds/home/           laid into $HOME of every new sandbox: the agent's standing configuration
seeds/workspace/      laid into the workspace of a new session
README.md, tests/     yours; the platform reads neither
```

## The manifest

```json
{
  "contract": 1,
  "name": "my-agent",
  "version": "2.1.310-1",
  "description": "…",
  "provider": {"apis": ["anthropic"]},
  "defaults": {"model": "claude-sonnet-5-5"},
  "install": {"egress": ["registry.npmjs.org"], "timeout_s": 1200},
  "run": {"egress": ["api.github.com"], "stop_grace_s": 10},
  "persist": [".claude/projects"],
  "settings": {"LOG_LEVEL": {"description": "the CLI's log level", "default": "info"}},
  "secrets": {"GITHUB_TOKEN": {"description": "for the GitHub MCP server", "hosts": ["api.githubcopilot.com"]}},
  "mcp": {"github": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}}}
}
```

Unknown fields are refused. `provider.apis` names the API shapes the CLI speaks (`anthropic`, `openai`, `google`);
leave it out for a harness without a model. Egress entries are `host`, `*.domain` or `*`, with an optional `:port`,
`!` in front to deny; the organization's entries apply to every harness, the package's add to them (or deny).

## Install

`install.sh` runs once per package digest on a node, in a sandbox with a writable `$HG_TOOLS` and egress
`install.egress`, no provider and no secrets. Install the CLI by its vendor's documented method, pinned to a version,
and verify the version. Never name a CPU architecture (the package manager picks the build); never edit installed
files afterwards. The result is mounted read-only into every sandbox of that package.

## Run

`run.sh` runs once per turn in the session's sandbox, in the workspace, with the request on stdin and at
`$HG_REQUEST`. It prints one JSON object per stdout line: Responses stream events (`"type": "response.…"`), anything
else is a log line. Exactly one terminal event (`response.completed`, `.incomplete`, `.failed`) ends the turn. The
platform validates every event against the OpenAI SDK's models and assigns sequence numbers.

What a turn sees (SPEC §5.6): `HG_REQUEST`, `HG_FILES` (file_id inputs and `index.json`), `HG_MCP` (the turn's
servers, header values holding placeholders), `HG_WORKSPACE`, `HG_STATE`, `HOME`, `HG_PACKAGE_DIR`, `HG_TOOLS`,
`HG_SESSION`, `HG_RESPONSE`, `HG_TURN`, `HG_MODEL`, `HG_PROVIDER_API`, `HG_PROVIDER_URL` (`http://hg-door:8080`:
point the CLI's base URL here), `HG_PROVIDER_TOKEN` (use it as the API key), each declared setting as a variable, each
declared secret as a variable holding its placeholder.

### hgkit

The helper library in the sandbox image (`from hgkit import Request, Emitter, State`):

```python
req, out = Request.load(), Emitter()
names = req.place_inputs()                 # the request's files, into the workspace by name
prompt = req.prompt                        # the user text
system = req.instructions                  # `instructions` + system/developer messages: append to the CLI's own
servers = Request.mcp_servers()            # [{label, url, transport, headers, allowed_tools}]
out.ignored(["temperature"])              # fields the CLI could not honour → metadata.hg_ignored_fields
out.text("Hel"); out.text("lo")            # streamed assistant text
out.shell("ls", stdout="a\n", exit_code=0)
out.patch("create_file", "a.py", "+print(1)\n")
out.mcp("github", "get_issue", {"n": 1}, output="…")
out.completed(usage=Emitter.usage(input_tokens=10, output_tokens=5))
```

`State` keeps small key/values under `$HG_STATE` (the CLI's own session id for `--resume`, for instance), which
persists with the session.

## Secrets

A secret's value never enters the sandbox. The CLI sees a placeholder (`hgs_…`) in the variable and in MCP headers;
the egress door replaces it in request headers to the hosts the secret is bound to, and nowhere else. So put the
placeholder exactly where the CLI would put the token (an `Authorization` header, an `X-Api-Key` header) and bind the
secret to that host in the manifest. The provider key is the same idea: `HG_PROVIDER_TOKEN` is a per-turn token the
gateway exchanges for the organization's real key.

## Persistence

The workspace and `$HG_STATE` survive across turns and sandboxes; under `$HOME`, only the paths in `persist`
(use it for the CLI's session store, nothing else). Everything else in `$HOME` is reseeded from `seeds/home` in every
new sandbox, so the agent's configuration is always the package's.

## Overlays: an organization's agent on a built-in CLI

An overlay is `harness.json` with `extends` naming the base harness (a built-in or one the organization installed),
plus `seeds/`: instruction files, skills, hooks, an MCP catalog, settings and secrets of its own. No scripts. At
install the base is pinned by digest; the overlay's seeds lay over the base's, its defaults win, and its egress,
persist, settings, secrets and MCP union with the base's (a name both define is refused). Keep overlays in git and
install them with `{"git": {"url", "ref", "path"}}`: the organization's IP, version-controlled, without copying any
CLI's mapping.

## Testing

Offline: run `map.py` on recorded CLI output and check every printed line with the platform's own reader:

```python
from harnessgateway.domain.contract import check_output
events, outcome = check_output(stdout_text)      # raises OutputError with the offending field
```

Live: install (`POST /v1/harnesses` with a multipart `package` archive, `{"git": …}` or `{"builtin": name}`), then
send a turn with `"model": "my-agent/provider/model"`, and read `GET /v1/traces/{response_id}` for the harness's
stderr tail, timings and the door's connection log.
