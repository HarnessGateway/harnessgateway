# Billing

The platform charges for what it costs to run: compute, network and storage for execution and sessions. Model usage
is never resold: organizations bring their own providers and pay them directly.

## Meters

| Meter | Source | Unit |
|---|---|---|
| `compute.vcpu_seconds` | node: allocated vCPUs × seconds each sandbox exists (warm time included) and each install runs | vCPU-second |
| `compute.memory_gb_seconds` | node: allocated memory × the same seconds | GB-second |
| `network.egress_bytes` | node (bytes a sandbox sends out through its door, provider calls included) and api (bytes sent to clients) | byte |
| `network.ingress_bytes` | node (bytes a sandbox receives) and api (bytes uploaded by clients) | byte |
| `storage.gb_hours` | api: every live object of the organization (snapshots, files, requests, logs, captures), accrued hourly | GB-hour |

Compute is billed on allocation, not utilization, because allocation is what the platform pays for. An organization's
`idle_ttl_s` sets how long a warm sandbox waits for the next turn (and is billed); 0 stops it after every turn.

## Price

`charge = Σ quantity × unit_cost × markup`. Unit costs are the deployment's own infrastructure costs, set with
`HG_PRICES` (JSON, USD per unit) or `HG_PRICES_FILE`; the markup is `HG_MARKUP` (default 3). Defaults approximate a
public cloud's on-demand prices:

```json
{"compute.vcpu_seconds": 0.000011, "compute.memory_gb_seconds": 0.0000012,
 "network.egress_bytes": 0.00000000009, "network.ingress_bytes": 0, "storage.gb_hours": 0.000032}
```

Observability (captures of harness and provider exchanges) is enabled per organization (`observability` limit). Its
captures are stored objects, so they are billed as storage like everything else.

## Reading usage

- Organization: `GET /v1/usage?start=&end=` → lines per meter with quantity, unit cost, cost and charge, plus totals.
- Operator: `GET /admin/v1/organizations/{id}/usage`.
- Raw events: `usage_events` (append-only), for export to an invoicing system.

## Caps

`monthly_charge_usd` (per organization) refuses new turns once the month's charge reaches it (`429 spending_limit_reached`);
`storage_gb` refuses new turns while the organization's stored objects exceed it (`429 storage_limit_reached`).
