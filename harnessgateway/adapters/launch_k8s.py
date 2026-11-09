"""The launcher on Kubernetes (SPEC §8.1): one pod per box in HG_K8S_NAMESPACE, under the gVisor RuntimeClass unless the
operator chose `runc`. The worker runs in the cluster and speaks to the Kubernetes API with its service account.

Each box pod gets what a Docker box gets: a read-only root; a memory-backed /tmp of `tmp_mb`; its writable directories
as emptyDirs (nothing is put there before it starts: the runner fetches the package, the tools, the trust bundle and
the workspace itself); the runner under the image's tini, as with Docker (`args`, not `command`); uid 10001, no
capabilities, no privilege escalation; cpu and memory limits, a quarter of each reserved (the pid limit is the kubelet's
podPidsLimit); no DNS and one hosts entry, the door, at the address of the door's Service; no service
account token and no service links. Its two environment values live in a Secret owned by the pod, never in the pod
spec. A NetworkPolicy (the chart's) lets box pods reach the door and nothing else; at start-up the launcher proves that
with a probe pod: the door must answer and the api must not, or the worker refuses to start."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from ..ports.launch import BoxSpec, LaunchError
from .launch_docker import RUNNER, UID, WRITABLE

log = logging.getLogger("harnessgateway.launcher")
LABEL = "harnessgateway.box"
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
GRACE_S = 120                     # a Secret without its pod younger than this may belong to a launch in progress
PROBE_S = 180
FATAL = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError",
         "RunContainerError"}
PROBE = r"""
import socket, sys
def reach(host, port):
    try:
        socket.create_connection((host, int(port)), timeout=5).close()
        return True
    except OSError:
        return False
door, api = sys.argv[1].rsplit(":", 1), sys.argv[2].rsplit(":", 1)
sys.exit(10 if not reach(*door) else 11 if reach(*api) else 0)
"""


class Kube:
    """The few Kubernetes API calls the launcher makes, as the pod's service account (in-cluster only)."""

    def __init__(self, base: str, token: Path, ca: Path | None, transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca)) if ca else True
        self.http = httpx.AsyncClient(base_url=base, verify=verify, timeout=30, transport=transport)

    @classmethod
    def in_cluster(cls) -> Kube:
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host or not (SA / "token").is_file():
            raise LaunchError("HG_LAUNCHER=kubernetes needs the worker to run in the cluster with a service account")
        return cls(f"https://{host}:{port}", SA / "token", SA / "ca.crt")

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None,
                   params: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        token = self.token.read_text().strip() if self.token.is_file() else ""     # bound tokens rotate: read each time
        try:
            r = await self.http.request(method, path, json=body, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise LaunchError(f"the Kubernetes API did not answer {method} {path}: {type(e).__name__}") from None
        try:
            doc = r.json()
        except ValueError:
            doc = {}
        return r.status_code, doc if isinstance(doc, dict) else {}

    async def ok(self, what: str, method: str, path: str, body: dict[str, Any] | None = None,
                 params: dict[str, str] | None = None, allow: tuple[int, ...] = ()) -> dict[str, Any]:
        status, doc = await self.call(method, path, body, params)
        if status >= 300 and status not in allow:
            raise LaunchError(f"{what}: {status} {str(doc.get('message') or doc.get('reason') or '')[:300]}")
        return doc


class KubernetesLauncher:
    def __init__(self, settings: Settings, kube: Kube | None = None):
        self.s = settings
        self._kube = kube
        self.ns = settings.k8s_namespace
        self.door_ip = ""
        if settings.sandbox_runtime != "runsc":
            log.warning("HG_SANDBOX_RUNTIME=%s: box pods run without gVisor and are not isolated from the node's kernel",
                        settings.sandbox_runtime)

    @property
    def kube(self) -> Kube:
        if self._kube is None:
            self._kube = Kube.in_cluster()
        return self._kube

    @staticmethod
    def name(box_id: str) -> str:
        return f"hg-box-{box_id[-12:]}"

    # ── the pod ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def _base_spec(self) -> dict[str, Any]:
        """What every pod the launcher makes shares: the sandbox, the user, no DNS but the door, nothing mounted from
        the cluster."""
        door_host = self.s.door.rsplit(":", 1)[0]
        spec: dict[str, Any] = {
            "restartPolicy": "Never", "hostname": "sandbox", "automountServiceAccountToken": False,
            "enableServiceLinks": False, "dnsPolicy": "None", "dnsConfig": {"nameservers": ["127.0.0.1"]},
            "hostAliases": [{"ip": self.door_ip, "hostnames": [door_host]}], "terminationGracePeriodSeconds": 5,
            "securityContext": {"runAsUser": UID, "runAsGroup": UID, "runAsNonRoot": True, "fsGroup": UID,
                                "seccompProfile": {"type": "RuntimeDefault"}},
        }
        if self.s.sandbox_runtime == "runsc":
            spec["runtimeClassName"] = self.s.k8s_runtime_class
        return spec

    @staticmethod
    def _container_security() -> dict[str, Any]:
        return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "privileged": False,
                "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}}

    def pod(self, spec: BoxSpec, name: str) -> dict[str, Any]:
        volumes = [{"name": f"w-{n}", "emptyDir": {}} for n in WRITABLE]
        volumes.append({"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": f"{spec.tmp_mb}Mi"}})
        mounts = [{"name": f"w-{n}", "mountPath": target} for n, target in WRITABLE.items()]
        mounts.append({"name": "tmp", "mountPath": "/tmp"})                                          # noqa: S108
        memory_mb = spec.memory_mb + spec.tmp_mb                 # /tmp is memory: it counts, as Docker's tmpfs does
        # limits are the box's size; the pod reserves a quarter of it, as a Docker box reserves nothing: boxes rarely
        # use their whole size, and the platform's own capacity accounting decides how many run
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": self.ns,
                         "labels": {LABEL: spec.box_id, "harnessgateway.org": spec.org_id, "harnessgateway.kind": spec.kind}},
            "spec": {**self._base_spec(), "volumes": volumes, "containers": [{
                "name": "box", "image": self.s.sandbox_image, "imagePullPolicy": self.s.k8s_pull_policy,
                "args": RUNNER, "envFrom": [{"secretRef": {"name": name}}], "volumeMounts": mounts,
                "securityContext": self._container_security(),
                "resources": {"limits": {"cpu": str(spec.vcpus), "memory": f"{memory_mb}Mi"},
                              "requests": {"cpu": str(round(spec.vcpus / 4, 3)), "memory": f"{-(-memory_mb // 4)}Mi"}}}]},
        }

    # ── the port ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def start(self, spec: BoxSpec) -> str:
        if not self.door_ip:
            await self._resolve_door()
        name = self.name(spec.box_id)
        pods = f"/api/v1/namespaces/{self.ns}/pods"
        status, _ = await self.kube.call("POST", pods, self.pod(spec, name))
        if status == 409:                    # a launch that never recorded its pod (the worker restarted): start over
            await self.stop(name)
            for _ in range(30):
                if (await self.kube.call("GET", f"{pods}/{name}"))[0] == 404:
                    break
                await asyncio.sleep(1)
            status, _ = await self.kube.call("POST", pods, self.pod(spec, name))
        if status >= 300:
            raise LaunchError(f"box pod: {status}")
        try:
            created = await self.kube.ok("box pod", "GET", f"{pods}/{name}")
            env = {**spec.env(self.s.api_url, self.s.door), "HG_RUN_UID": str(UID)}
            await self.kube.ok("box secret", "POST", f"/api/v1/namespaces/{self.ns}/secrets", {
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": self.ns, "labels": {LABEL: spec.box_id},
                             "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": name,
                                                  "uid": created["metadata"]["uid"]}]},
                "stringData": env})
        except BaseException:
            await self.stop(name)
            raise
        return name

    async def stop(self, reference: str) -> None:
        body = {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0, "propagationPolicy": "Background"}
        for kind in ("pods", "secrets"):
            try:
                await self.kube.call("DELETE", f"/api/v1/namespaces/{self.ns}/{kind}/{reference}", body)
            except LaunchError as e:
                log.warning("could not delete %s %s: %s", kind, reference, e)

    async def alive(self, reference: str) -> bool:
        status, pod = await self.kube.call("GET", f"/api/v1/namespaces/{self.ns}/pods/{reference}")
        return status == 200 and not self._finished(pod)

    @staticmethod
    def _finished(pod: dict[str, Any]) -> bool:
        """Done, failed, or stuck where it can never run (an image it cannot pull, a config it cannot use)."""
        st = pod.get("status") or {}
        if st.get("phase") in ("Succeeded", "Failed", "Unknown") or (pod.get("metadata") or {}).get("deletionTimestamp"):
            return True
        return any(((c.get("state") or {}).get("waiting") or {}).get("reason") in FATAL
                   for c in st.get("containerStatuses") or [])

    async def reap(self) -> int:
        """Finished box pods removed (with their Secrets); Secrets left without a pod removed."""
        params = {"labelSelector": LABEL}
        pods = (await self.kube.ok("list pods", "GET", f"/api/v1/namespaces/{self.ns}/pods", params=params)).get("items", [])
        live, n = set(), 0
        for pod in pods:
            name = pod["metadata"]["name"]
            if self._finished(pod) and not name.startswith("hg-probe-"):
                await self.stop(name)
                n += 1
            else:
                live.add(name)
        found = await self.kube.ok("list secrets", "GET", f"/api/v1/namespaces/{self.ns}/secrets", params=params)
        for secret in found.get("items", []):
            meta = secret["metadata"]
            if meta["name"] not in live and _age(meta.get("creationTimestamp")) > GRACE_S:
                await self.kube.call("DELETE", f"/api/v1/namespaces/{self.ns}/secrets/{meta['name']}")
        return n

    async def prepare(self) -> None:
        """Before the first launch: the door's address, the RuntimeClass, and proof that the NetworkPolicy holds."""
        await self._resolve_door()
        if self.s.sandbox_runtime == "runsc":
            status, _ = await self.kube.call("GET", f"/apis/node.k8s.io/v1/runtimeclasses/{self.s.k8s_runtime_class}")
            if status == 404:
                raise LaunchError(f"RuntimeClass {self.s.k8s_runtime_class} does not exist: install gVisor on the nodes and "
                                  "create it (handler runsc), or set HG_SANDBOX_RUNTIME=runc to run boxes unisolated")
            if status >= 300:
                raise LaunchError(f"RuntimeClass {self.s.k8s_runtime_class} could not be read: {status}")
        if self.s.k8s_probe:
            await self.probe()

    async def _resolve_door(self) -> None:
        ns, _, name = self.s.k8s_door_service.rpartition("/")
        svc = await self.kube.ok("the door's Service", "GET", f"/api/v1/namespaces/{ns or self.ns}/services/{name}")
        ip = (svc.get("spec") or {}).get("clusterIP") or ""
        if not ip or ip == "None":
            raise LaunchError(f"the door's Service {self.s.k8s_door_service} has no cluster IP")
        self.door_ip = ip

    async def probe(self) -> None:
        """A pod placed as a box: the door must answer it, the api must not. Otherwise boxes are either cut off or not
        fenced, and the worker refuses to launch any."""
        u = urlsplit(self.s.api_url)
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            api_ip = (await asyncio.to_thread(socket.getaddrinfo, u.hostname, port, socket.AF_INET))[0][4][0]
        except (OSError, IndexError):
            raise LaunchError(f"the api's address {u.hostname} does not resolve from the worker") from None
        door_port = self.s.door.rsplit(":", 1)[1]
        name = f"hg-probe-{secrets.token_hex(4)}"
        pod = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"name": name, "namespace": self.ns, "labels": {LABEL: "probe", "harnessgateway.kind": "probe"}},
               "spec": {**self._base_spec(), "containers": [{
                   "name": "probe", "image": self.s.sandbox_image, "imagePullPolicy": self.s.k8s_pull_policy,
                   "command": ["python3", "-c", PROBE, f"{self.door_ip}:{door_port}", f"{api_ip}:{port}"],
                   "securityContext": self._container_security(),
                   "resources": {"limits": {"cpu": "250m", "memory": "128Mi"}}}]}}
        await self.kube.ok("probe pod", "POST", f"/api/v1/namespaces/{self.ns}/pods", pod)
        try:
            code = await self._exit_code(name)
        finally:
            await self.kube.call("DELETE", f"/api/v1/namespaces/{self.ns}/pods/{name}",
                                 {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0})
        if code == 10:
            raise LaunchError("a box pod cannot reach the door: the NetworkPolicy must allow box pods to the door's port")
        if code == 11:
            raise LaunchError(f"a box pod reached the api at {api_ip}:{port} directly: the cluster does not enforce the "
                              "box NetworkPolicy (use a CNI that enforces NetworkPolicy, e.g. Cilium or Calico)")
        if code != 0:
            raise LaunchError(f"the network probe pod ended with exit code {code}")
        log.info("network probe: box pods reach the door and not the api")

    async def _exit_code(self, name: str) -> int:
        deadline = time.monotonic() + PROBE_S
        while time.monotonic() < deadline:
            status, pod = await self.kube.call("GET", f"/api/v1/namespaces/{self.ns}/pods/{name}")
            st = pod.get("status") or {}
            for c in st.get("containerStatuses") or []:
                done = (c.get("state") or {}).get("terminated")
                if done:
                    return int(done.get("exitCode", 1))
                reason = ((c.get("state") or {}).get("waiting") or {}).get("reason")
                if reason in FATAL:
                    raise LaunchError(f"the probe pod cannot start: {reason}: "
                                      f"{((c.get('state') or {}).get('waiting') or {}).get('message', '')[:200]}")
            await asyncio.sleep(2)
        raise LaunchError(f"the probe pod did not finish in {PROBE_S}s: {json.dumps(st)[:300]}")


def _age(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    from datetime import UTC, datetime
    return (datetime.now(UTC) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
