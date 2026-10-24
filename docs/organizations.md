# Organizations, plans and limits

The deployer runs the platform; organizations use it. The deployer's key (`hgd_…`) opens the
deployer's routes under `/v1` and the deployer's console; it opens no organization's content.

## Organizations

`GET /v1/orgs` lists every organization with its plan, status, limits and usage; `POST /v1/orgs`
with `{"name", "owner", "plan"}` creates one and returns the owner's first key (shown once);
`PATCH /v1/orgs/{name}` sets `status` (`active` or `suspended`: a suspended organization's keys,
accounts and requests are refused until it is active again), `plan`, `limits`, `trace_sharing` or
`billing_id`; `DELETE` removes it with everything it holds. With sign-up open, organizations also
create themselves, on the free plan.

## Plans

A plan is the ceilings an organization starts with and whether it may share traces.

| Plan | Starts with | Traces |
|---|---|---|
| `free` | 2 turns at once, 30 requests a minute, 30-minute turns, 1 GB per session | may share; does by default on a deployment with `HG_TRACE_SHARING=1` |
| `team` | 8 turns at once, 240 requests a minute | may share, off by default |
| `enterprise` | 32 turns at once, 1200 requests a minute, 8 GB per session | never |

Changing an organization's plan brings the new plan's ceilings unless `limits` are given in the
same request; the deployer may set any ceiling on any organization at any time. The plan is the
deployer's to set; an owner sees it on the Organization page.

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

A package's `harness.json` asks for timeouts within these; the lower wins. The plans above override
some defaults.

## Usage and billing

`GET /v1/platform/usage?days=30` is the platform's usage: totals, a row per day, per organization
and per harness, of turns, compute, egress, storage and tokens; `GET /v1/orgs/{name}/usage` is one
organization's, with rows per team, member and session. Counts and sizes only. With
`HG_BILLING=stripe`, every reading is also sent as a meter event for the organization's
`billing_id`. [Data, metering and trace sharing](data.md) defines each meter; the dashboard draws
them.

<figure class="shot" markdown>
![The deployer's dashboard](assets/console/dashboard.png)
</figure>

## The dataset

`GET /v1/dataset?since=&until=` exports, one JSON line per session, the traces of the organizations
that share them; `GET /v1/dataset/summary` counts them. What is in a row, and who is never in it,
is on [Data](data.md).
