# Any CLI

The platform runs command lines in sandboxes and speaks the Responses API about them. Coding
agents are the command lines it happens to ship packages for; nothing in it knows what an agent is.
A package is the adapter between one CLI and the wire, and the platform is the same for every
package: the request arrives verbatim, the package runs the CLI in a sandbox the platform owns, the
events the package prints become the response, and everything about it is traced, metered and kept.

## The schema, on both sides

**In:** the body of `POST /v1/responses` is an [OpenAI Responses request](requests.md), validated
against the SDK's own schema and written to `$HG_REQUEST` as `request.json`, whole. The package
reads what its CLI can use; `harness.json` says which fields that is (`accepts`), and the platform
tells a client nothing else was read.

**Out:** `request.sh` prints JSON lines. A line whose `type` starts with `response.` is a Responses
stream event, relayed to the client as it is printed; the `response.output_item.done` events
assemble the response's `output`, and `response.completed` carries the usage. Any other line is the
turn's log. The item shapes are the SDK's (`message`, `shell_call`, `apply_patch_call`,
`function_call`, `reasoning`, …) and the platform validates each before a client sees it. A
package that would rather shape the response at the end writes `response.sh`. [Write a
package](packages.md) has every field and every variable.

## The shell package

The catalog's `shell` package is the whole idea in forty lines: the request's `input` is run as a
shell script in the session's workspace, `instructions` run first as a setup step, and the output
is one `shell_call`, its `shell_call_output` with stdout, stderr and the exit code, and a message
carrying stdout so `output_text` is what the command printed.

```bash
hg harness new sh --catalog shell
hg ask sh "echo hello > a.txt && wc -c < a.txt"           # 6
hg ask sh --previous resp_… "cat a.txt"                     # hello: the workspace persisted
```

```python
r = client.responses.create(model="sh", input="python3 -c 'print(2**64)'")
print(r.output_text)
```

It declares no provider: `"provider_env": {}`. A package that names no provider environment
variables runs with no session token and no gateway forwarder, so nothing in its sandbox can reach a
model provider, and its only way out is the package's egress list (empty here). No provider has to
be registered to use it.

## Wrapping a CLI of your own

Fork `shell` or `template` and change three things:

1. **`install.sh`**: put the CLI under `$HG_TOOLS` (npm, pip, a release tarball; the egress list under
   `install` names the hosts it may fetch from). It runs once per package commit, unprivileged.
2. **`request.sh`**: read the fields you want from `$HG_REQUEST` with `jq`, run the CLI in
   `$HG_WORKSPACE`, pipe its output through a mapper that prints Responses events. Keep the CLI's
   resume handle in `$HG_STATE` so `previous_response_id` continues it.
3. **`harness.json`**: the CLI's name and version, `accepts`, the egress lists, the timeouts, the
   steer signal, and `provider_env` if the CLI calls a model provider (the platform then puts the
   gateway's address and a session token in those variables; the real key never enters the sandbox).

Every package gets the same sandbox: a fresh container per turn as a uid unique to the organization
or the team, a read-only root, no capabilities, the organization's cpu, memory and pid ceilings, one
door out to the listed hosts, a workspace and a home that persist for the session, and a trace of
every request, response, steer and provider exchange. What the package is for is the package's
business.
