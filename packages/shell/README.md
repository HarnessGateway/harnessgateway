# shell

Runs the request's input as a shell command, in the session's work folder, and answers with what the command
printed. It needs no model provider.

It is the smallest harness package there is, and the one to copy when writing a new one.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="hg_...")
first = client.responses.create(model="shell", input="echo hello > note.txt; ls")
print(first.output_text)                     # note.txt

later = client.responses.create(model="shell", input="cat note.txt", previous_response_id=first.id)
print(later.output_text)                     # hello
```

A command that exits non-zero ends the turn as `failed`, with `metadata.hg_reason` `harness_failed` and
`metadata.hg_code` `command_failed`. What it printed is still in the response, and what it wrote is still in
the session.

## What a package is

| File | What it is |
|---|---|
| `harness.json` | The manifest: name, version, which provider APIs the harness speaks, the hosts it needs, its settings and secrets |
| `install` | Executable. Runs once per environment image. Puts the CLI under `/harness`. |
| `run` | Executable. Runs once per turn. Starts the CLI and prints what it does. |
| `seeds/mind/` | Optional. Files copied into the session's mind on first use. |
| `tests/<case>/` | Optional. Cases that `harnessgateway package check` runs. |

`install` and `run` can be written in any language. The platform runs them and never looks inside.

## What `run` is given

- The working directory is `/work`. `$HOME` is `/mind`. Both stay with the session; nothing else does.
- `/turn/request.json` is the Responses request exactly as it was sent. `/turn/handover.txt` is one line when
  another harness worked on the session last. `/turn/history.json` is the turns this harness has not seen.
  `/turn/files/<id>/<name>` are the uploaded files the request refers to. `Request.place_files()` puts them,
  and files sent inside the request, under `inputs/` in the work folder.
- Variables: `HG_MODEL`, `HG_PROVIDER_API`, `HG_PROVIDER_URL`, `HG_PROVIDER_KEY` (a placeholder; the real key
  is added outside the sandbox), `HG_STATE` (a folder in the mind for the package's own small state),
  `HG_HANDOVER`, and the rest of the table in the specification.

## What `run` prints

One JSON object per line on standard output:

- events of the OpenAI Responses stream that describe output (`response.output_item.added`,
  `response.output_text.delta`, …), without `sequence_number`;
- `{"type": "hg.log", "text": "…"}` for the turn's log;
- exactly one terminal line, last: `hg.done`, `hg.incomplete` or `hg.failed`.

Standard error is free text and goes to the turn's log. Exit 0.

The kit at `/harness/kit/hgkit.py` (Python, standard library only) reads the inputs and prints valid lines:
`Request`, `State`, `seed_mind`, `Cli`, `Emitter`. Using it is optional.

## Writing a new package

1. Copy this folder. Change `name`, `version` and `description` in `harness.json`. Set `mind` to the CLI's
   dot directory under `$HOME`, `apis` to the provider APIs it can speak, and the egress lists to the hosts
   its install and its turns need.
2. In `install`, install the CLI at a pinned version under `$HG_HARNESS/tools`, by the CLI's own documented
   method, and check the version you got.
3. In `run`, start the CLI headless with `hgkit.Cli`, giving it `request.brief()` (the handover, the
   instructions, where the request's files are, and the prompt, as one text). Translate each line it prints
   into `Emitter` calls, and keep the CLI's session id with `hgkit.State` so the next turn resumes it.
4. Record the CLI's real output into `tests/<case>/recording` beside a `request.json`, then:

```
harnessgateway package check . --record     # write each case's expected.jsonl
harnessgateway package check .              # from then on: compare against it
```

Under a check, `HG_REPLAY` names the recording and `hgkit.Cli` reads it in place of starting the CLI, so the
translation is tested without the CLI or a model.
