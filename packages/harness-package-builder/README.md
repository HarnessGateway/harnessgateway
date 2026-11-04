# harness-package-builder

Claude Code, run headless, seeded with the platform's harness-package-builder skill, the template package and the
packages guide. Say "Create a harness package for <name>": it researches the CLI (its docs, repository and registry
page), installs its latest stable version in its sandbox, tries it against this session's provider when the CLI
speaks that API shape, writes the package under `/workspace/<name>/`, validates what the package prints with hgkit's
check (the platform's own), and hands the folder back. Say what you need an agent *for* ("an agent that triages our
support tickets with Codex", "harden claude-code for the docs team") and it asks what changes what it will build
(purpose, callers, what it may reach and must never do, model, the operator's knobs), sends a plan, and on a go
builds a tailored package: the vanilla base plus the organization, with the CLI's own permission rules and hooks in
`seeds/home`, an `mcp` catalog with secrets bound to hosts, declared `settings` and `secrets`, narrowed egress, and
a recorded forbidden run that proves the fence. In the console, **New harness from workspace** on this session's
page turns the folder into a draft; **Install**, then a turn on the new harness is its real test.

This is the one built-in harness, installed by `init`, and the one shipped package that is not vanilla: its seeds are
the platform's own skill, not an organization's. An organization that wants its own builder (its conventions, its
CLIs) edits this one. The skill in this folder is the one every deployment gets: `init` installs the package from
the image, and `scripts/sync-builder-seeds` keeps the template and the guide inside the skill current.

- **CLI:** `@anthropic-ai/claude-code`, the version in `harness.json` (`version` is `<CLI version>-<revision>`;
  `install` reads it from `HG_PACKAGE_VERSION`), installed with npm into `$HG_TOOLS`.
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
| `harness.json` | manifest: name, version, provider, egress, persist |
| `install` | Claude Code, pinned to the manifest's version, into `$HG_TOOLS` |
| `run` | one turn: the request → `claude -p --output-format stream-json`; its stream → Responses events, with hgkit |
| `seeds/home/.claude/CLAUDE.md` | the builder's standing instructions |
| `seeds/home/.claude/skills/harness-package-builder/` | the skill, self-contained: `SKILL.md`; `reference/events.md` (what a package prints), `reference/request-fields.md`, `reference/tailoring.md` (a purpose as a package), `reference/packages.md` (a copy of `docs/packages.md`); `assets/template/` (a copy of `packages/shell`), `assets/observe-egress.py` (the hosts a command asks for) |
| `tests/` | the mapper run on streams recorded from real turns; `run` end to end against a fake `claude` |

## The command, request fields, output mapping, known limits

The request's input is the prompt on stdin, `instructions` are appended to the system prompt, `reasoning.effort`,
`text.format` and `max_tool_calls` map to the CLI's flags, the turn's MCP servers become its MCP config, turns after
the first resume the CLI's session (and a turn whose CLI session is gone, or a fork onto this harness, starts a fresh
one from the session's history); assistant text streams (`out.say`), thinking streams (`out.think`), Bash becomes
`out.ran`, Write/Edit become `out.wrote` / `out.edited`, MCP tools `out.called`, other tools `out.used`.

What the builder can try in its sandbox is bounded by this session's provider: `HG_PROVIDER_URL` and
`HG_PROVIDER_TOKEN` reach that provider only, in its API shape. A long study (installing a large CLI, recording a run)
is one long turn; the chat shows it as it goes, and the session page shows every command it ran.

## Tests

`.venv/bin/pytest -q packages/harness-package-builder`
