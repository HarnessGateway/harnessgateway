# Kubernetes

The Helm chart in `charts/harnessgateway` runs the platform on a Kubernetes cluster: the api, the worker, the door and
the console as Deployments, with a bundled Postgres and Redis or your own. The worker starts every box as a pod
(`HG_LAUNCHER=kubernetes`) in a namespace of its own, under gVisor, fenced by a NetworkPolicy so the only thing a box
can reach is the door. The design is SPEC §8.1; every setting is in [`operations.md`](operations.md).

## What the cluster needs

- **gVisor on the nodes that run boxes, and a RuntimeClass for it.** The chart's default is a RuntimeClass named
  `gvisor` whose handler is `runsc`. GKE Sandbox provides it (`gvisor`). Elsewhere, install `runsc` and
  `containerd-shim-runsc-v1` on those nodes and register the `runsc` runtime with containerd
  ([gVisor's containerd guide](https://gvisor.dev/docs/user_guide/containerd/quick_start/)); then
  `boxes.createRuntimeClass=true` makes the RuntimeClass, or make it yourself.
- **A CNI that enforces NetworkPolicy**: Calico, Cilium, GKE Dataplane V2, or the VPC CNI with network policies
  enabled on EKS. Without enforcement a box could reach the api, Postgres and the cluster's API server directly. The
  worker checks this before it starts any box and refuses to run otherwise (below).
- **A pid limit for pods**: the kubelet's `podPidsLimit` (a box's `pids` limit cannot be set per pod in Kubernetes).
- **Storage**: a default StorageClass for Postgres, the console's drafts and, without S3, the objects volume.
- Kubernetes 1.25 or later, and Helm 3.

## Install

```bash
helm install hg charts/harnessgateway -n hg --create-namespace \
  --set console.url=https://console.example.com
kubectl -n hg rollout status deploy/hg-harnessgateway-worker
kubectl -n hg exec deploy/hg-harnessgateway-api -- harnessgateway init --admin-email you@example.com
```

`init` prints, once, the first admin's invitation link and an admin API key. The console and the api are ClusterIP
Services; reach them with `kubectl port-forward`, or set `ingress.enabled` with a host for each (and TLS). Invitation
and SSO links are built on `console.url`.

The chart generates the secret key, the door key, the operator key and the database passwords into the Secret
`<release>-harnessgateway` on install, reads them back on every upgrade, and keeps the Secret after an uninstall, so a
reinstall over the same volumes still opens its data. Back it up with the database: without the secret key no
organization's data can be opened. `secrets.existingSecret` names a Secret of your own with the same keys instead.

## Values

| Key | Default | Meaning |
|---|---|---|
| `image.repository`, `image.tag` | `ghcr.io/harnessgateway/harnessgateway`, the chart's version | the platform image |
| `sandbox.repository`, `sandbox.tag` | `ghcr.io/harnessgateway/sandbox`, the chart's version | the image every box runs; a new one re-runs each package's install once |
| `sandbox.runtime` | `runsc` | `runc` drops the RuntimeClass: boxes run unisolated (tests only) |
| `boxes.namespace` | `<release namespace>-boxes` | where box pods run; the chart makes it (`boxes.createNamespace`) with the `restricted` Pod Security Standard |
| `boxes.runtimeClassName`, `boxes.createRuntimeClass` | `gvisor`, `false` | the RuntimeClass for `runsc`, and whether the chart makes it |
| `boxes.probe` | `true` | prove the fence at worker start-up |
| `objects.url` | — | `s3://bucket[/prefix][?region=…][&endpoint=…]`; empty: a volume shared by the api and the worker |
| `objects.envSecret` | — | a Secret with `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (or use the pods' cloud identity) |
| `objects.persistence.accessMode` | `ReadWriteOnce` | with a ReadWriteOnce volume the worker is scheduled beside the api; `ReadWriteMany` lets both spread |
| `postgresql.enabled`, `externalDatabase.existingSecret` | `true`, — | the bundled Postgres, or a Secret holding the URL of your own (an ordinary role, never a superuser or `BYPASSRLS`) |
| `redis.enabled`, `externalRedis.url` | `true`, — | the bundled Redis (nothing durable, no volume), or your own |
| `caBundle.configMap`, `caBundle.key` | —, `ca.crt` | a network that re-signs TLS: the CA the door trusts upstream and boxes get in their trust bundle |
| `config` | `HG_LOG_LEVEL: info` | further `HG_` settings for the api, worker and door (`HG_UPSTREAM_PROXY`, `HG_DEFAULT_*`, `HG_EXTENSIONS`, …) |
| `extraEnv` | `[]` | environment entries in Kubernetes form, e.g. `HG_SMTP_URL` from a Secret |
| `api.replicas`, `worker.replicas`, `door.replicas` | `1` | scale each role on its own; every one is stateless |
| `ingress.*` | off | one Ingress for the console's and the api's hosts |

## How a box is fenced

Each box is a pod in the boxes namespace with:

- the gVisor RuntimeClass; a read-only root; its directories as emptyDirs and `/tmp` in memory; uid 10001, every
  capability dropped, no privilege escalation, the RuntimeDefault seccomp profile; cpu and memory limits (a quarter of
  each reserved: the platform's own limits decide how many boxes run);
- no DNS (`dnsPolicy: None`, nameserver `127.0.0.1`) and one hosts entry, `hg-door`, at the door Service's cluster IP;
- no service-account token and no service links;
- its two environment values (its id and token) in a Secret the pod owns, never in the pod spec, deleted with the pod.

The chart's NetworkPolicy selects every pod with the `harnessgateway.box` label: no connection in, and out only to the
door's pods on the door's port. Everything else a box reaches (the package registry its install needs, the model
provider, the hosts its organization allows) goes through the door, which applies the organization's egress policy and
puts secrets in at the edge.

At start-up the worker checks that the RuntimeClass exists and runs a probe pod placed exactly as a box: the door must
answer it and the api must not. If either check fails the worker exits with the reason, and restarts until the
cluster is fixed:

| Worker log | Meaning |
|---|---|
| `RuntimeClass gvisor does not exist` | install gVisor on the nodes and make the RuntimeClass (`boxes.createRuntimeClass`) |
| `a box pod reached the api … the cluster does not enforce the box NetworkPolicy` | the CNI ignores NetworkPolicy: use one that enforces it |
| `a box pod cannot reach the door` | something else blocks box pods from the door (another policy, or the door is down) |

The worker's rights are a Role in the boxes namespace (pods and Secrets), reading the door's Service in its own, and
reading that one RuntimeClass.

When a box pod is deleted (a node drained, a pod evicted), its runner says so before it goes: the turn it was running
fails at once as `box_lost`, and the session's next turn starts a new pod that restores the session's files.

## Upgrade, scale, remove

- **Upgrade**: `helm upgrade hg charts/harnessgateway -n hg` with the new chart. The keys stay; migrations run when the
  api starts; a new sandbox image re-runs each package's install once, on the first turn that needs it.
- **Scale**: raise `api.replicas` and `door.replicas` freely. More than one worker, or workers on several nodes, needs
  the objects on S3 (`objects.url`) or a ReadWriteMany volume. Box pods go wherever the scheduler puts them.
- **Back up**: the Secret, Postgres (`kubectl -n hg exec hg-harnessgateway-postgres-0 -- pg_dump -U postgres
  harnessgateway`), and the objects volume or bucket.
- **Remove**: `helm uninstall hg -n hg` removes the workloads and the boxes namespace; the Secret, the objects and
  console volumes and Postgres's volume stay until you delete them.
