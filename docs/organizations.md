# Organizations and limits

The deployer runs the platform; organizations use it. The deployer's key (`hgd_…`) opens the
deployer's routes under `/v1`; it opens no organization's content.

## Organizations

`GET /v1/orgs` lists every organization with its status, limits and counts (sessions, responses,
running, queued); `POST /v1/orgs` with `{"name", "owner", "limits", "title"}` creates one and
returns the owner's first key (shown once); `PATCH /v1/orgs/{name}` sets `status` (`active` or
`suspended`: a suspended organization's keys, accounts and requests are refused until it is active
again), `limits` or `title`; `DELETE` removes it with everything it holds. With sign-up open,
organizations also create themselves, with the default limits.

## Limits

Every number an organization could influence has a ceiling on its record:

| Limit | Default | Applies to |
|---|---|---|
| `cpus` | 2 | each sandbox |
| `memory` | `4g` | each sandbox |
| `pids` | 512 | each sandbox |
| `run_timeout_s` | 3600 | the most a package may ask for a turn |
| `install_timeout_s` | 1200 | and for its install |
| `max_runs` | 4 | turns at once across the organization; the rest queue |
| `requests_per_minute` | 120 | turns created a minute; past it, `429` with `Retry-After` |
| `disk_mb` | 2048 | a session's workspace and home, snapshotted |
| `output_mb` | 16 | of a turn's output kept in its record |
| `upload_mb` | 64 | a package archive, or a request's files |

A package's `harness.json` asks for timeouts within these; the lower wins.

## The fleet

`GET /v1/workers` lists the nodes with their slots, running turns, version and last heartbeat;
`DELETE /v1/workers/{id}` forgets one; `GET /v1/fleet` is the platform in numbers. See [Nodes and
scale](workers.md).
