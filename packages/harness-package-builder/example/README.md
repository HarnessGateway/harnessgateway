# Example harness package

A whole harness package, to read before writing one: a cloud consultant, backed by Claude Code 2.1.325. It reads
AWS's and Microsoft's documentation, and writes what a client needs, such as a statement of work. Another CLI answers
the same questions with its own flags, settings, and files: take the shape from this, not the flags.

## What a package is

| File | Required | What it is |
|---|---|---|
| `README.md` | Yes | This page: what the package is, what it honours, and what it needs |
| `install` | Yes | Runs once for each image: puts the CLI, at one pinned version, under `$HG_HARNESS/tools` |
| `run` | Yes | Runs once a turn: reads the Responses request, drives the CLI, and prints Responses output |
| `AGENTS.md` | No | Who the agent is: a cloud consultant, and how it works |
| `skills/`, `.claude-plugin/` | No | What it knows how to do: scoping a statement of work, and comparing cloud services. With the manifest this CLI finds them by |
| `mcp.json` | No | Tool servers of the agent's own: AWS's documentation, and Microsoft's |
| `settings.json` | No | Permissions: it reads, writes, and edits files, and runs no command. A check before every write: the file is inside its working folder |

The three required files are a vanilla package: the CLI as it comes. Every optional file is a customization, and
`run` uses each only when it is there. Delete them all and this is still a working package. Anything else a CLI can
be configured with is brought the same way: a file in the package, and one line of `run` that points the CLI at it.

## What it honours of a request

| Field | How |
|---|---|
| `input` | One message: text, images, and files. Items an earlier response returned are given back as what the agent said, thought, did, and called |
| `instructions` | Its system prompt for that response only, after `AGENTS.md` |
| `previous_response_id` | The platform puts the session's folders back, and the CLI resumes its own session |
| `reasoning.effort` | How hard it thinks |
| `text.format` | A JSON schema the answer must fit |
| `tools` of type `function` | Offered to the CLI; a call ends the turn, and the developer's next request answers it |

Any other field fails the turn, naming it.

## What its environment must give it

- An image with Node.js 22 or later, npm, and Python 3.
- A provider that speaks the Anthropic API.
- A network that reaches `knowledge-mcp.global.api.aws` and `learn.microsoft.com`, the two tool servers in `mcp.json`.
  Neither needs a token. One that did would take it from a secret of the environment.
