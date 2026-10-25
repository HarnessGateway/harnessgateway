# HarnessGateway

HarnessGateway speaks the [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
and nothing else. Behind it, a *harness package* an organization owns maps each request onto a
command line, usually a coding-agent CLI (Claude Code, Codex, Pi, Hermes, Kilo, Cline, …), running
in a sandbox the platform owns, with the organization's provider keys swapped in for one request at
a time. Switch harnesses by changing the model name; wrap [any CLI](any-cli.md) the same way.

```python
from openai import OpenAI
client = OpenAI(base_url="https://hg.example.com/v1", api_key="hg_…")
r = client.responses.create(model="codex", input="Add a --dry-run flag to bin/deploy", stream=True)
for event in r:
    print(event.type)
```

## The platform

**Organizations own their harnesses.** How an organization runs its agents (the CLI, its version,
the instructions, the tools, the MCP servers, the seeds) is a git repository the organization keeps
on the platform, forked from the catalog or uploaded, pushed from the command line. The platform
never decides any of it.

**The platform provides the execution.** Every turn runs in a fresh hardened sandbox as a uid of
its organization, with one door out, provider keys that never enter it, and a workspace that
persists for the session. The wire is the Responses API: validated against the SDK's schema,
streamed, continued with `previous_response_id`, run in the background, cancelled, or stopped by
closing the request. A new request on a session that is running a turn stops that turn and runs the
new one on the workspace it left, the way a coding agent's web view takes the next message.
Everything is traced and kept.

**One product, two deployments.** The managed platform and a self-hosted one run the same image;
they differ in the backends their environment names (SQLite or Postgres, a directory or S3, a master
key or a KMS) and in who the deployer is.

## In this documentation

| You want to | Read |
|---|---|
| run it | [Deploy](deploy.md), [First boot and sign-in](initialize.md) |
| use it | [Providers](providers.md), [Harnesses](harnesses.md), [Sessions](sessions.md), [Requests](requests.md) |
| script it, call it from an agent | [The hg command line](cli.md), [The API](api.md) |
| write or change a package, wrap a CLI | [Write a package](packages.md), [Any CLI](any-cli.md), [The catalog](catalog/index.md), [The Responses wire](responses-wire.md) |
| operate it | [Members and keys](members.md), [Organizations and limits](organizations.md), [Nodes and scale](workers.md), [Configuration](configuration.md), [Security](security.md) |

The platform is Apache-2.0, at [github.com/HarnessGateway/harnessgateway](https://github.com/HarnessGateway/harnessgateway).
