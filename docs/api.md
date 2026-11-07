# The API

One contract: the OpenAI Responses API. Any OpenAI SDK pointed at `http://host:8000/v1` with a HarnessGateway key
works unchanged. The full route list is [`SPEC.md`](../SPEC.md) §14; this page is the shape of a conversation with
it. Every request carries `Authorization: Bearer <key or sign-in>`; every error is the OpenAI envelope
`{"error": {"message", "type", "param", "code"}}`.

## A turn

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_API_KEY=hg_…
curl $OPENAI_BASE_URL/responses -H "authorization: Bearer $OPENAI_API_KEY" -H 'content-type: application/json' \
     -d '{"model": "claude-code", "input": "Write hello.py and run it"}'
```

- `model` is `harness[/provider[/model]]`: the harness is a package the organization installed; the provider is one
  of its model endpoints (`PUT /v1/providers/{name}`), chosen by name, by the harness's default, or alone when it is
  the only one of an API the package speaks; the model part is passed to the CLI as `HG_MODEL`.
- `previous_response_id` continues a session. Naming the session's last response is the next turn; naming an earlier
  one forks a new session from the state saved after it; naming a response of another harness forks onto that harness
  with the workspace and the conversation (SPEC §9.3).
- `stream: true` returns the Responses event stream as SSE. `background: true` returns the queued Response at once;
  read it later with `GET /v1/responses/{id}` or follow it with `?stream=true&starting_after=N` from any api replica.
- `store: false` sessions are deleted ten minutes after their last turn.
- `tools` of type `mcp` select servers from the package's catalog or add new ones the organization's egress allows;
  an `authorization` or header value in the request is replaced by a placeholder the door swaps on the server's host.
- Files: upload with `POST /v1/files` and reference them with `file_id` input parts; they land in `/run/hg/files` and
  the package places them.

A Response carries `metadata.session_id`; a failed one carries `metadata.hg_reason`: `install_failed`, `box_lost`,
`sandbox_failed`, `timed_out`, `cancelled`, `invalid_output`, `exited_without_result`, `suspended`.

## Sessions

`GET /v1/containers` lists sessions (`?user=` by end user); `GET /v1/containers/{id}/responses` the turns in order;
`GET …/files` the workspace as the current snapshot lists it, with `POST …/files` staging a file for the next turn
and `GET …/files/{fid}/content` reading one; `DELETE /v1/containers/{id}` ends the session and releases its storage.
`GET /v1/traces/{response_id}` and `GET /v1/containers/{id}/traces` return the observability records: the harness run,
the door's connections, the provider calls.

## Management

| What | Routes |
|---|---|
| harnesses | `POST /v1/harnesses` (multipart `package`), `GET/PATCH/DELETE /v1/harnesses/{name}`, `GET …/files`, `GET …/files/{path}`, `GET/POST …/install` (the install's state; retry) |
| providers, egress | `PUT/GET/DELETE /v1/providers/{name}`, `GET/PUT /v1/egress` |
| keys, accounts, SSO, audit | `GET/POST /v1/keys`, `DELETE /v1/keys/{id}`, `GET/POST /v1/accounts`, `GET/PATCH /v1/accounts/{id}`, `POST …/reset`, `GET/PUT/DELETE /v1/sso`, `GET /v1/audit` |
| people | `POST /v1/auth/login`, `/accept`, `/logout`, `/password`, `GET /v1/auth/me`; `GET /v1/auth/sso`, `GET /v1/auth/sso/start?redirect=`, `POST /v1/auth/sso/exchange` |
| usage | `GET /v1/usage?start&end&group_by=harness\|session\|day\|created_by\|user` |
| the platform scope | `GET /internal/health`, `/metrics`, `/boxes`, `/config`, `/versions`, `/audit`, `/usage`, `GET/PATCH /internal/orgs/{id}`, `POST …/accounts`, `POST /internal/boxes/{id}/stop` (operator key or a platform admin) |

Roles (SPEC §8.4): a **run-only** key creates turns and reads what it made; a **developer** also installs and configures
harnesses and manages its own keys; an **admin** everything in the organization. The platform scope is the operator
key (`HG_OPERATOR_KEY`) or a platform admin's sign-in.

## What a box calls

A box (the runner inside a sandbox) authenticates with its box token at `/internal/boxes/{id}/…`: `GET` (what it is),
`POST …/ready`, `GET …/next?wait=` (its session's next turn, long-polled), `POST …/turns/{rid}/events|heartbeat|finish`,
`PUT …/turns/{rid}/manifest`, `POST …/turns/{rid}/saved`, `PUT …/turns/{rid}/logs/{stdout|stderr}`,
`GET …/objects/{key}`, `POST …/blobs` (which hashes are missing), `GET/PUT …/blobs/{hash}`, `PUT …/archive`,
`POST …/installed`, `POST …/bye`. A door authenticates with the door key at `POST /internal/door/policy` and
`POST /internal/door/records`. These are the platform's own; applications never call them.
