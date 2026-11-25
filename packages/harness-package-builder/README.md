# Harness package builder

The harness package that builds every other harness package. Name a harness CLI, or describe the agent you want on
one: it researches the CLI, writes the package, and ends with a link that adds it. It ships with the platform and
is updated with it.

| File | What it is |
|---|---|
| `README.md`, `install`, `run` | The builder itself, as a harness package. It runs Claude Code 2.1.325 |
| `AGENTS.md` | Who the builder is, why it exists, the developer it builds for, and how the platform works |
| `skills/building-harness-packages/` | How it builds a package: seven steps, five primitives |
| `contract/` | The Responses API as three schemas (a request, a response, the stream's events), generated from the OpenAI SDK the platform is pinned to |
| `example/` | A whole harness package for it to read: three required files, and the optional ones |
| `requests/` | One session of the requests a developer sends, which a package is tested with |

It honours `input`, `instructions`, `reasoning`, `text.format`, `tool_choice`, and tool servers named in `tools`.
Any other field fails the turn, naming it.

Its environment must give it: an image with Node.js 22 or later, npm, and Python 3; a provider that speaks the
Anthropic API; and a network that reaches the registries and documentation of the CLIs it is asked to build for.
