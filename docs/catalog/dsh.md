# DeepSeek Harness

DeepSeek's own harness in its headless profile with JSON events, on DeepSeek's Anthropic-compatible endpoint. Sessions resume by id; the reasoning effort maps onto the DeepSeek route's levels.

The CLI's documentation: <https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.0-rc.2>.

## The package

| | |
|---|---|
| CLI | `dsh` 0.2.0-rc.2 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `metadata` |
| Egress, install | `registry.npmjs.org` |
| Egress, run | none |

## Verified

On 2026-10-24, contract 7, provider `deepseek (anthropic-messages)`, model `deepseek-chat`: **3 of 3** checks passed. hg harness run against DeepSeek, two turns

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | passes |
| stream | a streamed turn delivers events as they happen | passes |

## Notes

- The provider is DeepSeek's Anthropic-compatible endpoint (api: anthropic-messages); the patch sets the llm-deepseek row's baseURL to the gateway and its key to the session token.
- DSH_PERMISSION_MODE is danger-full-access inside the platform's sandbox; session-log upload to DeepSeek is disabled by the patch.
- Text and thinking arrive per committed block, not per token.

## Use it

```bash
hg harness new dsh --catalog dsh        # fork it into your organization
hg ask dsh "Create hello.txt containing hello, then say done."
hg ask dsh/myprovider/some-model "…"       # another provider and model
```

The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
