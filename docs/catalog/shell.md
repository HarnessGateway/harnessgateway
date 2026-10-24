# Shell

Any command line as a harness: the input is run as a shell script in the session's workspace and its output is the answer. No provider is needed. Fork it to wrap a CLI of your own, agent or not.

The CLI's documentation: <https://github.com/HarnessGateway/harnessgateway/blob/main/docs/any-cli.md>.

## The package

| | |
|---|---|
| CLI | `bash` 5.2 |
| Default provider | `(none: name one in the model path)` |
| Default model | `(the provider decides)` |
| Accepts | `input`, `instructions`, `previous_response_id`, `metadata` |
| Egress, install | none |
| Egress, run | none |
| Steer signal | `SIGUSR1` |

## Verified

On 2026-10-24, contract 7, provider `(none)`, model `(none)`: **3 of 3** checks passed. the API suite, through a worker: a first turn writes a file, a continuation reads it, a streamed turn

| Check | Means | Result |
|---|---|---|
| make | a first turn writes a file and answers | passes |
| recall | a second turn, continued with previous_response_id, recalls it | passes |
| stream | a streamed turn delivers events as they happen | passes |

## Notes

- provider_env is empty, so the platform issues no token and starts no gateway forwarder: the sandbox has no way out but the run egress list (empty here).
- instructions run first as a setup script; a steer runs as one more script after the current one ends.
- The workspace persists for the session: continue with previous_response_id and the files are there.
- Add egress hosts in harness.json to let the script reach the network; add a CLI in install.sh under $HG_TOOLS.

## Use it

```bash
hg harness new shell --catalog shell        # fork it into your organization
hg ask shell "Create hello.txt containing hello, then say done."
hg ask shell/myprovider/some-model "…"       # another provider and model
```

In the console: Catalog, then Fork on **Shell**. The fork's first commit is the catalog's package; edit it as your own from there.
