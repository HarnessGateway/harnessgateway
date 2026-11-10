"""The Kubernetes launcher against a fake API server: the box pod's shape (gVisor, no DNS but the door, no token, read-only
root, the environment in a Secret owned by the pod), a relaunch over a stale pod, reaping, and the start-up checks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from harnessgateway.adapters import launch_k8s
from harnessgateway.adapters.launch_k8s import Kube, KubernetesLauncher
from harnessgateway.config import Settings
from harnessgateway.ports.launch import BoxSpec, LaunchError

NS = "hg-boxes"


class FakeApi:
    """Pods, Secrets, one door Service and the RuntimeClasses, in memory."""

    def __init__(self, runtime_classes: tuple[str, ...] = ("gvisor",), probe_exit: int = 0):
        self.pods: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.runtime_classes = runtime_classes
        self.probe_exit = probe_exit
        self.calls: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if path == "/api/v1/namespaces/hg/services/hg-door":
            return httpx.Response(200, json={"spec": {"clusterIP": "10.96.0.50"}})
        if path.startswith("/apis/node.k8s.io/v1/runtimeclasses/"):
            return httpx.Response(200 if path.rsplit("/", 1)[1] in self.runtime_classes else 404, json={})
        for kind, store in (("pods", self.pods), ("secrets", self.secrets)):
            base = f"/api/v1/namespaces/{NS}/{kind}"
            if path == base and method == "POST":
                name = body["metadata"]["name"]
                if name in store:
                    return httpx.Response(409, json={"reason": "AlreadyExists"})
                body["metadata"].update(uid=f"uid-{name}", creationTimestamp="2020-01-01T00:00:00Z")
                if kind == "pods":
                    terminated = {"exitCode": self.probe_exit} if name.startswith("hg-probe-") else None
                    body["status"] = {"phase": "Succeeded" if terminated else "Running", "containerStatuses": [
                        {"state": {"terminated": terminated} if terminated else {"running": {}}}]}
                store[name] = body
                return httpx.Response(201, json=body)
            if path == base and method == "GET":
                sel = request.url.params.get("labelSelector")
                return httpx.Response(200, json={"items": [o for o in store.values() if sel in o["metadata"]["labels"]]})
            if path.startswith(base + "/"):
                name = path.rsplit("/", 1)[1]
                if method == "GET":
                    return httpx.Response(200, json=store[name]) if name in store else httpx.Response(404, json={})
                if method == "DELETE":
                    return httpx.Response(200, json=store.pop(name)) if name in store else httpx.Response(404, json={})
        return httpx.Response(404, json={"message": f"no route {method} {path}"})


def launcher(api: FakeApi, tmp_path: Path, **overrides: Any) -> KubernetesLauncher:
    token = tmp_path / "token"
    token.write_text("t0ken")
    settings = Settings(launcher="kubernetes", k8s_namespace=NS, k8s_door_service="hg/hg-door",
                        api_url="http://hg-api.hg:8000", door="hg-door:3128", sandbox_image="sandbox:test", **overrides)
    kube = Kube("https://k8s.test", token, None, transport=httpx.MockTransport(api.handle))
    return KubernetesLauncher(settings, kube)


SPEC = BoxSpec("box_0123456789abcdef0123456789abcdef", "org_x", "hgb_secret_token", 1.5, 2048, 512, 256, "session")


async def test_a_box_is_a_gvisor_pod_fenced_to_the_door_with_its_env_in_a_secret(tmp_path):
    api = FakeApi()
    ref = await launcher(api, tmp_path).start(SPEC)
    assert ref == "hg-box-456789abcdef"
    pod = api.pods[ref]
    spec, box = pod["spec"], pod["spec"]["containers"][0]
    assert pod["metadata"]["labels"]["harnessgateway.box"] == SPEC.box_id
    assert spec["runtimeClassName"] == "gvisor" and spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False
    assert spec["dnsPolicy"] == "None" and spec["dnsConfig"] == {"nameservers": ["127.0.0.1"]}
    assert spec["hostAliases"] == [{"ip": "10.96.0.50", "hostnames": ["hg-door"]}]
    assert spec["securityContext"]["runAsUser"] == 10001 and spec["securityContext"]["runAsNonRoot"] is True
    sec = box["securityContext"]
    assert sec["readOnlyRootFilesystem"] is True and sec["allowPrivilegeEscalation"] is False
    assert sec["capabilities"] == {"drop": ["ALL"]}
    assert box["resources"]["limits"] == {"cpu": "1.5", "memory": "2560Mi"}
    assert box["resources"]["requests"] == {"cpu": "0.375", "memory": "640Mi"}
    assert {m["mountPath"] for m in box["volumeMounts"]} >= {"/workspace", "/home/hg", "/tmp", "/opt/hg/package"}
    assert "hgb_secret_token" not in json.dumps(pod)                         # the token is never in the pod spec
    secret = api.secrets[ref]
    assert secret["stringData"]["HG_BOX_TOKEN"] == "hgb_secret_token" and secret["stringData"]["HG_DOOR"] == "hg-door:3128"
    assert secret["metadata"]["ownerReferences"][0]["uid"] == f"uid-{ref}"    # deleted with its pod
    assert box["envFrom"] == [{"secretRef": {"name": ref}}]
    assert box["args"] == ["python3", "/opt/hg/runner.py"] and "command" not in box   # under the image's tini


async def test_runc_drops_the_runtime_class(tmp_path):
    api = FakeApi()
    ref = await launcher(api, tmp_path, sandbox_runtime="runc").start(SPEC)
    assert "runtimeClassName" not in api.pods[ref]["spec"]


async def test_a_stale_pod_of_the_same_box_is_replaced(tmp_path):
    api = FakeApi()
    lx = launcher(api, tmp_path)
    ref = await lx.start(SPEC)
    api.secrets.pop(ref)                          # as if the worker died between the pod and its Secret
    api.pods[ref]["metadata"]["uid"] = "old"
    assert await lx.start(SPEC) == ref
    assert api.pods[ref]["metadata"]["uid"] == f"uid-{ref}" and ref in api.secrets


async def test_stop_and_reap(tmp_path, monkeypatch):
    api = FakeApi()
    lx = launcher(api, tmp_path)
    a = await lx.start(SPEC)
    b = await lx.start(BoxSpec("box_ffffffffffffffffffffffffffffffff", "org_x", "t", 1, 512, 64, 64, "install"))
    api.pods[b]["status"] = {"phase": "Succeeded"}
    api.secrets["hg-box-orphan"] = {"metadata": {"name": "hg-box-orphan", "labels": {"harnessgateway.box": "x"},
                                                 "creationTimestamp": "2020-01-01T00:00:00Z"}}
    assert await lx.alive(a) is True and await lx.alive(b) is False
    assert await lx.reap() == 1
    assert set(api.pods) == {a} and set(api.secrets) == {a}
    await lx.stop(a)
    assert not api.pods and not api.secrets
    await lx.stop(a)                              # stopping twice is harmless


async def test_prepare_proves_the_fence(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_k8s.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.96.0.10", 8000))])
    await launcher(FakeApi(probe_exit=0), tmp_path).prepare()             # the door answers, the api does not
    with pytest.raises(LaunchError, match="does not enforce the box NetworkPolicy"):
        await launcher(FakeApi(probe_exit=11), tmp_path).prepare()
    with pytest.raises(LaunchError, match="cannot reach the door"):
        await launcher(FakeApi(probe_exit=10), tmp_path).prepare()
    with pytest.raises(LaunchError, match="RuntimeClass gvisor does not exist"):
        await launcher(FakeApi(runtime_classes=()), tmp_path).prepare()
    api = FakeApi(probe_exit=11)
    await launcher(api, tmp_path, k8s_probe=False).prepare()             # an operator may skip the probe
    assert not any(p.startswith("hg-probe-") for p in api.pods)


def test_settings_require_the_namespace_and_door_with_kubernetes():
    from harnessgateway.config import ConfigError
    env = {"HG_DATABASE_URL": "postgresql://x/y", "HG_BLOB_URL": "file:///tmp/x", "HG_SECRET_KEY": "a" * 44,
           "HG_DOOR_KEY": "hgd_x", "HG_LAUNCHER": "kubernetes"}
    with pytest.raises(ConfigError, match="HG_K8S_NAMESPACE"):
        Settings.load(("worker",), env=env)
    s = Settings.load(("worker",), env={**env, "HG_K8S_NAMESPACE": "b", "HG_K8S_DOOR_SERVICE": "hg/hg-door"})
    assert s.launcher == "kubernetes" and s.k8s_runtime_class == "gvisor" and s.k8s_probe is True
