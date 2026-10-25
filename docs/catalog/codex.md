# Codex

OpenAI's Codex CLI in exec mode, on any provider that speaks the Responses API (OpenAI by default). Thread ids are kept for continuation; reasoning effort, summary, verbosity and a JSON schema for the answer all pass through.

The CLI's documentation: <https://learn.chatgpt.com/docs/non-interactive-mode>.

## The package

| | |
|---|---|
| CLI | `codex` 0.163.0 |
| Default provider | `openai` |
| Default model | `gpt-5.4-mini` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `reasoning.summary`, `text.format`, `text.verbosity`, `tools`, `metadata` |
| Egress, install | `registry.npmjs.org` |
| Egress, run | none |

## Verified

On 2026-10-24, contract 7, provider `openai (openai-responses)`, model `gpt-5.4-mini`: **3 of 3** checks passed. hg harness run against OpenAI, two turns

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | passes |
| stream | a streamed turn delivers events as they happen | passes |

## Notes

- The provider must speak the Responses API (api: openai-responses): Codex's wire_api is responses only.
- Codex's own sandbox is set to danger-full-access: the platform's sandbox is the boundary.
- MCP servers in the request's tools are written into config.toml as [mcp_servers.<label>] with a bearer token from the environment.

## Use it

```bash
hg harness new codex --catalog codex        # fork it into your organization
hg ask codex "Create hello.txt containing hello, then say done."
hg ask codex/myprovider/some-model "…"       # another provider and model
```

The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
