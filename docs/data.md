# Data: what is kept, metered and shared

The platform keeps an organization's work for the organization, meters what the work costs the
platform, and, when an organization says so, shares the traces of that work. This page says
exactly what each of those means. A self-hosted deployment and the managed one run the same
code; what differs is who the deployer is.

## What an organization's work is

| Kept | Where | For whom |
|---|---|---|
| Harness packages, every commit | the organization's repositories | its members, by team |
| Requests, as sent; responses, as returned; steers, cancels, disconnects | the session's trace | its members, by reach |
| The sandbox's output as it ran, the package's log and stderr | the turn's record | its members, by reach |
| Workspace and home snapshots | blobs, `tenants/<org>/sessions` and `…/members` | its members, by reach |
| Provider exchanges the gateway relayed, with the key removed | the turn's captures | its members, by reach |
| Provider keys | sealed with the deployment's key management | the gateway process, for one call at a time |

Reach is organization, then team, then member ([Teams](teams.md)). The deployer's routes return
counts and sizes of all of this and the content of none of it, except through the dataset below.
Deleting a session deletes its records, chunks, trace and blobs; deleting an organization deletes
everything it holds.

## What is metered

Every turn writes one usage record. The platform meters what the turn cost the platform, and
shows what it cost the organization's provider:

| Meter | What it counts | Billed? |
|---|---|---|
| `cpu_seconds` | the sandbox's CPU ceiling × the turn's wall seconds | yes |
| `memory_gb_seconds` | the sandbox's memory ceiling, in GB × wall seconds | yes |
| `egress_bytes` | bytes the sandbox sent out, through the door and through the gateway | yes |
| `storage_gb_days` | snapshots, packages and homes kept, sampled once a day | yes |
| `input_tokens`, `output_tokens` | what the provider counted in its responses | no: shown only |

Tokens are the organization's business with its provider. The platform never marks them up and
never needs to see the provider's invoice. The ceilings a plan starts with are on
[Organizations](organizations.md).

`GET /v1/usage?days=30&team=&harness=` is the organization's view: totals, a row per day, per
harness, per team, per member and per session, the latest storage sample, and the meaning of each
meter. An owner sees the organization; a member their own turns and their teams'. The deployer's
`GET /v1/platform/usage` and `GET /v1/orgs/{name}/usage` are the same numbers across organizations.
With `HG_BILLING=stripe`, every reading is also sent to the billing system as a meter event for
the organization's `billing_id`.

## Trace sharing

A deployment may offer to share traces: the full record of sessions, exported to train models on
how coding agents work. Sharing is per organization, dated, and visible:

- **Who shares.** An organization on the free or team plan whose owner turned sharing on (`PATCH
  /v1/org {"trace_sharing": true}`, or the organization settings in the console). On a managed
  deployment with `HG_TRACE_SHARING=1`, an organization that signs up on the free plan shares until
  an owner turns it off; the sign-up page says so. An **enterprise** organization never shares,
  whatever is set, and setting its plan to enterprise ends sharing at once.
- **What is shared.** Sessions created after sharing was turned on: for each turn the request as
  sent, the response as returned, every steer, cancel and disconnect, the usage record, and every
  provider exchange the gateway relayed, request and response bodies verbatim (capped at 2 MB each).
  Package files, workspace snapshots, homes and provider keys are not in it.
- **Who it names.** The organization, the member and the team are replaced by pseudonyms: the same
  value gives the same pseudonym on one deployment and an unrelated one on any other. The harness
  name and the package commit are kept, since the trace is only useful with them. What a member
  typed into a prompt, or a package wrote to a file the model read, is in the trace as it was.
- **When it ends.** Turning sharing off removes the organization from the export at once, past
  sessions included. Turning it on again shares only sessions from that moment on.

The deployer reads it with `GET /v1/dataset?since=&until=` (one JSON line per session) and `GET
/v1/dataset/summary`. Nothing leaves a self-hosted deployment unless its deployer sends it.
