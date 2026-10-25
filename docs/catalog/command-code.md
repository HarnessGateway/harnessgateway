# Command Code

Command Code in headless JSON mode with a bring-your-own provider (DeepSeek's Anthropic-compatible endpoint by default). Text streams token by token; sessions resume by id; effort and a turn cap pass through.

The CLI's documentation: <https://commandcode.ai/docs/headless>.

## The package

| | |
|---|---|
| CLI | `command-code` 1.88.0 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `max_tool_calls`, `tools`, `store`, `metadata` |
| Egress, install | `registry.npmjs.org` |
| Egress, run | none |

## Verified

On 2026-10-24, contract 7, provider `deepseek (anthropic-messages)`, model `deepseek-chat`: **0 of 3** checks passed. not verified: 1.88.0 in headless mode refuses to run without a Command Code account (cmd login), even with a bring-your-own provider and --local-only

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | does not pass |
| recall | a second turn, continued with previous_response_id, recalls it | does not pass |
| stream | a streamed turn delivers events as they happen | does not pass |

Not passing: make, recall, stream. See the notes.

## Notes

- providers.json is written every turn with one BYOK provider, hg, whose api follows the provider's shape; --local-only keeps the CLI off its own backend.
- --yolo is required in -p mode: --permission-mode yolo alone does not unblock writes.
- Only the tool_running event is documented; the other event names come from the 1.88.0 package and may change with a release.

## Use it

```bash
hg harness new command-code --catalog command-code        # fork it into your organization
hg ask command-code "Create hello.txt containing hello, then say done."
hg ask command-code/myprovider/some-model "…"       # another provider and model
```

In the app: Providers and harnesses, Add a harness, **Command Code**. The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
