# OpenClaw

OpenClaw's agent in exec mode: one JSON envelope per turn with the answer and usage. Continuity is the state directory kept with the session, since exec cannot name a prior session.

The CLI's documentation: <https://docs.openclaw.ai/cli/agent>.

## The package

| | |
|---|---|
| CLI | `openclaw` 2026.10.6 |
| Default provider | `deepseek` |
| Default model | `deepseek-chat` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `metadata` |
| Egress, install | `registry.npmjs.org`, `nodejs.org` |
| Egress, run | none |
| Steer signal | `SIGUSR1` |

## Verified

On 2026-10-24, contract 7, provider `deepseek (anthropic-messages)`, model `deepseek-chat`: **1 of 3** checks passed. hg harness run against DeepSeek; exec starts a new agent session per turn (the workspace carries over, the conversation does not); one envelope, no stream

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | does not pass |
| stream | a streamed turn delivers events as they happen | does not pass |

Not passing: recall, stream. See the notes.

## Notes

- OpenClaw needs Node 24; install.sh puts a private Node under the tools layer.
- The provider is written into a per-turn config file (models.providers.hg) with the gateway as baseUrl; --config pins it, so nothing ambient applies.
- exec emits no events while it runs: the response arrives whole, and tool calls appear only as counts in its summary.

## Use it

```bash
hg harness new openclaw --catalog openclaw        # fork it into your organization
hg ask openclaw "Create hello.txt containing hello, then say done."
hg ask openclaw/myprovider/some-model "…"       # another provider and model
```

In the console: Catalog, then Fork on **OpenClaw**. The fork's first commit is the catalog's package; edit it as your own from there.
