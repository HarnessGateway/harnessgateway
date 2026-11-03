# Building a harness package

A harness package is what turns a coding-agent CLI into an agent HarnessGateway can run: how to install the CLI, how
to run one turn, and what the agent *is* (its standing instructions, skills, hooks, MCP catalog). The platform knows
no CLI and no language; everything CLI-specific lives in the package. The contract is [`SPEC.md`](../SPEC.md) §4–§5;
this page is the walkthrough. `packages/shell` is the template: the smallest complete package, and the one every new
harness starts from. Its README says, file by file, what to change.

## In the console

Under **Setup → Harnesses**, **New harness** copies the template into a draft under the name you give. Edit its files
in the editor (add files for seeds), **Install**, then test it with a request: pick it in **Workbench → Chat**, or
send `POST /v1/responses` with `"model": "<name>/<provider>/<model>"`. Or let the built-in write it:
`harness-package-builder` is Claude Code seeded with the builder skill, this guide and the template, installed by
`init`; say "Create a harness package for <name>" in the chat, and it researches the CLI, installs it in its sandbox,
writes the package under `/workspace/<name>/` and validates what it prints; or say what you need an agent for, and it
asks what it needs to know, proposes a plan, and builds a tailored package (below, "Your organization's agent") with
the CLI's own permission rules, an MCP catalog, settings and secrets, and narrowed egress; **New harness from
workspace**, on that session's page, turns the folder into a draft. The session under **Observability → Sessions** shows every event your
mapping produced and, under each turn, what the platform measured: the exit code, the CLI's stderr tail, provider
calls and door connections. Edit the installed harness (**Edit** opens it as a draft) and **Install** again to
upgrade it in place; running sessions keep the package they started on. The same package can be kept in git and
installed with `POST /v1/harnesses` as a tar archive.

## The split

| | Comes from |
|---|---|
| The task: `input` text and files, `instructions`, `reasoning.effort`, `text.format`, a selection of MCP servers | the request |
| The agent: the CLI and its version, its standing system prompt, instruction files, skills, hooks, subagents, permissions, what persists, the MCP catalog, which hosts it may reach | the package |
| MCP servers | the union: the package's catalog, narrowed or added to by the request (added servers must be on hosts egress allows) |

## Layout

Three files make a package. The platform reads the first and runs the other two; it never looks inside them.

```
harness.json          the manifest: what the agent is
install               an executable: installs the CLI into $HG_TOOLS, pinned, once per package
run                   an executable: one turn. The request in, Responses events out. Any language
seeds/home/           laid into $HOME of every new sandbox: the agent's standing configuration (optional)
seeds/workspace/      laid into the workspace of a new session (optional)
README.md, tests/     yours; the platform reads neither
```

`install` and `run` are run as they are: a script whose first line names its interpreter (`#!/usr/bin/env python3`,
`#!/usr/bin/env bash`, `#!/usr/bin/env node`), or a Linux binary. The platform marks them executable when it installs
the package. The sandbox image has bash, python3 and node; what
`run` is written in is yours to choose. The template's `run` is Python with hgkit (below) because that is the least
code for most CLIs.

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

Unknown fields are refused. `version` is `<CLI version>-<package revision>`: the platform hands it to `install` and
`run` as `HG_PACKAGE_VERSION`, so the CLI's version is written here and nowhere else. `provider.apis` names the API
shapes the CLI speaks (`anthropic`, `openai`, `google`); leave it out for a harness without a model. Egress entries
are `host`, `*.domain` or `*`, with an optional `:port`, `!` in front to deny; the organization's entries apply to
every harness, the package's add to them (or deny).

## Install

`install` runs once per package digest on a node, in a sandbox with a writable `$HG_TOOLS` and egress
`install.egress`, no provider and no secrets. Install the CLI by its vendor's documented method, pinned to the
manifest's version (`${HG_PACKAGE_VERSION%-*}` in bash), and verify the version. Never name a CPU architecture (the
package manager picks the build); never edit installed files afterwards. The result is mounted read-only into every
sandbox of that package.

## Run

`run` runs once per turn in the session's sandbox, in the workspace, with the request on stdin and at `$HG_REQUEST`.
It prints one JSON object per stdout line: Responses stream events (`"type": "response.…"`), anything else is a log
line. Exactly one terminal event (`response.completed`, `.incomplete`, `.failed`) ends the turn. The platform
validates every event against the OpenAI SDK's models and assigns sequence numbers.

What a turn sees (SPEC §5.6): `HG_REQUEST`, `HG_FILES` (file_id inputs and `index.json`), `HG_MCP` (the turn's
servers, header values holding placeholders), `HG_WORKSPACE`, `HG_STATE`, `HOME`, `HG_PACKAGE_DIR`, `HG_TOOLS`,
`HG_PACKAGE_VERSION`, `HG_SESSION`, `HG_RESPONSE`, `HG_TURN`, `HG_MODEL`, `HG_PROVIDER_API`, `HG_PROVIDER_URL`
(`http://hg-door:8080`: point the CLI's base URL here), `HG_PROVIDER_TOKEN` (use it as the API key), each declared
setting as a variable, each declared secret as a variable holding its placeholder.

### hgkit

hgkit is the Responses contract in Python: the request read for you, the events printed correctly, and the
platform's own validator to check with. It is in the sandbox image (`/opt/hg/lib`, on `PYTHONPATH`) and optional: it
knows nothing about any CLI, and a `run` in another language prints the same lines without it.

```python
#!/usr/bin/env python3
from hgkit import Request, Emitter

req, out = Request.load(), Emitter()
names = req.place_inputs()                 # the request's files, into the workspace by name
prompt = req.prompt                        # the user text
system = req.instructions                  # `instructions` + system/developer messages: append to the CLI's own
model, provider = req.model, req.provider  # provider.url (no /v1), provider.token, provider.api
servers = req.mcp_servers()                # [{label, url, transport, headers, allowed_tools}]
resume = req.state.get("session")          # small key/values kept with the session ($HG_STATE)

out.think("Reading the tests")             # reasoning, streamed
out.say("Hel"); out.say("lo")              # assistant text, streamed
run = out.ran("pytest -q")                 # a shell call now; run.done(stdout=.., exit=0) when it ends
out.ran("ls", stdout="a\n", exit=0)        # or both at once
out.wrote("a.py", "print(1)\n")            # a file created; out.edited(path, diff); out.deleted(path)
call = out.called("github", "get_issue", {"n": 1})      # an MCP tool; call.done(result) / call.failed(error)
out.used("WebSearch", {"q": "x"}, "…")     # any other tool
out.ignored(["temperature"])               # fields the CLI could not honour → metadata.hg_ignored_fields
out.done(input=10, output=5, cached=3)     # or out.stopped("max_output_tokens"), out.failed(code, message)
```

`out.item({...})` and `out.emit({...})` take any Responses item or event the schema allows, for whatever a harness
does that these verbs do not name. An open message or reasoning item closes when anything else is emitted.

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

## Your organization's agent

A package meant for others to start from stays vanilla: the CLI as its vendor ships it plus what it needs to run
headless in the sandbox. Your organization's agent on that CLI is a package of its own: open the vanilla one in the
console, **Edit**, give it a new `name`, and add what makes it yours: instruction files, skills, hooks and settings
under `seeds/home`, an `mcp` catalog, `settings` and `secrets` in the manifest. Everything under `$HOME` is reseeded
from the package in every new sandbox, so the agent's configuration is always the package's and takes effect on
upgrade without migration. Least privilege is two fences: the platform's, outside the sandbox (the door's egress
allow-list, secrets injected only toward their hosts, gVisor), and the package's, inside it, in the CLI's own terms
(its permission rules, its hooks, which tools of which MCP servers). The builder knows how to write both: tell it
the purpose and it asks for what it needs, then proves the fence with a recorded forbidden run.

## Testing

The validator is hgkit's, and the platform runs the same one, on the same pinned OpenAI SDK: what passes locally
passes live.

- **While writing `run`:** `Emitter(validate=True)` checks each event before printing it and raises with the field
  named, instead of the turn failing later as `invalid_output`.
- **On recorded output:** `python3 -m hgkit check out.jsonl` (in the sandbox, or anywhere hgkit and `openai` are
  installed) reads the whole output as the platform does and names the first line and field it would refuse.
- **In tests:** `hgkit.check(text)` returns `(events, outcome)` or raises `OutputError`; run `run` on recorded CLI
  output and assert on the outcome. The template's `tests/` show the shape.

Live: install (**Install** on the draft in the console, or `POST /v1/harnesses` with a multipart `package` archive),
then send a turn with `"model": "my-agent/provider/model"`, and read `GET /v1/traces/{response_id}` for the harness's
stderr tail, timings and the door's connection log.
