# Managed SaaS deployment

The managed service runs the same image and code as self-hosted. What changes is where each role runs and which
managed services back it.

```
             clients (OpenAI SDK)                         hybrid customers' nodes (optional)
                    │ HTTPS                                         │ HTTPS, outbound only
        ┌───────────▼────────────┐                                  │
        │ load balancer (TLS)    │◄─────────────────────────────────┘
        └──────┬─────────┬───────┘
               │         │ (internal LB or private DNS)
        ┌──────▼───┐ ┌───▼──────┐       managed Postgres (app role: no superuser, no BYPASSRLS)
        │ api  xN  │ │gateway xN│ ────► S3 bucket (objects)          KMS key (HG_KMS_KEY_ID)
        └──────────┘ └──────────┘
               ▲ pull jobs, stream events (HTTPS)
        ┌──────┴───────────────────────────────┐
        │ node pool (VMs, autoscaled)          │  Docker + gVisor, one sandbox per session,
        │ install-gvisor.sh + run-node.sh      │  egress only through each sandbox's door
        └──────────────────────────────────────┘
```

## Control plane (stateless; scale horizontally)

- **api**: `harnessgateway serve --role api`, 2+ replicas behind the load balancer. Applies migrations on start (under
  an advisory lock, so replicas starting together are safe). Housekeeping runs in every replica and takes advisory
  locks, so each job runs once.
- **gateway**: `harnessgateway serve --role gateway`, 2+ replicas, reachable by nodes only (internal load balancer in
  the VPC; for hybrid nodes, a separate TLS endpoint). It is the only process that decrypts provider keys: give it
  `kms:Decrypt` and run it in its own service/security group.
- `deploy/saas/kubernetes.yaml` is a starting manifest for both on Kubernetes; ECS or plain VMs work the same way.

## Backing services

| Concern | Managed service | Settings |
|---|---|---|
| Records | RDS / Aurora Postgres, Cloud SQL | `HG_DATABASE_URL` (an ordinary role that owns the database) |
| Objects | S3, GCS (S3 interoperability) | `HG_S3_BUCKET`, `HG_S3_REGION`; credentials from the instance role (leave keys empty) |
| Master key | AWS KMS | `HG_KMS_KEY_ID`; the api needs `kms:Encrypt`, the gateway `kms:Decrypt` |
| Secrets | Secrets Manager / SSM | `HG_SECRET_KEY` (pepper and URL signing), `HG_OPERATOR_KEY`, `HG_NODE_JOIN_TOKEN` |

## Node pool

- VMs with nested virtualization not required: gVisor's `systrap` platform runs on ordinary cloud instances.
- Boot with `cloud-init.yaml` (runs `install-gvisor.sh`, then `run-node.sh` with the shared join token). Nodes join on
  first start and keep their credential in `HG_NODE_DIR`.
- Size: each sandbox takes its organization's `vcpus` and `memory_mb`; set `HG_NODE_SLOTS` so slots x size fits the VM.
- Scale on queue depth (`SELECT count(*) FROM turns WHERE status = 'queued'`) or on node slot usage
  (`GET /admin/v1/nodes`).
- Drain a node before terminating it: `POST /admin/v1/nodes/{id}/drain` stops new claims while running turns finish;
  then `DELETE /admin/v1/nodes/{id}`. Its sessions restore from their snapshots on other nodes.

## Hybrid (customer-run nodes)

A customer runs nodes in their own network; sandboxes, workspaces and outbound traffic stay there:

1. `POST /admin/v1/node-tokens {"org_id": "org_...", "label": "acme-dc1", "uses": 10}` returns a dedicated join token.
2. `PATCH /admin/v1/organizations/org_... {"limits": {"dedicated_nodes": true}}` so the organization's turns run only
   on its own nodes.
3. The customer prepares hosts with `install-gvisor.sh` and starts `run-node.sh` with `HG_API_URL` and
   `HG_GATEWAY_URL` set to the public TLS endpoints. Nodes only make outbound HTTPS connections.

Compute on dedicated nodes is still metered; price it with a deployment-specific `HG_PRICES` (for example, a platform
fee per vCPU-second) if the customer pays for their own machines.

## Verify

`deploy/verify-harnesses.py` signs up a throwaway organization through an invite, installs the built-in harnesses and
runs eight checks on each (file in the sandbox, recall, streaming, instructions, usage, cancel, provider error, traces):

```bash
pip install openai httpx
HG_API=https://api.example.com HG_OPERATOR_KEY=... DEEPSEEK_API_KEY=... OPENAI_API_KEY=... python deploy/verify-harnesses.py
```

## Invite-only pilot

```bash
curl -X POST https://api.example.com/admin/v1/invites -H "Authorization: Bearer $HG_OPERATOR_KEY" \
  -d '{"label": "pilot-acme", "uses": 1, "ttl_days": 14, "limits": {"max_sandboxes": 20, "monthly_charge_usd": 500}}'
```

The organization redeems the code at `POST /v1/signup {"invite_code", "organization", "email"}` and receives its first
admin key. `HG_SIGNUP=closed` disables sign-up entirely.
