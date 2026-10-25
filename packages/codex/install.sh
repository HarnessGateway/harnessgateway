#!/usr/bin/env bash
# Installs the OpenAI Codex CLI, pinned, into $HG_TOOLS. @openai/codex is a small node launcher; the real CLI is a
# native binary shipped as the optional dependency @openai/codex-linux-x64 (an alias of @openai/codex@<v>-linux-x64),
# so the install fails unless that binary is present and runs here.
#
# It also writes $HG_TOOLS/codex-models.json, Codex's model catalog (run.py points model_catalog_json at it): the
# catalog bundled with this binary plus entries for the GPT-5.4 family, which 0.164.0 no longer ships. Without an
# entry Codex runs a model on "fallback metadata" (no apply_patch tool, no reasoning summaries, generic prompt).
set -euo pipefail
VERSION=0.164.0
mkdir -p "$HG_TOOLS/bin"
npm install --global --prefix "$HG_TOOLS" --no-audit --no-fund --include=optional --omit=dev "@openai/codex@$VERSION"
native=$(find "$HG_TOOLS/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64" -path '*/bin/codex' -type f 2>/dev/null | head -n1)
if [ -z "$native" ] || [ ! -x "$native" ]; then
  echo "the linux x64 Codex binary (@openai/codex-linux-x64) was not installed" >&2
  exit 1
fi
"$HG_TOOLS/bin/codex" --version
"$native" --version

home=$(mktemp -d)
CODEX_HOME="$home" "$native" debug models --bundled 2>/dev/null > "$home/bundled.json"
python3 - "$home/bundled.json" "$HG_TOOLS/codex-models.json" <<'PY'
import copy, json, sys
catalog = json.load(open(sys.argv[1]))
models = catalog["models"]
slugs = {m["slug"] for m in models}
base = next(m for m in models if m["slug"] == "gpt-5.5")
added = []
for slug, name in (("gpt-5.4", "GPT-5.4"), ("gpt-5.4-mini", "GPT-5.4 mini"), ("gpt-5.4-nano", "GPT-5.4 nano")):
    if slug in slugs:
        continue
    m = copy.deepcopy(base)
    m.update(slug=slug, display_name=name, description=f"{name} (entry added by the HarnessGateway package)",
             upgrade=None, availability_nux=None, additional_speed_tiers=[], service_tiers=[], priority=90 + len(added))
    added.append(m)
catalog["models"] = models + added
json.dump(catalog, open(sys.argv[2], "w"))
print("model catalog:", ", ".join(m["slug"] for m in catalog["models"]))
PY
rm -rf "$home"
