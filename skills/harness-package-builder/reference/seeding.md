# Seeding: making a general agent your organization's agent

A CLI reads far more than its flags: instruction files, skills, subagents, hooks, MCP servers, permission settings,
provider configuration. A package ships those files in `seeds/`; the platform copies them when a session starts.

| Seed path | Copied to | When |
|---|---|---|
| `seeds/workspace/...` | `/workspace/...` | once, when the session starts |
| `seeds/home/...` | `$HOME/...` (`/home/hg`) | once, when the session starts |

Both then persist with the session, so the agent can update them over the session's turns.

Typical seeds (check each CLI's documentation for its exact paths):

| What | Claude Code | Codex | Pi | Hermes |
|---|---|---|---|---|
| project instructions | `CLAUDE.md` (or `AGENTS.md`) | `AGENTS.md` | `AGENTS.md` | `AGENTS.md` / its context files |
| user settings | `home/.claude/settings.json` | `home/.codex/config.toml` | `home/.pi/agent/` | `home/.hermes/config.yaml` |
| skills | `home/.claude/skills/<skill>/SKILL.md` | `home/.codex/skills/` (if supported) | the CLI's skills dir | its skills dir |
| subagents | `home/.claude/agents/*.md` | — | — | — |
| MCP servers | `.mcp.json` / settings | `config.toml [mcp_servers]` | its MCP config | its config |

Guidelines:
- Seed what is the same for every session; write what depends on the request (instructions, MCP servers from `tools`,
  the provider URL and model) in `request.sh` at each turn, so a resumed session picks up changes.
- Never put secrets in seeds: declare them as `settings` (`secret: true`); they arrive in `$HG_SETTINGS` per turn.
- Keep provider configuration out of seeds: the provider URL, token and model change per turn.
- A `.gitignore`-style file is fine in seeds; seeds keep dotfiles.
