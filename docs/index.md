# HarnessGateway

HarnessGateway speaks the [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
and nothing else. Behind it, a *harness package* an organization owns maps each request onto a
command line, usually a coding-agent CLI (Claude Code, Codex, Pi, Hermes, Kilo, Cline, …), running
in a sandbox the platform owns, with the organization's provider keys injected for the length of
one turn. Switch harnesses by changing the model name; wrap [any CLI](any-cli.md) the same way.

```python
from openai import OpenAI
client = OpenAI(base_url="https://hg.example.com/v1", api_key="hg_…")
r = client.responses.create(model="codex", input="Add a --dry-run flag to bin/deploy", stream=True)
for event in r:
    print(event.type)
```

## The bet

**Harness packages are corporate assets.** How an organization runs its agents (the CLI, its
version, the instructions, the tools, the MCP servers, the seeds) is a git repository the
organization keeps on the platform, forked from the catalog or written from scratch, edited in the
console, pushed from the command line, owned by a team. The platform never decides any of it.

**The platform provides the execution.** Every turn runs in a fresh hardened sandbox as a uid of
its organization or team, with one door out, provider keys that never enter it, and a workspace that
persists for the session. The wire is the Responses API: validated against the SDK's schema,
streamed, continued with `previous_response_id`, run in the background, steered, cancelled, or
stopped by closing the request. Everything is traced, metered and kept.

**One product, two deployments.** The managed platform and a self-hosted one run the same image;
they differ in the backends their environment names (SQLite or Postgres, a directory or S3, a
master key or a KMS, no billing or a billing system) and in who the deployer is. The platform bills
compute, egress and storage; tokens are between an organization and its provider.

**Traces, when shared.** An organization on a free or team plan may share the traces of its
sessions, pseudonymized, for the deployer to train coding agents on; an enterprise organization
never does. [Data, metering and trace sharing](data.md) says exactly what that means.

## In this documentation

| You want to | Read |
|---|---|
| run it | [Deploy](deploy.md), [First boot and sign-in](initialize.md) |
| use it | [The console](console.md), [Providers](providers.md), [Harnesses](harnesses.md), [Requests](requests.md) |
| talk to a harness, compare harnesses | [Conversations and sessions](conversations.md), [Rodeo](rodeo.md) |
| work as a team | [Teams, members and keys](teams.md) |
| script it, call it from an agent | [The hg command line](cli.md), [The API](api.md), [retries and webhooks](requests.md#for-agents-retries-and-webhooks) |
| write or change a package, wrap a CLI | [Write a package](packages.md), [Any CLI](any-cli.md), [The catalog](catalog/index.md), [The Responses wire](responses-wire.md) |
| operate it | [Organizations, plans and limits](organizations.md), [Nodes and scale](workers.md), [Configuration](configuration.md), [Data](data.md), [Security](security.md) |

The platform is Apache-2.0, at [github.com/HarnessGateway/harnessgateway](https://github.com/HarnessGateway/harnessgateway).
