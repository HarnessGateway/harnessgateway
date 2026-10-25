# Hermes Agent

Nous Research's Hermes Agent in quiet chat mode on an Anthropic-shaped provider. The answer arrives whole; sessions resume by id with their history.

The CLI's documentation: <https://hermes-agent.nousresearch.com/docs/reference/cli-commands>.

## The package

| | |
|---|---|
| CLI | `hermes` 0.19.0 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `metadata` |
| Egress, install | `pypi.org`, `files.pythonhosted.org` |
| Egress, run | none |

## Verified

On 2026-10-24, contract 7, provider `deepseek (anthropic-messages)`, model `deepseek-chat`: **2 of 3** checks passed. hg harness run against DeepSeek, two turns; no event stream in 0.19.0

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | passes |
| stream | a streamed turn delivers events as they happen | does not pass |

Not passing: stream. See the notes.

## Notes

- Hermes 0.19.0 prints no event stream (the documented chat -q --format stream-json arrived in a later release), so the response is the final text; tool calls are not itemized and no token usage is reported.
- One-shot mode (-z) reports usage but does not carry a resumed session's history in 0.19.0, so the package uses chat -q -Q, which does.
- The provider is Anthropic-shaped (api: anthropic-messages); Hermes's config.yaml is written every turn with the gateway URL and the model.

## Use it

```bash
hg harness new hermes --catalog hermes        # fork it into your organization
hg ask hermes "Create hello.txt containing hello, then say done."
hg ask hermes/myprovider/some-model "…"       # another provider and model
```

In the app: Providers and harnesses, Add a harness, **Hermes Agent**. The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
