# Claude Code

Anthropic's coding agent, headless over stream-json. Any Anthropic-shaped provider: the default is DeepSeek's Anthropic-compatible endpoint.

The CLI's documentation: <https://code.claude.com/docs/en/cli-reference>.

## The package

| | |
|---|---|
| CLI | `claude` 2.1.310 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `tools`, `max_tool_calls`, `text.format`, `metadata` |
| Egress, install | `registry.npmjs.org` |
| Egress, run | none |
| Steer signal | `SIGUSR1` |

## Verified

On 2026-10-24, contract 7, provider `deepseek (anthropic-messages)`, model `deepseek-chat`: **3 of 3** checks passed. hg harness run against DeepSeek's Anthropic-compatible endpoint, two turns

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | passes |
| stream | a streamed turn delivers events as they happen | passes |

## Notes

- Point the provider at an Anthropic Messages endpoint (api: anthropic-messages); the model the request names is pinned for every alias the CLI uses.
- Continuation resumes the CLI's own session (--resume), kept in the session state; the transcript lives in the member's home, so the home is kept per member and package.
- MCP servers in the request's tools become --mcp-config with --strict-mcp-config: only those servers, nothing from the home.

## Use it

```bash
hg harness new claude-code --catalog claude-code        # fork it into your organization
hg ask claude-code "Create hello.txt containing hello, then say done."
hg ask claude-code/myprovider/some-model "…"       # another provider and model
```

In the console: Catalog, then Fork on **Claude Code**. The fork's first commit is the catalog's package; edit it as your own from there.
