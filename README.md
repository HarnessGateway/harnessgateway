# HarnessGateway

Runs any agent harness CLI in a sandbox, behind the OpenAI Responses API.

Claude Code, Codex, pi, Hermes: each is a command-line program that already knows how to be an agent.
HarnessGateway runs them as they ship, one sandbox per turn, and gives you one API for all of them: the
OpenAI Responses API, unchanged. Your code uses the OpenAI SDK. The agent you get is chosen by the `model`
string.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="hg_...")

first = client.responses.create(
    model="claude-code/deepseek",
    input="Write a script that prints the first 20 primes, and run it.",
)
print(first.output_text)

# Same session, different harness: it finds the same files, and is told where the first one kept its notes.
second = client.responses.create(
    model="codex/deepseek",
    input="Add tests for that script.",
    previous_response_id=first.id,
)
```

## The idea

- The **request** says what to do and why.
- The **sandbox** is where and when it happens: an image, limits, and the network it may reach.
- The **harness CLI** is how: the agent loop its makers wrote, unmodified.

A session keeps two folders: the **mind** (`$HOME`, where each harness keeps its own sessions and memory)
and the **work** (the working directory). Both are saved after every turn and put back before the next, so
the sandbox and the agent process are disposable. Continue a session with `previous_response_id`; name
another harness and it takes over the same work.

Everything specific to one harness (how it is installed, its flags, its output format) lives in a
**harness package**: a folder with `harness.json`, `install` and `run`. The platform knows nothing about any
harness. Write a package and any CLI becomes a model name.

## Install

One machine with Docker (Linux, or a laptop with Docker Desktop or OrbStack):

```sh
curl -fsSL https://raw.githubusercontent.com/HarnessGateway/harnessgateway/main/deploy/install.sh | bash
```

It prints the address of the console and API, and the first admin key. On a Linux host, add
`--with-gvisor` (`... | bash -s -- --with-gvisor`) so sandboxes do not share the host's kernel. Without it,
isolation is an ordinary container: fine for development and code you trust.

Then, in the console or with the API:

```sh
export HG=http://localhost:8000 KEY=hg_...

# A provider: where models are, and the key for them.
curl -s $HG/v1/providers -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "name": "deepseek", "api_key": "sk-...", "default_model": "deepseek-flash",
  "apis": {"anthropic": "https://api.deepseek.com/anthropic",
           "openai-responses": "https://api.deepseek.com/v1",
           "openai-chat": "https://api.deepseek.com/v1"}}'

# A harness, from the packages that ship with the platform.
curl -s $HG/v1/harnesses -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"catalog": "claude-code"}'
```

## The `model` string

```
[environment:]harness[/provider[/model]]
```

| `model` | Means |
|---|---|
| `claude-code` | Claude Code, with its default provider and that provider's default model |
| `codex/openai/gpt-5.2` | Codex, on the provider named `openai`, with that model |
| `gpu:pi/deepseek` | pi, in the environment named `gpu` |
| `shell` | Runs the input as a shell command. No provider. The template for new packages |

## What you get

- **The Responses API as it is.** `POST /v1/responses`, streaming, background, `previous_response_id`,
  cancel, and the Files and Containers APIs to send files in and read an agent's work out. No added fields.
- **Sandboxes you define.** An environment is an image, CPU, memory, disk and time limits, variables,
  secrets, and a network policy. Any Linux image works.
- **Secrets that never enter the sandbox.** The sandbox holds placeholders. The egress proxy, the only way
  out, puts the real value into requests to the hosts a secret is bound to.
- **Any provider.** A provider is a name, base URLs per API (Anthropic Messages, OpenAI Responses, OpenAI
  Chat Completions) and a key. A package picks the API its CLI speaks.
- **A trace of every turn.** The command that ran, its output, its log, what it cost in tokens and time,
  and every host it reached.
- **A console.** Chat with any harness, browse sessions and files, manage harnesses, environments,
  providers and keys.

## Harness packages

`packages/shell` is the smallest package and the template. The packages for Claude Code, Codex, pi and
Hermes, and the harness package builder (an agent that writes packages), are in
[harness-packages](https://github.com/HarnessGateway/harness-packages).

```sh
harnessgateway package check packages/shell     # validates a package and replays its recorded cases
```

## Development

```sh
uv sync
uv run ruff check . && uv run mypy
uv run pytest                                                     # unit tests
HG_TEST_DATABASE_URL=postgresql://... uv run pytest               # and integration tests
HG_TEST_DATABASE_URL=... HG_TEST_DOCKER=1 uv run pytest tests/e2e # real turns in real containers
```

`SPEC.md` is the whole design, from the experiment it came from to every endpoint and table. `AGENTS.md` is
the short version for someone about to change the code.

## Licence

Apache-2.0.
