# Oh My Pi

The Pi fork with its own extensions, headless over --mode json. Sessions resume by the id the stream announces; thinking levels, MCP servers, service tiers and stateless runs pass through.

The CLI's documentation: <https://github.com/can1357/oh-my-pi/blob/main/docs/cli-reference.md>.

## The package

| | |
|---|---|
| CLI | `omp` 18.12.0 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `tools`, `store`, `service_tier`, `metadata` |
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

- models.yml is written every turn with one provider, hg, whose api follows the provider's shape (anthropic-messages by default).
- MCP servers in the request's tools are written to .omp/mcp.json.
- The session id comes from the first line of the JSON stream and is kept for --resume.

## Use it

```bash
hg harness new omp --catalog omp        # fork it into your organization
hg ask omp "Create hello.txt containing hello, then say done."
hg ask omp/myprovider/some-model "…"       # another provider and model
```

The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
