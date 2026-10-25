# Pi

The minimal coding agent, headless over --mode json. The platform's session id is Pi's session id, so continuation needs no state; thinking levels, MCP servers and stateless runs pass through.

The CLI's documentation: <https://github.com/earendil-works/pi/tree/v1.3.0/packages/coding-agent/docs>.

## The package

| | |
|---|---|
| CLI | `pi` 1.3.0 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `tools`, `store`, `metadata` |
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

- models.json is written every turn with one provider, hg, whose api follows the provider's shape (anthropic-messages by default).
- MCP servers in the request's tools are written to .pi/mcp.json with exposure direct, so the agent sees them as tools.
- store: false runs with --no-session; everything else opens the session named after the platform's.

## Use it

```bash
hg harness new pi --catalog pi        # fork it into your organization
hg ask pi "Create hello.txt containing hello, then say done."
hg ask pi/myprovider/some-model "…"       # another provider and model
```

In the app: Providers and harnesses, Add a harness, **Pi**. The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
