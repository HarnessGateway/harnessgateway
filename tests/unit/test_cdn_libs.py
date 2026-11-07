"""The chat's CDN libraries are pinned in console/chat/package.json (what Dependabot moves) and loaded by version with an
integrity hash in rich.js and viewer.js (what the browser checks). The two must agree: `scripts/sync-cdn-libs` writes
the manifest's versions and fresh hashes into the scripts."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "console/chat/static"


def test_the_scripts_load_the_versions_the_manifest_pins():
    manifest = json.loads((ROOT / "console/chat/package.json").read_text())["devDependencies"]
    scripts = (STATIC / "rich.js").read_text() + (STATIC / "viewer.js").read_text()
    loaded = {pkg: ver for pkg, ver in re.findall(r"\$\{CDN\}/(@?[A-Za-z0-9./-]+?)@([0-9][0-9A-Za-z.\-]*)/", scripts)}
    assert loaded == manifest, "run scripts/sync-cdn-libs"
    for url in re.findall(r"src: `[^`]+`|css: `[^`]+`|src: \"[^\"]+\"", scripts):
        assert "@" in url or "cdn.sheetjs.com/xlsx-" in url        # every library is pinned by version


def test_every_cdn_script_has_an_integrity_hash():
    for name in ("rich.js", "viewer.js"):
        text = (STATIC / name).read_text()
        loads = re.findall(r"(?:src|css): [`\"][^`\"]+\.min\.(?:js|css)[`\"]", text)
        assert loads and len(loads) == len(re.findall(r"[iI]ntegrity: \"sha384-", text))
