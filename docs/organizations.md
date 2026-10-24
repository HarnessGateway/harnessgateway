# Organizations and limits

The deployer runs the platform; organizations use it. The deployer's key (`hgd_…`) opens the
deployer's routes under `/v1` and the deployer's console; it opens no organization's content.

## Organizations

`GET /v1/orgs` lists every organization with its status, limits and usage; `POST /v1/orgs` with
`{"name", "owner"}` creates one and returns the owner's first key (shown once); `PATCH
/v1/orgs/{name}` sets `status` (`active` or `suspended`: a suspended organization's keys, accounts
and requests are refused until it is active again) or `limits`; `DELETE` removes it with
everything it holds. With sign-up open, organizations also create themselves.

## Limits

Every number an organization could influence has a ceiling on its record, set by the deployer:

| Limit | Default | Applies to |
|---|---|---|
| `cpus` | 2 | each sandbox |
| `memory` | `4g` | each sandbox |
| `pids` | 512 | each sandbox |
| `run_timeout_s` | 3600 | the most a package may ask for a turn |
| `install_timeout_s` | 1200 | and for its install |
| `max_runs` | 4 | turns at once across the organization; the rest queue |
| `disk_mb` | 2048 | a session's workspace and home, snapshotted |
| `output_mb` | 16 | of a turn's output kept in its record |
| `upload_mb` | 64 | a package archive, or a request's files |

A package's `harness.json` asks for timeouts within these; the lower wins.

## Usage

`GET /v1/platform/usage?days=30` is the platform's usage: totals, a row per day, per organization and per
harness, of turns, sandbox seconds, provider calls and bytes relayed. Counts and sizes only. The
dashboard draws it.

<figure class="shot" markdown>
![The deployer's dashboard](assets/console/dashboard.png)
</figure>
