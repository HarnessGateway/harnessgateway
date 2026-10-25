---
name: harness-mapping
description: Research a coding-agent CLI (a "harness") and map it onto the OpenAI Responses API for HarnessGateway. Use when adding a harness, bumping a harness version, or auditing an existing mapping. Produces the request-to-invocation table, the output-to-response table, the seeds inventory (files to write into the workspace and home), the defect list, and a package skeleton, every cell checked against the CLI's own --help and its official documentation.
---

# Harness mapping

A harness package turns a Responses request into a CLI run and the CLI's output into a Responses
response, and seeds the files that make the agent behave as the organization wants. This skill is
the procedure for working out, from evidence, what a given harness can take, what it emits, and
what it reads from disk. The result is the material for `docs/responses-wire.md` and for a package.

Read `docs/responses-wire.md` first. Its tables are the format to match, and its Sources section
shows the depth of citation expected.

## Rules

1. **Pin the version.** Install the exact version being mapped and name it everywhere. Help text,
   documentation and source are all read at that version; if a docs site is unversioned, say so
   and check contested cells against the source at the version's tag.
2. **Two sources per cell.** Every cell comes from the CLI's own `--help` and is then confirmed
   against official documentation. Mark the cell *docs* when only the documentation has it,
   *source* when only the source has it, and "not in docs" when a help flag is undocumented.
   Never fill a cell from memory or from a blog.
3. **Record one real run** in JSON mode before writing the output table. Event names in a table
   that was never exercised are a guess.
4. **List every alternative.** Where the harness offers a flag and a file and an environment
   variable for the same thing, list all three, flags first, then files, then environment.
5. **Write "—" for nothing.** If neither help nor docs offer a way to carry a request field, the
   cell is "—" and the platform refuses that field for this harness. Do not invent a workaround.
6. **Name defects plainly.** A documented flag that the source does not honour (Cline's `--id` in
   headless mode) or a missing capability (OpenClaw's `exec` cannot resume) goes in a short
   defects list, not in a footnote.
7. **Never name the ranking source** that chose the harnesses. Write "a public ranking".
8. **Nothing secret in the repo.** Keys stay in the environment; scan every diff before committing.

## Procedure

### 1. Install and capture help

```
<install command at the pinned version>
<cli> --version
<cli> --help
<cli> <headless subcommand> --help      # exec, run, chat, -p, agent exec, --profile headless
<cli> <mcp|auth|config|session|history> --help
```

Save the help text in the scratchpad. Find the non-interactive invocation: the flag or
subcommand that takes a prompt, runs to completion without a TTY, auto-approves tools, and prints
machine-readable output.

### 2. Find the documentation

Look for an index first (`/llms.txt`, a sitemap, a docs repo at the version's tag). Fetch the
pages for: CLI reference, headless or non-interactive mode, configuration and settings,
instruction or memory files, MCP, skills, hooks, subagents or custom agents, permissions and
sandboxing, sessions, providers and custom models, environment variables, output or event format.
Save raw copies in the scratchpad so cells can be quoted.

### 3. Request side: fill the input table

For each Responses request field, write how it reaches the CLI, or "—":

`input` text · `input_file` · `input_image` · prior items in `input` · `instructions` ·
`previous_response_id` / `conversation` · `model` · `reasoning.effort` · `reasoning.summary` ·
`text.format` · `text.verbosity` · `tools` of type `mcp` · the harness's own tools and approvals ·
`tool_choice` · `parallel_tool_calls` · `max_tool_calls` · `max_output_tokens` · `temperature`,
`top_p`, `top_logprobs` · `store: false` · `stream` · `truncation` / `context_management` ·
`service_tier` · `access_programs` · `background` · `metadata` · the rest.

Also record how the harness is pointed at a provider (URL and key), since the request never
carries them: environment variables, a provider file and its shape, or config rows.

### 4. Output side: run once, then fill the emit and response tables

Run the headless invocation in JSON mode against a real model with a prompt that makes the agent
run a command, write a file and answer. Keep the raw output. From it and the docs, write:

- the emit summary: structured output format and event names, where the final answer is, where
  the session id is, where usage is, exit codes;
- the response table: for each Responses field (`status`, `error`, `incomplete_details`, the
  `output` items `message`, `reasoning`, `shell_call`, `apply_patch_call`, `function_call`,
  `mcp_call`, `web_search_call`, then `usage`, `metadata.session_id`, stream deltas) the harness
  event that carries it, or "—".

Note whether text arrives as token deltas or completed blocks, and whether the session id is in
the stream at all without extra flags.

### 5. Seeds: what the harness reads from disk

List, with exact paths relative to the workspace or the home directory, format and effect:

always-loaded instruction files · path-scoped rules · system prompt replace/append files · MCP
server file and its JSON or config shape · skills directories and the `SKILL.md` frontmatter ·
hooks (file, shape, event names, how to block, trust requirements) · subagent definitions ·
prompt templates and slash commands · settings and permission files and their precedence ·
provider and model configuration · environment variables the run needs · where sessions are
stored · the flag or variable that turns the seeds off (`--bare`, `--isolated`, `-nc`).

Check specifically: is `AGENTS.md` read, and under what condition; is `.agents/skills/` read; is
the MCP file the `{"mcpServers": {}}` shape; is the hooks file the
`{"PreToolUse": [{"matcher", "hooks": [{"type": "command", "command"}]}]}` shape; which home or
config directory variable relocates everything per tenant.

### 6. Control: stop and resume, guardrails, tool restriction

Record: whether the session is written incrementally (so SIGINT then resume loses nothing);
whether the CLI resumes the session a stopped run left (the platform's one steering model: the
next request stops the running turn and continues the session); which hook events can block a tool call and how; the deny-rule or
permission mechanism; the sandbox mode; how built-in tools are restricted by name; anything that
silently disables hooks (Cline's `-y`) or needs a trust flag headless.

### 7. Apply the precedence ladder

For every field with more than one mechanism, pick the default by the ladder in
`docs/responses-wire.md` ("Precedence"): the mechanism shared by the most harnesses first, then
file over flag over environment, flag only where the shared file is absent or defective for this
harness. Record the choice and the reason in the package manifest.

### 8. Write it down

- Add or update the harness's column in every table of `docs/responses-wire.md`, its row in the
  harnesses table, its provider paragraph, its Sources entry and, if any, its defects.
- Keep the condensed research notes in the scratchpad (`review/<harness>.md`) until the doc
  commit lands.
- Produce the package skeleton (below) under the packages directory, with the recorded run as a
  test fixture.

## Package skeleton

```
<harness>/
  package.yaml          # harness, version, invocation, accepted request fields, precedence
                        # choices, guardrail layers provided, defects
  request.*             # Responses request -> argv, files, env for one turn
  response.*            # CLI output -> Responses output items, usage, status, session id
  seeds/
    workspace/          # AGENTS.md, .agents/skills/, .mcp.json or rendered equivalent, hooks
    home/               # settings, provider config, hooks that live in the home directory
  tests/
    recorded/           # the raw JSON run from step 4 and the response it must map to
```

The template package is the one whose seeds are the shared mechanisms (`AGENTS.md`,
`.agents/skills/`, `{"mcpServers": {}}`, the PreToolUse hooks shape); a harness package is the
template plus the renderers its harness needs.

## Checklist before committing

- [ ] version pinned and named in every table
- [ ] every cell has a help source and a docs source, or a *docs* / *source* mark, or "—"
- [ ] one recorded run in JSON mode is in the fixtures and the event names in the tables match it
- [ ] alternatives listed in order: flags, files, environment
- [ ] defects listed
- [ ] Sources entry added with page links
- [ ] no secrets in the diff; the ranking source unnamed
