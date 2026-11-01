# harness-package-builder

Claude Code, run exactly as the `claude-code` package runs it, seeded with the platform's harness-package-builder
skill, the shell template and the packages guide. Say "Create a harness package for <name>": it researches the CLI
(its docs, repository and registry page), installs its latest stable version in its sandbox, tries it against this
session's provider when the CLI speaks that API shape, writes the package under `/workspace/<name>/`, checks the
mapper's events offline, and hands the folder back. In the console, **New harness from workspace** on this session's
page turns that folder into a draft; **Install**, then a turn on the new harness is its real test.

This is the one built-in harness, installed by `init`, and the one shipped package that is not vanilla: its seeds are
the platform's own skill, not an organization's. An organization that wants its own builder (its conventions, its
CLIs) edits this one.

- **CLI:** `@anthropic-ai/claude-code` (see `harness.json` for the pinned version), installed with npm into `$HG_TOOLS`.
- **Provider:** `anthropic` (Claude Code speaks the Messages API). A CLI the builder studies can be tried for real only
  when it speaks the same shape as this session's provider; otherwise the builder writes the mapping from the CLI's
  documented output and says so.
- **Run egress:** `*`. The builder researches a CLI before it packages it (documentation, repository, registry,
  release notes) and installs it by whatever route the project documents, so its own sandbox reaches everything. The
  harness it writes gets only the egress its manifest declares.
- **Persist:** `.claude/projects` (Claude Code's own session store, so a later turn resumes the conversation).

## Files

| File | Role |
|---|---|
| `harness.json` | manifest: the claude-code manifest with this name, description and run egress |
| `install.sh`, `run.sh`, `map.py` | the claude-code package's, unchanged |
| `seeds/home/.claude/skills/harness-package-builder/` | the skill: `SKILL.md`, `reference/events.md` (the events cookbook), `reference/request-fields.md`, `scripts/check_events.py` (an offline check of a mapper's stdout) |
| `seeds/workspace/CLAUDE.md` | the builder's standing instructions |
| `seeds/workspace/template/` | the shell package, the template every harness starts from |
| `seeds/workspace/docs/packages.md` | the platform's packages guide |
| `tests/` | the claude-code package's fixture tests, run against this package's `map.py` |

## The command, request fields, output mapping, known limits

As the `claude-code` package: the request's input is the prompt on stdin, `instructions` are appended to the system
prompt, `reasoning.effort`, `text.format` and `max_tool_calls` map to the CLI's flags, the turn's MCP servers become
its MCP config, turns after the first resume the CLI's session; assistant text streams, thinking becomes `reasoning`,
Bash becomes `shell_call`, Write/Edit become `apply_patch_call`, MCP tools `mcp_call`, other tools `function_call`.

What the builder can try in its sandbox is bounded by this session's provider: `HG_PROVIDER_URL` and
`HG_PROVIDER_TOKEN` reach that provider only, in its API shape. A long study (installing a large CLI, recording a run)
is one long turn; the chat shows it as it goes, and the session page shows every command it ran.

## Tests

`.venv/bin/pytest -q packages/harness-package-builder`
