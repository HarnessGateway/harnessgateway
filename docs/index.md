# HarnessGateway

HarnessGateway speaks the [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
and nothing else. Behind it, a *harness package* maps each request onto a coding-agent CLI (Claude
Code, Codex, Pi, Hermes, Kilo, Cline, …) running in a sandbox the platform owns, with your
provider keys injected for the length of one turn. Switch harnesses by changing the model name.

```python
from openai import OpenAI
client = OpenAI(base_url="https://hg.example.com/v1", api_key="hg_…")
r = client.responses.create(model="codex", input="Add a --dry-run flag to bin/deploy", stream=True)
for event in r:
    print(event.type)
```

## The division of labour

**The package owns the mapping.** A package is a git repository your organization keeps on the
platform: `harness.json`, `install.sh`, `request.sh`, an optional `response.sh`, and the *seeds*
(files the CLI reads from the workspace and the home, such as `AGENTS.md` and an MCP config). The
client's request is handed to `request.sh` whole, as `request.json`; the package reads what its CLI
can use and prints Responses stream events as the CLI runs. Nothing in between invents a schema.
Fork a package from the catalog, edit it in the console, commit; or `hg harness pull`, change it,
`hg harness push`.

**The platform owns everything else.** The wire (validation against the SDK's own schema, streaming,
`previous_response_id`, background turns, steering and cancel), managed multi-tenant sandboxing
(a fresh hardened container per turn; the tenant controls only the egress list), provider keys that
never enter a sandbox, tracing of every request and response, the organization and team hierarchy,
conversations with a workspace view, and rodeos that run one task across several harnesses so you
can pick the winner.

## In this documentation

| You want to | Read |
|---|---|
| run it | [Deploy](deploy.md), [First boot and sign-in](initialize.md) |
| use it | [The console](console.md), [Providers](providers.md), [Harnesses](harnesses.md), [Requests](requests.md) |
| talk to a harness, compare harnesses | [Conversations and sessions](conversations.md), [Rodeo](rodeo.md) |
| work as a team | [Teams, members and keys](teams.md) |
| script it | [The hg command line](cli.md), [The API](api.md) |
| write or change a package | [Write a package](packages.md), [The catalog](catalog/index.md), [The Responses wire](responses-wire.md) |
| operate it | [Organizations and limits](organizations.md), [Workers and scale](workers.md), [Configuration](configuration.md), [Security](security.md) |

The platform is Apache-2.0, at [github.com/HarnessGateway/harnessgateway](https://github.com/HarnessGateway/harnessgateway).
