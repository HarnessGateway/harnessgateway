# The API

Every route takes `Authorization: Bearer <key>`: an organization member's key (`hg_…`) for the
organization's routes, the deployer's key (`hgd_…`) for the deployer's, or the app's cookie. The
interactive reference is at `/reference`. Bodies and responses are JSON; lists are
`{"object": "list", "data": […]}`; errors are `{"detail": "…"}` with the status that fits (400 a
bad request, 401 no credential, 403 the wrong one, 404 not reachable, 409 busy, 429 the ceiling).

## The Responses API

| Route | Does |
|---|---|
| `POST /v1/responses` | a turn; the body is a Responses request ([Requests](requests.md)); `x-session-id` header on the response. Closing a foreground request stops its turn; a request on a session that is running a turn supersedes it ([Sessions](sessions.md)). An `Idempotency-Key` header makes a retry return the first turn; past the organization's rate ceiling the answer is 429 with `Retry-After` |
| `GET /v1/responses/{id}` | the response object; `?stream=true` streams it (a running one from its start) |
| `GET /v1/responses/{id}/detail` | plus the package's log and stderr, the worker, the timings, the raw status |
| `GET /v1/responses/{id}/request` | the request as sent |
| `POST /v1/responses/{id}/cancel` | stop it on purpose |
| `GET /v1/responses/{id}/captures`, `…/captures/{name}` | the provider exchanges the gateway relayed |

## Sessions

| Route | Does |
|---|---|
| `GET /v1/sessions`, `GET /v1/sessions/{id}` | the member's sessions (an owner's organization's); one with its turns |
| `GET /v1/sessions/{id}/trace` | every request, response, cancel, disconnect and supersede |
| `GET /v1/sessions/{id}/files`, `…/files/{path}`, `…/archive` | the workspace after the last turn |
| `DELETE /v1/sessions/{id}` | remove it and everything it holds |

## Setup

| Route | Does |
|---|---|
| `PUT /v1/providers/{name}`, `GET /v1/providers`, `GET/DELETE …/{name}` | `{"url", "api", "key"}` (owners) |
| `GET /v1/catalog`, `GET /v1/catalog/{name}`, `…/files/{path}` | the shipped packages and their files |
| `POST /v1/harnesses/{name}` | `{"catalog": "pi"}`, `{"fork": "other"}` or `{"archive": "<base64 tar>"}` (owners) |
| `PUT /v1/harnesses/{name}` | a tar (gzip accepted) as the next commit, or a new harness; `x-commit-message` (owners) |
| `GET /v1/harnesses`, `GET /v1/harnesses/{name}` | the harnesses; one with its files and configuration |
| `GET /v1/harnesses/{name}/files/{path}?at=` | a file at the head or at a commit |
| `POST /v1/harnesses/{name}/commits` | `{"changes": {"path": "text" or null}, "message", "parent"}`: a commit against the head `parent` (owners) |
| `GET /v1/harnesses/{name}/commits` | the history |
| `GET /v1/harnesses/{name}/archive` | the package as a tar |
| `DELETE /v1/harnesses/{name}` | remove it (owners) |

## The organization

| Route | Does |
|---|---|
| `GET /v1/me` | who the key is: member, organization, role, limits |
| `GET /v1/org`, `PATCH /v1/org` | the organization; `{"title"}` (owners) |
| `GET /v1/members`, `POST /v1/members`, `PATCH/DELETE …/{name}` | members; `{"name", "role"}`; a role change; removal (owners) |
| `POST /v1/invites`, `GET /v1/invites`, `DELETE …/{email}` | invitations by email (owners) |
| `POST /v1/members/{name}/keys`, `GET /v1/keys`, `DELETE /v1/keys/{id}` | mint (`{"label"}`), list, revoke |

## Accounts

`GET /auth/providers` (what sign-in offers), `POST /auth/signup`, `POST /auth/login`, `POST
/auth/logout`, `GET /auth/me`, `POST /auth/orgs`, `POST /auth/switch`. The app uses these; a
script uses a key.

## The deployer

`POST /v1/orgs`, `GET /v1/orgs`, `GET/PATCH/DELETE /v1/orgs/{name}`, `GET /v1/workers`, `DELETE
/v1/workers/{id}`, `GET /v1/fleet`. See [Organizations and limits](organizations.md).

## Nodes

`/internal/*` is the nodes' side: `join`, `heartbeat`, `claim`, `turns/{id}/started`, `chunks`,
`result`, and signed `blobs/{token}` reads and writes. A node's credential opens nothing else.
