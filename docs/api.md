# The API

Every route takes `Authorization: Bearer <key>`: an organization member's key (`hg_…`) for the
organization's routes, the deployer's key (`hgd_…`) for the deployer's, or the console's cookie.
The interactive reference is at `/reference`. Bodies and responses are JSON; lists are
`{"object": "list", "data": […]}`; errors are `{"detail": "…"}` with the status that fits (400 a
bad request, 401 no credential, 403 the wrong one, 404 not reachable, 409 busy, 413 too large).

## The Responses API

| Route | Does |
|---|---|
| `POST /v1/responses` | a turn; the body is a Responses request ([Requests](requests.md)); `x-session-id` header on the response. Closing a foreground request stops its turn ([Requests](requests.md#closing-the-request)). An `Idempotency-Key` header makes a retry return the first turn; past the organization's rate ceiling the answer is 429 with `Retry-After` |
| `GET /v1/responses/{id}` | the response object; `?stream=true` streams it (a running one from its start) |
| `GET /v1/responses/{id}/detail` | plus the package's log and stderr, the worker, the timings, the raw status |
| `GET /v1/responses/{id}/request` | the request as sent |
| `POST /v1/responses/{id}/cancel` | stop it on purpose (the one way to end a background turn early) |
| `GET /v1/responses/{id}/captures`, `…/captures/{name}` | the provider exchanges the gateway relayed |

## Sessions and rodeos

| Route | Does |
|---|---|
| `GET /v1/sessions`, `GET /v1/sessions/{id}` | the member's sessions (an owner's organization's); one with its turns |
| `POST /v1/sessions/{id}/interrupt` | `{"input": "…"}`: steer the running turn |
| `GET /v1/sessions/{id}/trace` | every request, response, steer, cancel and disconnect |
| `GET /v1/sessions/{id}/files`, `…/files/{path}`, `…/archive` | the workspace after the last turn |
| `DELETE /v1/sessions/{id}` | remove it and everything it holds |
| `POST /v1/rodeos`, `GET /v1/rodeos`, `GET/PATCH/DELETE /v1/rodeos/{id}` | `{"task", "entries": [{"response"}]}`; the list with the leaderboard; `{"winner": "harness"}` |

## Setup

| Route | Does |
|---|---|
| `PUT /v1/providers/{name}`, `GET /v1/providers`, `DELETE …/{name}` | `{"url", "api", "key"}` (owners) |
| `GET /v1/catalog`, `GET /v1/catalog/{name}`, `…/files/{path}` | the shipped packages and their files |
| `POST /v1/harnesses/{name}` | `{"catalog": "pi"}`, `{"fork": "other"}` or `{"archive": "<base64 tar>"}`, with `"team"` |
| `PUT /v1/harnesses/{name}` | a tar (gzip accepted) as the next commit, or a new harness; `?team=`, `x-commit-message` |
| `GET /v1/harnesses`, `GET /v1/harnesses/{name}` | the harnesses the member can use; one with its files and configuration |
| `GET /v1/harnesses/{name}/files/{path}?at=` | a file at the head or at a commit |
| `POST /v1/harnesses/{name}/commits` | `{"changes": {"path": "text" or null}, "message", "parent"}`: a commit against the head `parent` |
| `GET /v1/harnesses/{name}/commits` | the history |
| `GET /v1/harnesses/{name}/archive` | the package as a tar |
| `PATCH /v1/harnesses/{name}` | `{"team": "platform"}` moves it |
| `DELETE /v1/harnesses/{name}` | remove it |

## Team

| Route | Does |
|---|---|
| `GET /v1/me` | who the key is: member, organization, role, teams, limits, plan |
| `GET /v1/org`, `PATCH /v1/org` | the organization: `{"title", "trace_sharing"}` ([Data](data.md)) |
| `GET /v1/usage?days=&team=&harness=` | what the organization used: compute, egress, storage, tokens shown ([Data](data.md)) |
| `GET /v1/members`, `POST /v1/members`, `PATCH/DELETE …/{name}` | members; `{"name", "role"}`; a role change; removal |
| `POST /v1/invites`, `GET /v1/invites`, `DELETE …/{email}` | invitations by email |
| `PUT /v1/teams/{name}`, `GET /v1/teams`, `GET/DELETE …/{name}` | `{"members": [...], "title"}` |
| `POST /v1/members/{name}/keys`, `GET /v1/keys`, `DELETE /v1/keys/{id}` | mint (`{"label"}`), list, revoke |
| `POST /v1/webhooks`, `GET /v1/webhooks`, `POST …/{id}/ping`, `DELETE …/{id}` | owners: `{"url", "label"}` returns the signing secret once; every final response is posted there ([Requests](requests.md#for-agents-retries-and-webhooks)) |

## Accounts

`/auth/providers` (what sign-in offers), `POST /auth/signup`, `POST /auth/login`, `POST
/auth/logout`, `GET /auth/me`, `GET /auth/verify`, `POST /auth/resend`, `POST /auth/reset`,
`POST /auth/reset/confirm`, `POST /auth/password`, `PATCH /auth/account`, `POST /auth/orgs`, `POST
/auth/switch`, `GET /auth/{github|google}` and its callback. The console uses these; a script uses
a key.

## The deployer

`GET /v1/platform/usage?days=`, `GET /v1/orgs/{name}/usage`, `GET /v1/dataset`, `GET /v1/dataset/summary`, `POST /v1/orgs`, `GET /v1/orgs`, `GET/PATCH/DELETE /v1/orgs/{name}`,
`GET /v1/workers`, `DELETE /v1/workers/{id}`, `GET /v1/fleet`. See [Organizations and limits](organizations.md).

## Workers

`/internal/*` is the workers' side: `join`, `heartbeat`, `claim`, `turns/{id}/started`, `chunks`,
`result`, and signed `blobs/{token}` reads and writes. A worker's credential opens nothing else.
