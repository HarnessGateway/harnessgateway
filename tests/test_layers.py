"""The layer rule: a module imports only modules of its layer or below; cloud SDKs appear only in the
two backend modules. A failure here is a design regression, not a bug."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "harnessgateway"
LAYERS = {"errors": 0, "execution": 0, "secrets": 0, "files": 0,
          "runtimes": 1, "runtimes.process": 1, "runtimes.docker": 1, "runtimes.door": 1,
          "harness": 2, "gitstore": 2, "wire": 2,
          "records": 3, "blobs": 3, "limits": 3, "store": 3, "catalog": 3, "accounts": 3,
          "sandboxes": 4, "gateway": 4,
          "sessions": 5, "worker": 5,
          "api": 6, "api.config": 6, "api.auth": 6, "api.accounts": 6, "api.app": 6, "api.deployer": 6, "api.internal": 6,
          "cli": 7, "__main__": 7, "__init__": 7}
VENDOR = {"boto3", "botocore", "psycopg", "google", "azure", "kubernetes", "openai"}
VENDOR_ALLOWED = {"blobs": {"boto3", "botocore"}, "records": {"psycopg"}, "wire": {"openai"}, "cli": {"openai"}}


def modules():
    for p in PACKAGE.rglob("*.py"):
        rel = p.relative_to(PACKAGE).with_suffix("").as_posix().replace("/", ".")
        yield rel.removesuffix(".__init__"), ast.parse(p.read_text())


def imported(tree, this: str) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = this.split(".")
            parent = base[:-node.level] if node.level <= len(base) else []
            prefix = ".".join(parent)
            mod = node.module or ""
            for alias in node.names:
                candidate = ".".join(p for p in (prefix, mod, alias.name) if p)
                out.add(candidate)
    return out


def layer_of(name: str) -> int:
    while name and name not in LAYERS:
        name = name.rsplit(".", 1)[0] if "." in name else ""
    return LAYERS[name] if name else 0


def test_imports_go_down_only():
    for this, tree in modules():
        for dep in imported(tree, this):
            top = dep.split(".")[0] if "." in dep else dep
            if top not in LAYERS and dep.split(".")[0] not in LAYERS:
                continue
            assert layer_of(dep) <= layer_of(this), f"{this} (layer {layer_of(this)}) imports {dep} (layer {layer_of(dep)})"


def test_no_vendor_sdk_outside_backends():
    for this, tree in modules():
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in VENDOR:
                    assert n in VENDOR_ALLOWED.get(this, set()), f"{this} imports {n}"
