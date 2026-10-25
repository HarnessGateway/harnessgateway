# Kilo Code

Kilo's CLI (an OpenCode fork) in run mode with JSON events, on an Anthropic-shaped provider. Sessions resume by id; the provider is configured through KILO_CONFIG_CONTENT every turn.

The CLI's documentation: <https://kilo.ai/docs/code-with-ai/platforms/cli-reference>.

## The package

| | |
|---|---|
| CLI | `kilo` 7.8.12 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `metadata` |
| Egress, install | `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` |
| Egress, run | none |

## Verified

On 2026-10-24, contract 7, provider `deepseek (anthropic-messages)`, model `deepseek-chat`: **3 of 3** checks passed. hg harness run against DeepSeek, two turns

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | passes |
| stream | a streamed turn delivers events as they happen | passes |

## Notes

- The provider is Anthropic-shaped (api: anthropic-messages), configured as Kilo's built-in anthropic provider with the gateway as its baseURL.
- --auto approves every permission not denied: the platform's sandbox is the boundary.
- Kilo's JSON events carry whole parts, not token deltas, so text arrives per completed part.

## Use it

```bash
hg harness new kilo --catalog kilo        # fork it into your organization
hg ask kilo "Create hello.txt containing hello, then say done."
hg ask kilo/myprovider/some-model "…"       # another provider and model
```

The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
