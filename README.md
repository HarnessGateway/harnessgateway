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
    model="default/claude-code/deepseek/deepseek-flash",
    input="Write a script that prints the first 20 primes, and run it.",
)
print(first.output_text)

# Same session, different harness: it finds the same files, and is told where the first one kept its notes.
second = client.responses.create(
    model="default/codex/deepseek/deepseek-flash",
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

It prints the address of the console and API, and the first admin key. The harness packages that ship
with the platform are added, and the `default` environment carries them all: their installs start when the
platform does, one after another, and take about 12 GB of disk between them; a turn that names a harness
not yet installed installs it first. Untick in the `default` environment the harnesses you will not use. Run again, it updates the
installation to the current version and keeps its data and settings. On a Linux host, add
`--with-gvisor` (`... | bash -s -- --with-gvisor`) so sandboxes do not share the host's kernel. Without it,
isolation is an ordinary container: fine for development and code you trust.

Then, in the console or with the API:

```sh
export HG=http://localhost:8000 KEY=hg_...

# A provider: where models are, the key for them, and the models to offer in the pickers.
curl -s $HG/v1/providers -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "name": "deepseek", "api_key": "sk-...", "models": ["deepseek-flash", "deepseek-v4-pro"],
  "apis": {"anthropic": "https://api.deepseek.com/anthropic",
           "openai-responses": "https://api.deepseek.com/v1",
           "openai-chat": "https://api.deepseek.com/v1"}}'

# An environment of your own: an image set up by a script, carrying the harnesses you choose.
curl -s $HG/v1/environments -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "name": "data", "harnesses": ["claude-code", "codex"],
  "setup": {"script": "pip install pandas matplotlib"}}'
```

An environment's setup script runs once, as root, on its image, before any harness is installed; what it
leaves is the image its turns start from. Then each harness it carries is installed on that image. The
platform's sandbox image has Python with pip and Node with npm, and no system package manager: a script that
needs `apt-get` belongs to an environment whose `image` has it (`"image": "python:3.13"`, say). Any key, a
runner's too, can list the providers, environments and harnesses there are, to know what to name.

## The `model` string

```
environment/harness/provider/model
```

Every part is named, always: nothing is filled in for you. The model is everything after the third slash.

| `model` | Means |
|---|---|
| `default/claude-code/anthropic/claude-opus-5-5` | Claude Code in the `default` environment, on the provider named `anthropic`, with that model |
| `gpu/pi/deepseek/deepseek-v4-pro` | pi, in the environment named `gpu`, which must carry pi |
| `default/shell/none/none` | Runs the input as a shell command. It calls no model, so its provider and model are `none`. The template for new packages |

## What you get

- **The Responses API as it is.** `POST /v1/responses`, streaming, background, `previous_response_id`,
  cancel, and the Files and Containers APIs to send files in and read an agent's work out. No added fields.
- **Sandboxes you define.** An environment is an image and a script that sets it up, the harnesses it
  carries, CPU, memory, disk and time limits, variables, secrets, and a network. Any Linux image works. Nothing is reachable unless the network allows it: closed
  reaches the model provider only, limited adds the hosts you allow, open reaches any public host.
- **Secrets that never enter the sandbox.** The sandbox holds placeholders. The egress proxy, the only way
  out, puts the real value into requests to the hosts a secret is bound to.
- **Any provider.** A provider is a name, base URLs per API (Anthropic Messages, OpenAI Responses, OpenAI
  Chat Completions) and a key. A package picks the API its CLI speaks.
- **A trace of every turn.** The command that ran, its output, its log, what it cost in tokens and time,
  and every host it reached.
- **A harness package builder.** One of the packages that ship is an agent that writes harness packages.
  Ask it in the chat for a harness for another CLI, turn the folder it wrote into a draft, read and edit the
  files, and add it.
- **A console.** Chat with any harness and see what it made beside the reply; a transcript, a trace and the
  files of every session; a harness editor; a playground; manage providers, environments,
  harnesses and keys.

## Harness packages

`packages/shell` is the smallest package and the template. The packages for 26 agent CLIs, each unmodified
(Claude Code, Codex, pi, Hermes, OpenCode, goose, Aider and the rest), and the harness package builder (an agent
that writes packages), are in [harness-packages](https://github.com/HarnessGateway/harness-packages).

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
