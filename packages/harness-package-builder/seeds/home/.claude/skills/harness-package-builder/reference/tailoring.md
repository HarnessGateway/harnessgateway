# Tailoring: an organization's purpose as a package

A tailored package is a vanilla CLI package plus the organization: what the agent is for, what it may do, what it
may reach, and the knobs the operator turns. Everything below lives in the package, so it is versioned, reviewed and
upgraded like code, and reseeded into every new sandbox so the agent cannot drift from it.

## The questions

Ask them in one message, numbered, each with the default you would pick; skip any the request already answered.

| Ask | Why it matters | Where the answer goes |
|---|---|---|
| What is the agent for, and what does a typical request look like? | the instructions, the tools, the egress | `seeds/home` instructions; `mcp`; `run.egress` |
| Who calls it, and in what shape must it answer (prose, files, JSON)? | `text.format` support, what `run` maps, the README | `run`; README "Purpose" |
| Which CLI? (or: recommend one for this provider and purpose) | the base package | the vanilla base |
| What must it reach: repositories, internal services, MCP servers, registries? | each is an egress entry or a catalog server with a secret | `mcp`, `secrets`, `run.egress` |
| What must it never do: paths, commands, hosts, kinds of change? | the permission rules and hooks | `seeds/home` permission config; deny egress entries |
| Which model? | the default | `defaults.model` |
| What should an operator change without editing the package? | settings and secrets | `settings`, `secrets` |
| How long may a task take? | the timeout | `run.timeout_s` |

Then the plan, as a table, and a go.

## The layers, and where each lives

| Layer | Mechanism | Who enforces it |
|---|---|---|
| Purpose, voice, what it refuses to do | the CLI's instruction file under `$HOME` (`seeds/home`) | the model, as instructed |
| Which tools, which commands, which paths | the CLI's permission rules and pre-tool hooks (`seeds/home`) | the CLI, before each tool call |
| Which servers and which of their tools | `mcp` in the manifest, `allowed_tools` narrowed | the CLI's MCP config, written by `run` from `$HG_MCP` |
| Credentials | `secrets` in the manifest, bound to hosts | the door: placeholders inside, values injected only toward those hosts |
| Which hosts at all | `run.egress` (catalog and secret hosts are implied) | the door, outside the sandbox |
| Configuration the operator sets | `settings` (environment variables in the sandbox) | `run` passes them to the CLI |
| Time | `run.timeout_s`, `stop_grace_s` | the node |
| What survives between turns | `persist` (the CLI's session store only); the workspace and `$HG_STATE` always | the platform's snapshot |

The outer layers (door, placeholders, gVisor, snapshot) hold whatever the agent does inside. The inner layers (the
CLI's own rules) are what make the agent *this* agent rather than a general one; they cannot widen what the outer
ones allow.

## Finding each CLI's mechanisms

You researched the CLI for the vanilla package; tailoring needs four more facts, from its current documentation:

1. **Where it reads standing instructions under `$HOME`** (a user-level memory or instruction file), so the purpose
   is in place for every session regardless of the workspace.
2. **How it allows and denies tools**: a settings file with allow and deny lists, patterns for commands and paths,
   a sandbox or approval mode, and how to make that mode non-interactive (the sandbox has no one to approve).
3. **Whether it runs hooks** before a tool call, and how a hook refuses one: the mechanism that expresses rules the
   lists cannot ("no `git push`", "nothing outside `src/`", "no network from Bash").
4. **Where its skills, subagents, commands and MCP config live**, so the purpose's extras go where the CLI will find
   them and `run` can write the MCP config from `$HG_MCP`.

Common cases, to be verified against the current docs before use, never assumed:

- *Claude Code*: `~/.claude/CLAUDE.md` for standing instructions; `~/.claude/settings.json` with `permissions.allow`
  and `permissions.deny` (tool names, `Bash(pattern)`, paths) and `hooks` (`PreToolUse` commands that can deny);
  `~/.claude/skills/`, `~/.claude/agents/`; `--mcp-config` for servers; `--dangerously-skip-permissions` is what
  makes it headless, so the deny list and hooks are the fence.
- *Codex*: `~/.codex/AGENTS.md` for standing instructions; `~/.codex/config.toml` for `approval_policy`,
  `sandbox_mode` and MCP servers; `codex exec` for headless runs.
- *Gemini CLI*: `~/.gemini/GEMINI.md`; `~/.gemini/settings.json` for tools and MCP servers.

When a CLI has no deny mechanism for something the purpose forbids, say so: the README's "may not" table lists only
what the package enforces, and the person decides whether the outer fence (egress, the request's `max_tool_calls`,
a `turn_timeout`) is enough.

## Settings and secrets, as the organization sees them

A declared setting shows in the console with its description and default; the operator fills it in once, and every
turn of every session gets it as an environment variable. A declared secret shows as "set" or "not set"; its value
is sealed and reaches the sandbox only as a placeholder that the door swaps in toward the declared hosts. So:

- A token for an MCP server: a secret bound to that server's host, referenced as `${TOKEN}` in the catalog header.
- A token for an API the CLI itself calls (through Bash, say): a secret bound to that API's host, and the
  instructions tell the agent to send it as the variable's value in an `Authorization` header; the door replaces it.
- A value that is not secret (a project key, a base URL, a team name): a setting, read by `run` or by the
  instructions as `$NAME`.

Never put a value in the package. The organization sets it in the console (or `PATCH /v1/harnesses/<name>`) before
the first turn; list in the README which ones must be set.

## Proving it

A tailored package is proven by two recorded runs against this session's provider: a realistic task that succeeds,
and a forbidden action that is refused by the package's own rules (the hook's denial, the CLI's permission error,
the door's 403 in the CLI's output). Both become fixtures and tests. The README's "may and may not" table then
states what was demonstrated, not what was intended.
