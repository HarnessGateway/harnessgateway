# Kubernetes

The Helm chart in `charts/harnessgateway` runs the platform on a Kubernetes cluster: the api, the worker, the door and
the console as Deployments, with a bundled Postgres and Redis or your own. The worker starts every box as a pod
(`HG_LAUNCHER=kubernetes`) in a namespace of its own, under gVisor, fenced by a NetworkPolicy so the only thing a box
can reach is the door. The design is SPEC §8.1; every setting is in [`operations.md`](operations.md).

## Before you start

You need:

- **A cluster you administer**, Kubernetes 1.25 or later, with `kubectl` pointed at it
  ([install kubectl](https://kubernetes.io/docs/tasks/tools/#kubectl)). The chart creates a namespace, a
  NetworkPolicy, a Role and, if you ask, a RuntimeClass, so you need the rights to create those. No cluster yet:
  [A cluster on one Linux machine](#a-cluster-on-one-linux-machine) builds one.
- **Helm 3** ([install Helm](https://helm.sh/docs/intro/install/)) and **git**: the chart is in this repository.
- **The first admin's email address**, and a key for a model provider (Anthropic, OpenAI, Gemini or a compatible
  endpoint) to run a first turn with.

The cluster needs:

- **gVisor on the nodes that run boxes, and a RuntimeClass for it.** The chart's default is a RuntimeClass named
  `gvisor` whose handler is `runsc`. GKE Sandbox provides it (`gvisor`). Elsewhere, install gVisor on those nodes
  ([gVisor's installation guide](https://gvisor.dev/docs/user_guide/install/)) and register the `runsc` runtime with
  containerd ([gVisor's containerd guide](https://gvisor.dev/docs/user_guide/containerd/quick_start/)); then
  `boxes.createRuntimeClass=true` makes the RuntimeClass, or make it yourself. gVisor is `runsc`, its containerd shim
  `containerd-shim-runsc-v1` and a `gvisor-bin/` directory of helpers that `runsc` looks for beside itself: a node
  needs all three, in one directory on containerd's path. It needs Linux 5.6 or later, on x86_64 or arm64.
- **A CNI that enforces NetworkPolicy**: [Calico](https://docs.tigera.io/calico/latest/getting-started/kubernetes/),
  [Cilium](https://docs.cilium.io/en/stable/gettingstarted/), GKE Dataplane V2, or the VPC CNI with network policies
  enabled on EKS. Without enforcement a box could reach the api, Postgres and the cluster's API server directly. The
  worker checks this before it starts any box and refuses to run otherwise (below).
- **A pid limit for pods**: the kubelet's `podPidsLimit` (a box's `pids` limit cannot be set per pod in Kubernetes).
- **Storage**: a default StorageClass for Postgres, the console's drafts and, without S3, the objects volume.
- **Images the nodes can pull**: `ghcr.io/harnessgateway/harnessgateway` and `ghcr.io/harnessgateway/sandbox`
  (public), and `postgres` and `redis` from Docker Hub unless you bring your own.
- **Room**: the platform's pods request about 0.65 CPU and 1.3 GiB of memory in all. Each box is limited to 2 CPUs
  and 4 GiB by default (plus up to 4 GiB of `/tmp`) and reserves a quarter of that.

Check it before installing:

```bash
kubectl version                          # the client, and the server at 1.25 or later
kubectl get runtimeclass                 # one whose HANDLER is runsc (or let the chart make it)
kubectl get storageclass                 # one marked (default)
helm version --short
```

## Install

```bash
git clone https://github.com/HarnessGateway/harnessgateway && cd harnessgateway
helm install hg charts/harnessgateway -n hg --create-namespace \
  --set console.url=https://console.example.com --wait --timeout 10m
kubectl -n hg exec deploy/hg-harnessgateway-api -c api -- harnessgateway init --admin-email you@example.com
```

`console.url` is where people will open the console: invitation and SSO links are built on it. Add
`--set boxes.createRuntimeClass=true` where containerd has the `runsc` handler and no RuntimeClass names it yet.
`init` prints, once, the first admin's invitation link and an admin API key: keep both.

The worker then proves the fence, which takes a minute or two the first time (a node pulls the sandbox image). Wait
for this line before sending a turn; until it is there no box starts and turns wait in the queue:

```bash
kubectl -n hg logs deploy/hg-harnessgateway-worker -c worker | grep "network probe"
# … "network probe: box pods reach the door and not the api"
```

If it does not appear, [How a box is fenced](#how-a-box-is-fenced) lists what the worker says instead.

The console and the api are ClusterIP Services. Reach them with `kubectl port-forward`, or set `ingress.enabled` with
a host for each (and TLS):

```bash
kubectl -n hg port-forward svc/hg-harnessgateway-console 8800:8800       # http://localhost:8800
kubectl -n hg port-forward svc/hg-harnessgateway-api 8000:8000           # http://localhost:8000/v1
```

A port-forward listens on the machine `kubectl` runs on, and on `localhost` only. To open the console from another
machine, add `--address 0.0.0.0` to both and install with `console.url` set to that machine's address
(`http://<host>:8800`).

Open the invitation link and choose a password. Then, as on any installation: add a provider under **Setup →
Providers**, add a harness under **Setup → Harnesses** (or ask the built-in `harness-package-builder` in the chat),
and run a turn in **Workbench → Chat**. A harness's first turn waits for its install box, then for the session's
box; watch them with `kubectl -n hg-boxes get pods -w`.

The chart generates the secret key, the door key, the operator key and the database passwords into the Secret
`<release>-harnessgateway` on install, reads them back on every upgrade, and keeps the Secret after an uninstall, so a
reinstall over the same volumes still opens its data. Back it up with the database: without the secret key no
organization's data can be opened. `secrets.existingSecret` names a Secret of your own with the same keys instead.

## A cluster on one Linux machine

For a trial or for development: a one-node cluster made with [kind](https://kind.sigs.k8s.io/) (Kubernetes in a
Docker container), with gVisor in the node and Calico enforcing NetworkPolicy. These steps were run on Ubuntu 26.04
(arm64) with kind v0.30.0, Kubernetes v1.34, Calico v3.30.3 and gVisor release 20261109; they are the same on x86_64.
On macOS or Windows, run them in a Linux virtual machine.

Install these first, each by its own guide:

| What | Guide | Check |
|---|---|---|
| Docker Engine, usable by your user without `sudo` | [install](https://docs.docker.com/engine/install/), then [post-install](https://docs.docker.com/engine/install/linux-postinstall/) | `docker ps` |
| gVisor (on Debian and Ubuntu the apt package; its guide installs `gnupg` first) | [install](https://gvisor.dev/docs/user_guide/install/) | `runsc --version` |
| kind | [install](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | `kind version` |
| kubectl | [install](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/) | `kubectl version --client` |
| Helm 3 | [install](https://helm.sh/docs/intro/install/) | `helm version --short` |
| git and curl | your distribution's packages | |

1. **A node image with gVisor in it.** kind's node is a container, so gVisor goes into its image: `runsc`, the shim
   and `gvisor-bin/`, together. Use the node image your kind release names for the Kubernetes version you want.

   ```bash
   bin=$(dirname "$(command -v runsc)")
   mkdir kind-gvisor && cp -r "$bin/runsc" "$bin/containerd-shim-runsc-v1" "$bin/gvisor-bin" kind-gvisor/
   printf 'FROM kindest/node:v1.34.0\nCOPY runsc containerd-shim-runsc-v1 /usr/local/bin/\nCOPY gvisor-bin /usr/local/bin/gvisor-bin\n' \
     > kind-gvisor/Dockerfile
   docker build -t kind-node:gvisor kind-gvisor
   ```

2. **The cluster**, without kind's own network (Calico replaces it) and with the `runsc` runtime registered in
   containerd:

   ```bash
   cat > kind.yaml <<'EOF'
   kind: Cluster
   apiVersion: kind.x-k8s.io/v1alpha4
   networking:
     disableDefaultCNI: true
     podSubnet: 192.168.0.0/16
   nodes:
     - role: control-plane
       image: kind-node:gvisor
   containerdConfigPatches:
     - |-
       [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runsc]
         runtime_type = "io.containerd.runsc.v1"
   EOF
   kind create cluster --name hg --config kind.yaml
   ```

3. **Calico.** Its manifest, with the images from Calico's own registry on quay.io (Docker Hub limits anonymous
   pulls) and no IP-in-IP encapsulation, which one node does not need:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/projectcalico/calico/v3.30.3/manifests/calico.yaml \
     | sed -e 's|docker.io/calico/|quay.io/calico/|' -e '/CALICO_IPV4POOL_IPIP/{n;s/Always/Never/}' \
     | kubectl apply -f -
   kubectl -n kube-system rollout status ds/calico-node --timeout=300s
   ```

4. **HarnessGateway**, as under [Install](#install), with the chart making the RuntimeClass:

   ```bash
   git clone https://github.com/HarnessGateway/harnessgateway && cd harnessgateway
   helm install hg charts/harnessgateway -n hg --create-namespace --set boxes.createRuntimeClass=true \
     --wait --timeout 10m
   kubectl -n hg exec deploy/hg-harnessgateway-api -c api -- harnessgateway init --admin-email you@example.com
   kubectl -n hg logs deploy/hg-harnessgateway-worker -c worker | grep "network probe"
   ```

   Then the port-forwards and the first turn, as under [Install](#install). `console.url` defaults to
   `http://localhost:8800`, which is right when the browser runs on this machine.

To run images you built from a checkout, build them under the release's names and load them into the node before
`helm install`: `docker build -t ghcr.io/harnessgateway/harnessgateway:<version> .`,
`docker build -t ghcr.io/harnessgateway/sandbox:<version> -f sandbox/Dockerfile .`, then
`kind load docker-image <image> --name hg` for each. `kind delete cluster --name hg` removes everything.

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
| `the probe pod did not finish in 180s` | the probe pod never ran: `kubectl -n hg-boxes get events --sort-by=.lastTimestamp` says why. `sidecar "gvisor_sentry" not usable` there means a node has `runsc` without its `gvisor-bin/` directory |
| `the probe pod cannot start: ErrImagePull` (or `ImagePullBackOff`) | the nodes cannot pull the sandbox image (`sandbox.repository`, `sandbox.tag`) |
| nothing after the start-up lines | the probe is still running: a node is pulling the sandbox image, or the probe pod is waiting to start (the events above) |

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
