# Template

A minimal package around a stand-in CLI: request.json read with jq, instructions into AGENTS.md, the CLI's output mapped onto Responses events by map.py. Fork it to start a package of your own.

The CLI's documentation: <https://github.com/HarnessGateway/harnessgateway/blob/main/docs/packages.md>.

## The package

| | |
|---|---|
| CLI | `echo-cli` 1.0.0 |
| Default provider | `(none: name one in the model path)` |
| Default model | `(the provider decides)` |
| Accepts | `input`, `instructions`, `previous_response_id`, `reasoning.effort`, `metadata` |
| Egress, install | `registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org` |
| Egress, run | none |

## Verified

On 2026-10-24, contract 7, provider `(none)`, model `(none)`: **3 of 3** checks passed. hg harness run, two turns, the stand-in CLI

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | passes |
| stream | a streamed turn delivers events as they happen | passes |

## Notes

- Set defaults.provider to a provider you registered and defaults.model to a model it serves.
- install.sh runs unprivileged in the base image; put the CLI under $HG_TOOLS.
- map.py is the whole mapping; the item shapes are the SDK's and the platform validates them.
- response.sh is optional: add one to shape the final response yourself instead of letting the events decide.

## Use it

```bash
hg harness new template --catalog template        # fork it into your organization
hg ask template "Create hello.txt containing hello, then say done."
hg ask template/myprovider/some-model "…"       # another provider and model
```

The fork's first commit is the catalog's package; pull it, change it and push it as your own from there.
