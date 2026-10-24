"""The layer rule: a module imports only modules of its layer or below; vendor SDKs appear only in the
backend module that wraps them (and the SDK the wire is built on, in the contract). A failure here is a
design regression, not a bug."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "harnessgateway"
LAYERS = {"errors": 0, "kernel": 0,
          "contract": 1,
          "backends": 2,
          "domain": 3,
          "services": 4,
          "api": 5, "config": 5, "cli": 5, "__main__": 5, "__init__": 5}
VENDOR = {"boto3", "botocore", "psycopg", "google", "azure", "kubernetes", "openai"}
VENDOR_ALLOWED = {"backends.blobs.s3": {"boto3", "botocore"}, "backends.records.postgres": {"psycopg"}, "contract.wire": {"openai"}, "cli": {"openai"}}


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
