# Cline

Cline's CLI in JSON mode on an OpenAI-compatible provider (DeepSeek's OpenAI endpoint by default). Text, reasoning and tool content become items as each piece ends.

The CLI's documentation: <https://docs.cline.bot/cli/cli-reference>.

## The package

| | |
|---|---|
| CLI | `cline` 3.0.73 |
| Default provider | `deepseek-openai` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `metadata` |
| Egress, install | `registry.npmjs.org` |
| Egress, run | none |
| Steer signal | `SIGUSR1` |

## Verified

On 2026-10-24, contract 7, provider `deepseek-openai (openai-chat)`, model `deepseek-chat`: **2 of 3** checks passed. hg harness run against DeepSeek's OpenAI endpoint; the second turn did not recall the first (--id resumes only in interactive mode in 3.0.73)

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | does not pass |
| stream | a streamed turn delivers events as they happen | passes |

Not passing: recall. See the notes.

## Notes

- The provider is OpenAI-compatible (api: openai-chat); Cline's providers.json is written every turn with the gateway URL and the model.
- Cline's --id is documented to resume a session, but the 3.0.73 source honours it only in interactive mode: a continuation keeps the workspace and the home, and the agent's own memory of earlier turns may not carry.
- --auto-approve true is used rather than -y: -y runs a reduced tool set and disables hooks.

## Use it

```bash
hg harness new cline --catalog cline        # fork it into your organization
hg ask cline "Create hello.txt containing hello, then say done."
hg ask cline/myprovider/some-model "…"       # another provider and model
```

In the console: Catalog, then Fork on **Cline**. The fork's first commit is the catalog's package; edit it as your own from there.
